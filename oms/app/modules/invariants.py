"""Continuous invariant checks (spec §20 invariants; hardening test 3). Each check runs under the organization's own
identity (RLS applies). A violation opens an ``invariant_violation`` case with the offending references — never an
automatic correction. Runs from the API (finance, own org) and from a periodic worker task (all orgs)."""

from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules import exceptions

CHECKS = {
    "unbalanced_journals":
        "select journal_id::text as ref, sum(debit) - sum(credit) as amount from ledger_entries "
        "group by journal_id having sum(debit) <> sum(credit)",
    "contract_liability_after_full_delivery":
        "select o.id::text as ref, sum(le.credit - le.debit) as amount from orders o "
        "join ledger_entries le on le.order_id = o.id and le.account_code = '2200' "
        "where o.status in ('delivered','closed') group by o.id having sum(le.credit - le.debit) <> 0",
    "goods_in_transit_after_final_shipment":
        "select s.id::text as ref, sum(le.debit - le.credit) as amount from shipments s "
        "join ledger_entries le on le.order_id = s.order_id and le.account_code = '1250' "
        "where s.status in ('delivered','returned') "
        "and not exists (select 1 from shipments s2 where s2.order_id = s.order_id "
        "                and s2.status not in ('delivered','returned','canceled')) "
        "group by s.id having sum(le.debit - le.credit) <> 0",
    "quantity_pipeline":
        "select id::text as ref, 0 as amount from order_items where qty_delivered > qty_shipped "
        "or qty_returned > qty_delivered or qty_procured > qty_allocated_dropship "
        "or qty_canceled + qty_shipped > qty",
    "matched_reconciliation_with_difference":
        "select id::text as ref, difference as amount from reconciliation_items "
        "where status = 'matched' and abs(coalesce(difference, 0)) > 1",
    "negative_inventory":
        "select id::text as ref, on_hand as amount from inventory_balances where on_hand < 0 or reserved < 0 "
        "or reserved > on_hand",
    # --- remediation (Phase F) ------------------------------------------------------------------------------
    # A2: postings recorded twice before the unique posting key existed (flagged by the 0019 backfill).
    "legacy_duplicate_postings":
        "select id::text as ref, 0 as amount from journals where legacy_duplicate",
    # B2: stock subledger (on hand at moving average + goods in transit at the exact cost picked) = GL 1200 + 1250,
    # per portfolio. Legacy in-transit units without allocations are valued at their balance's average.
    "inventory_subledger_vs_gl":
        "with stock as ("
        "  select portfolio_id, on_hand * avg_unit_cost as v from inventory_balances"
        "  union all select portfolio_id, amount from shipment_cost_allocations where status = 'in_transit'"
        "  union all select b.portfolio_id, (b.in_transit - coalesce(a.q, 0)) * b.avg_unit_cost from inventory_balances b"
        "    left join (select balance_id, sum(qty) as q from shipment_cost_allocations"
        "               where status = 'in_transit' group by balance_id) a on a.balance_id = b.id),"
        " s as (select portfolio_id, sum(v) as v from stock group by portfolio_id),"
        " g as (select portfolio_id, sum(debit - credit) as v from ledger_entries"
        "       where account_code in ('1200','1250') group by portfolio_id)"
        " select coalesce(s.portfolio_id, g.portfolio_id)::text as ref, coalesce(s.v, 0) - coalesce(g.v, 0) as amount"
        " from s full join g on g.portfolio_id = s.portfolio_id"
        " where abs(coalesce(s.v, 0) - coalesce(g.v, 0)) > 1",
    # B1: a recognised transfer receivable can never be over-cleared.
    "transfer_receivable_negative":
        "select order_item_id::text as ref, sum(debit - credit) as amount from ledger_entries "
        "where account_code = '1105' and order_item_id is not null group by order_item_id "
        "having sum(debit - credit) < 0",
    # B1: a customer advance (contract liability) can never have a debit balance.
    "customer_advance_negative":
        "select order_item_id::text as ref, sum(credit - debit) as amount from ledger_entries "
        "where account_code = '2200' and order_item_id is not null group by order_item_id "
        "having sum(credit - debit) < 0",
    # B3: COD to collect over the order's shipments = goods (live quantity) + shipping, once all groups are booked.
    "cod_collectible_mismatch":
        "with sh as (select order_id, sum(cod_amount) as cod from shipments group by order_id),"
        " alloc as (select distinct on (order_item_id) order_item_id, amount from order_item_allocations"
        "           where kind = 'shipping_income' order by order_item_id, policy_version desc),"
        " due as (select oi.order_id, sum(round(oi.line_total * (oi.qty - oi.qty_canceled) / oi.qty, 2)"
        "                              + coalesce(a.amount, 0)) as v"
        "         from order_items oi left join alloc a on a.order_item_id = oi.id"
        "         where oi.qty_canceled < oi.qty group by oi.order_id)"
        " select o.id::text as ref, sh.cod - due.v as amount from orders o"
        " join sh on sh.order_id = o.id join due on due.order_id = o.id"
        " where o.payment_method = 'cod' and abs(sh.cod - due.v) > 1"
        " and not exists (select 1 from fulfillment_groups g where g.order_id = o.id"
        "                 and not exists (select 1 from shipments s where s.group_id = g.id))",
    # B5: provider-reported payouts not proven by a bank line within 7 days.
    "stale_funds_in_transit":
        "select expected_ref as ref, expected_amount as amount from reconciliation_items "
        "where kind = 'bank_payout' and status = 'open' and expected_at < current_date - 7",
    # Attribution: a ledger line's legal entity must be its portfolio's legal entity.
    "ledger_entity_mismatch":
        "select le.id::text as ref, le.debit - le.credit as amount from ledger_entries le "
        "join portfolios p on p.id = le.portfolio_id join brands b on b.id = p.brand_id "
        "where le.legal_entity_id is not null and b.legal_entity_id is not null "
        "and le.legal_entity_id <> b.legal_entity_id",
    # Round 2 / P0-3: a receivable is never cleared twice.
    "marketplace_receivable_negative":
        "select order_item_id::text as ref, sum(debit - credit) as amount from ledger_entries "
        "where account_code = '1110' and order_item_id is not null group by order_item_id "
        "having sum(debit - credit) < 0",
    "cod_receivable_negative":
        "select order_id::text as ref, sum(debit - credit) as amount from ledger_entries "
        "where account_code = '1100' and order_id is not null group by order_id having sum(debit - credit) < 0",
    # Money in settlement suspense must be under investigation (an open case for the order).
    "settlement_suspense_without_case":
        "select le.order_id::text as ref, sum(le.credit - le.debit) as amount from ledger_entries le "
        "where le.account_code = '2400' and le.order_id is not null group by le.order_id "
        "having sum(le.credit - le.debit) <> 0 and not exists (select 1 from exception_cases c "
        "where c.order_id = le.order_id and c.type in ('marketplace_settlement_mismatch','cod_settlement_mismatch'))",
    # Round 2 / P0-1: legacy expectations whose provider account could not be proven need a human mapping.
    "reconciliation_identity_quarantined":
        "select id::text as ref, expected_amount as amount from reconciliation_items where identity_quarantined",
    # A1: an operation claimed more than an hour ago and never finished needs a human (lookup or uncertain).
    "stale_sent_external_operations":
        "select id::text as ref, 0 as amount from external_operations "
        "where status = 'sent' and coalesce(claimed_at, sent_at) < now() - interval '1 hour'",
}

