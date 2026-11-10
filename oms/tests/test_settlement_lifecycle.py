"""Round 2 / P0-3 — settlement state reflects actual reconciliation, and no receivable is ever cleared twice.

Order: one Shopee item C1 (Rp120,000; 8% expected fee accrued = 9,600 → marketplace receivable 1110 = 110,400).
Payout lines may carry ``final`` (default true: Shopee escrow releases an order once; docs/12).
"""

import uuid
from decimal import Decimal as D

from sqlalchemy import func, select, text

import app.registry  # noqa: F401
from app.core.db import tenant_session, worker_session
from app.models.finance import LedgerEntry, Settlement
from app.models.orders import Order
from app.models.platforms import PlatformAccount
from app.modules import approvals, orders, settlements
from tests.conftest import owner_session
from tests.test_remediation_concurrency import _run_parallel
from tests.test_v31_correctness_remediation import _gl, _multi_portfolio_marketplace_order
from tests.world import build_world, principal


def _order_c1(t, w, ext: str) -> uuid.UUID:
    payload = {"external_order_id": ext, "status": "ready_to_ship", "total": 120000,
               "items": [{"external_sku_ref": "SHP-C1", "qty": 1, "unit_price": 120000}]}
    with worker_session(t.org_id) as s:
        acc = s.get(PlatformAccount, w.shopee_seller)
        oid = orders.ingest_marketplace_order(s, acc, payload)[0].id
        orders.ingest_marketplace_order(s, acc, {**payload, "status": "delivered"})
    return oid


def _payout(t, w, payout_id: str, lines: list[dict]):
    fin = principal(t, "finance")
    with tenant_session(t.org_id, fin.user_id) as s:
        return settlements.marketplace_payout(s, fin, w.shopee_seller, payout_id, lines).id


def _line(ext, gross, fee, net, tax=0, **kw) -> dict:
    return {"external_order_id": ext, "gross": str(gross), "fee": str(fee), "tax_withheld": str(tax),
            "net": str(net), **kw}


def _state(oid) -> str:
    with owner_session() as s:
        return s.get(Order, oid).settlement_status


def _cases(t, kind="marketplace_settlement_mismatch") -> int:
    with owner_session() as s:
        return s.execute(text("select count(*) from exception_cases where organization_id = :o and type = :k"),
                         {"o": t.org_id, "k": kind}).scalar()


def _settled_journals(t) -> int:
    with owner_session() as s:
        return s.scalar(select(func.count(func.distinct(LedgerEntry.journal_id))).where(
            LedgerEntry.organization_id == t.org_id, LedgerEntry.source_type == "marketplace_settled"))


def test_exact_payout_is_matched(tenant):
    w = build_world(tenant)
    oid = _order_c1(tenant, w, "EX-1")
    _payout(tenant, w, "P-EX-1", [_line("EX-1", 120000, 9600, 110400)])
    assert _gl("1110", order_id=oid) == 0 and _state(oid) == "matched" and _cases(tenant) == 0


def test_short_final_payout_is_a_variance_with_the_shortfall_still_receivable(tenant):
    w = build_world(tenant)
    oid = _order_c1(tenant, w, "SH-1")
    _payout(tenant, w, "P-SH-1", [_line("SH-1", 120000, 9600, 100000)])
    assert _gl("1110", order_id=oid) == D("10400")  # never cleared beyond what was paid
    assert _state(oid) == "variance" and _cases(tenant) == 1


def test_excess_payout_goes_to_suspense_never_below_zero_receivable(tenant):
    w = build_world(tenant)
    oid = _order_c1(tenant, w, "XS-1")
    _payout(tenant, w, "P-XS-1", [_line("XS-1", 120000, 9600, 130000)])
    assert _gl("1110", order_id=oid) == 0
    assert _gl("2400", order_id=oid) == D("-19600")
    assert _state(oid) == "variance" and _cases(tenant) >= 1


def test_replayed_payout_creates_nothing_and_altered_replay_is_flagged(tenant):
    w = build_world(tenant)
    oid = _order_c1(tenant, w, "RP-1")
    lines = [_line("RP-1", 120000, 9600, 110400)]
    first = _payout(tenant, w, "P-RP-1", lines)
    assert _payout(tenant, w, "P-RP-1", lines) == first
    assert _settled_journals(tenant) == 1 and _cases(tenant) == 0
    _payout(tenant, w, "P-RP-1", [_line("RP-1", 120000, 9600, 99999)])  # same id, different content
    assert _settled_journals(tenant) == 1 and _cases(tenant) == 1
    with owner_session() as s:
        assert s.scalar(select(func.count()).select_from(Settlement).where(
            Settlement.organization_id == tenant.org_id)) == 1
    assert _gl("1110", order_id=oid) == 0


