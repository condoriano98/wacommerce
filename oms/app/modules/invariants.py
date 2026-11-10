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
}


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