# Counts a restore must be reconciled against before traffic resumes (docs/18, docs/28; remediation E4).
POST_RESTORE = {
    "outbox_pending": "select count(*) from outbox_messages where status = 'pending'",
    "outbox_dead": "select count(*) from outbox_messages where status = 'dead'",
    "external_operations_sent_unconfirmed": "select count(*) from external_operations where status = 'sent'",
    "external_operations_uncertain": "select count(*) from external_operations where status = 'uncertain'",
    "payouts_awaiting_bank_proof": "select count(*) from reconciliation_items where kind = 'bank_payout' "
                                   "and status = 'open'",
    "legacy_duplicate_postings": "select count(*) from journals where legacy_duplicate",
}


def post_restore_report() -> dict:
    """After a restore: what must be reconciled with providers before workers are re-enabled (sent-but-unconfirmed
    external operations are resolved by lookup or by a human, never re-sent blindly). Plus the invariant run."""
    from app.core.db import worker_session

    with worker_session() as s:
        orgs = [r[0] for r in s.execute(text("select organization_ids()")).all()]
    report: dict = {"organizations": len(orgs), "counts": dict.fromkeys(POST_RESTORE, 0), "violations": {}}
    for org in orgs:
        with worker_session(org) as s:
            for name, sql in POST_RESTORE.items():
                report["counts"][name] += int(s.execute(text(sql)).scalar() or 0)
            for name, rows in run(s, org).items():
                report["violations"][name] = report["violations"].get(name, 0) + len(rows)
    return report


def run(session: Session, org_id) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for name, sql in CHECKS.items():
        rows = session.execute(text(sql)).all()
        if not rows:
            continue
        found[name] = [{"ref": r.ref, "amount": str(r.amount)} for r in rows[:50]]
        case = exceptions.open_case(session, org_id, "invariant_violation", subject_type="invariant",
                                    subject_id=name, context={"count": len(rows), "sample": found[name][:10]})
        case.exposure = sum((abs(Decimal(str(r.amount or 0))) for r in rows), Decimal(0))
    return found


def run_all() -> dict:
    from app.core.db import worker_session

    with worker_session() as s:
        orgs = [r[0] for r in s.execute(text("select organization_ids()")).all()]
    violations = 0
    for org in orgs:
        with worker_session(org) as s:
            violations += sum(len(v) for v in run(s, org).values())
    return {"organizations": len(orgs), "violations": violations}