def test_a_second_payout_id_for_an_already_cleared_order_never_clears_it_twice(tenant):
    w = build_world(tenant)
    oid = _order_c1(tenant, w, "DUP-1")
    _payout(tenant, w, "P-DUP-A", [_line("DUP-1", 120000, 9600, 110400)])
    _payout(tenant, w, "P-DUP-B", [_line("DUP-1", 120000, 9600, 110400)])
    assert _gl("1110", order_id=oid) == 0
    assert _gl("2400", order_id=oid) == D("-110400")  # held in suspense for investigation
    assert _gl("5300", order_id=oid) == D("9600")  # fees not booked twice
    assert _state(oid) == "variance" and _cases(tenant) >= 1


def test_two_partial_payouts_reconcile_cumulatively(tenant):
    w = build_world(tenant)
    oid = _order_c1(tenant, w, "PT-1")
    _payout(tenant, w, "P-PT-1", [_line("PT-1", 60000, 4800, 55200, final=False)])
    assert _gl("1110", order_id=oid) == D("55200") and _state(oid) == "partially_settled"
    _payout(tenant, w, "P-PT-2", [_line("PT-1", 60000, 4800, 55200, final=True)])
    assert _gl("1110", order_id=oid) == 0 and _gl("5300", order_id=oid) == D("9600")
    assert _state(oid) == "matched" and _cases(tenant) == 0
    with owner_session() as s:
        item = s.execute(text("select status, actual_amount from reconciliation_items where kind = 'marketplace' "
                              "and expected_ref = :r"), {"r": str(oid)}).one()
    assert (item.status, D(item.actual_amount)) == ("matched", D("110400"))


def test_fee_true_up_is_booked_and_stays_visible(tenant):
    w = build_world(tenant)
    oid = _order_c1(tenant, w, "FT-1")
    _payout(tenant, w, "P-FT-1", [_line("FT-1", 120000, 10000, 110000)])
    assert _gl("1110", order_id=oid) == 0 and _gl("5300", order_id=oid) == D("10000")
    assert _state(oid) == "variance" and _cases(tenant) == 1


def test_multi_portfolio_payout_reconciles_each_portfolio(tenant):
    w = build_world(tenant)
    oid = _multi_portfolio_marketplace_order(tenant, w, "MP-R2")
    _payout(tenant, w, "P-MP-R2", [_line("MP-R2", 200000, 16000, 184000)])
    for code in ("B", "C"):
        assert _gl("1110", order_id=oid, portfolio_id=tenant.portfolios[code]) == 0
    assert _state(oid) == "matched"


def test_concurrent_payouts_for_one_order_never_over_clear(tenant):
    w = build_world(tenant)
    oid = _order_c1(tenant, w, "CC-1")
    lines = [_line("CC-1", 120000, 9600, 110400)]
    _, errors = _run_parallel([lambda: _payout(tenant, w, "P-CC-A", lines),
                               lambda: _payout(tenant, w, "P-CC-B", lines)])
    assert errors == [None, None], errors
    assert _gl("1110", order_id=oid) == 0  # cleared exactly once
    assert _gl("2400", order_id=oid) == D("-110400")  # the other payout is held, not applied


def test_variance_accepted_with_approval_is_adjusted(tenant):
    w = build_world(tenant)
    oid = _order_c1(tenant, w, "ADJ-1")
    _payout(tenant, w, "P-ADJ-1", [_line("ADJ-1", 120000, 9600, 100000)])
    fin, owner = principal(tenant, "finance"), principal(tenant, "owner")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        aid = approvals.request_approval(s, fin, kind="variance_acceptance", subject_type="order",
                                         subject_id=str(oid), amount=D("10400"), payload={}, portfolio_id=None,
                                         idempotency_key=f"adj-{oid}").id
    with tenant_session(tenant.org_id, owner.user_id) as s:
        approvals.decide(s, owner, aid, True, "accepted short payout")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        settlements.accept_settlement_variance(s, fin, oid, aid)
    assert _gl("1110", order_id=oid) == 0 and _gl("5300", order_id=oid) == D("9600") + D("10400")
    assert _state(oid) in ("adjusted", "closed")


def test_cod_remittance_reported_twice_under_new_ids_never_clears_the_receivable_twice(tenant):
    from app.core.outbox import dispatch_due
    from app.models.fulfillment import Shipment
    from tests.test_accounting_services import _order, _ship_owned

    w = build_world(tenant)
    oid = _order(tenant, w, [("F2", 1, "0")], method="cod")
    dispatch_due()
    sh_ids = _ship_owned(tenant, oid, cod_fee=D("4950"))
    assert _gl("1100", order_id=oid) == D("148050")
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        ext = s.get(Shipment, sh_ids[0]).external_id
        row = [{"shipment_external_id": ext, "amount_remitted": "148050"}]
        settlements.cod_remittance(s, fin, w.wa_account, "COD-R1", row)
        settlements.cod_remittance(s, fin, w.wa_account, "COD-R2", row)  # same money reported again
    assert _gl("1100", order_id=oid) == 0  # cleared once
    assert _gl("2400", order_id=oid) == D("-148050")  # the duplicate is held for investigation
    assert _cases(tenant, "cod_settlement_mismatch") >= 1
