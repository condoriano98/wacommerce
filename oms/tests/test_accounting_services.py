"""Acceptance #19 (+ #3, #4): fixtures F1–F13 executed through the real OMS services against PostgreSQL.

Each scenario drives quotes → orders → payments → routing job → shipments/procurement → settlements → refunds, then the
journals it produced are compared with docs/fixtures/journals.yaml: every fixture journal must be produced exactly
(account, debit, credit per journal). Journals outside a fixture's scope (e.g. COGS for fixtures that only show the
payment side) are allowed; receipts/attestations must produce none.
"""

import uuid
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select, text

import app.registry  # noqa: F401
from app.core.db import tenant_session
from app.core.outbox import dispatch_due
from app.models.base import utcnow
from app.models.finance import LedgerEntry
from app.models.fulfillment import FulfillmentGroup, PurchaseTask, Shipment
from app.models.orders import Order, OrderItem, Payment
from app.models.org import BankAccount, RiskPolicy
from app.modules import approvals, fulfillment, orders, payments, procurement, quotes, settlements
from app.modules.finance import posting
from app.modules.quotes import QuoteRequestLine
from tests.conftest import owner_session
from tests.world import D, build_world, principal

FIX = yaml.safe_load((Path(__file__).parents[2] / "docs" / "fixtures" / "journals.yaml").read_text())["fixtures"]


def _norm(lines) -> tuple:
    agg: dict[str, list[Decimal]] = {}
    for acct, dr, cr in lines:
        a = agg.setdefault(str(acct), [Decimal(0), Decimal(0)])
        a[0] += Decimal(str(dr))
        a[1] += Decimal(str(cr))
    return tuple(sorted((k, v[0], v[1]) for k, v in agg.items() if v[0] or v[1]))


def fixture_journals(name: str) -> Counter:
    return Counter(_norm(j["lines"]) for j in FIX[name] if j["lines"])


class Ledger:
    """Snapshot journals before a scenario and diff after it."""

    def __init__(self, t):
        self.t = t
        self.before = self._ids()

    def _ids(self) -> set:
        with owner_session() as s:
            return set(s.scalars(select(LedgerEntry.journal_id).where(LedgerEntry.organization_id == self.t.org_id)))

    def produced(self) -> Counter:
        with owner_session() as s:
            rows = s.execute(select(LedgerEntry.journal_id, LedgerEntry.account_code, LedgerEntry.debit,
                                    LedgerEntry.credit).where(LedgerEntry.organization_id == self.t.org_id)).all()
        by_j: dict = {}
        for jid, a, dr, cr in rows:
            if jid not in self.before:
                by_j.setdefault(jid, []).append((a, dr, cr))
        return Counter(_norm(v) for v in by_j.values())


def assert_fixture(name: str, produced: Counter) -> None:
    missing = fixture_journals(name) - produced
    assert not missing, f"{name}: journals not produced by services: {dict(missing)}\nproduced: {dict(produced)}"


def assert_balanced(t) -> None:
    with owner_session() as s:
        bad = s.execute(text("select journal_id from ledger_entries where organization_id = :o group by journal_id "
                             "having sum(debit) <> sum(credit)"), {"o": t.org_id}).all()
        assert bad == []


def _order(t, w, lines, method="doku_link", by="ai"):
    ai = principal(t, "ai")
    with tenant_session(t.org_id, ai.user_id) as s:
        res = quotes.build_quote(s, platform_account_id=w.wa_account, customer_id=w.customer, conversation_id=None,
                                 request=[QuoteRequestLine(w.sku[c].code, q, D(d)) for c, q, d in lines],
                                 payment_method=method, created_by=by)
        assert res.quote.status == "offered", res.quote.customer_message
        return orders.accept_quote(s, res.quote.id, {"city": "Jakarta"}, actor_type="ai", actor_user_id=ai.user_id).id


def _pay_gateway(t, order_id, fee=None):
    owner = principal(t, "owner")
    with tenant_session(t.org_id, owner.user_id) as s:
        o = s.get(Order, order_id)
        p = payments.create_payment_link(s, o, provider="doku", provider_ref=f"INV-{o.order_number}",
                                         url="https://sandbox/pay", expires_at=utcnow() + timedelta(hours=1))
        ref, amount = p.provider_ref, p.amount
    from app.core.db import webhook_session

    with webhook_session() as s:  # adapter path: signature + status re-check already done
        org = s.execute(text("select organization_id from resolve_payment('doku', :r)"), {"r": ref}).scalar()
        from app.core.db import act_as_system

        act_as_system(s, org)
        assert payments.record_gateway_notification(s, provider="doku", notification_id=f"N-{ref}", provider_ref=ref,
                                                    verified_status="paid", verified_amount=amount, raw={},
                                                    fee=fee) == "captured"
    return ref


def _groups(t, order_id):
    with owner_session() as s:
        return [(g.id, g.route) for g in s.scalars(select(FulfillmentGroup).where(FulfillmentGroup.order_id == order_id))]


def _ship_owned(t, order_id, *, shipping_cost=D("12000"), events=("picked", "delivered"), cod_fee=None):
    packer = principal(t, "packer")
    sh_ids = []
    for gid, route in _groups(t, order_id):
        if route != "OWNED_STOCK_BITESHIP":
            continue
        with tenant_session(t.org_id, packer.user_id) as s:
            g = s.get(FulfillmentGroup, gid)
            sh = fulfillment.create_shipment(s, g, carrier="biteship", external_id=f"BS-{uuid.uuid4().hex[:8]}",
                                             shipping_cost=shipping_cost)
            sh_ids.append(sh.id)
        for i, ev in enumerate(events):
            with tenant_session(t.org_id, packer.user_id) as s:
                sh = s.get(Shipment, sh_ids[-1])
                fulfillment.apply_shipment_event(s, sh, ev, utcnow() + timedelta(minutes=i),
                                                 {"return_cost": "12000"}, cod_fee=cod_fee)
    return sh_ids


def test_F1_prepaid_whatsapp_doku(tenant):
    w = build_world(tenant)
    led = Ledger(tenant)
    oid = _order(tenant, w, [("A1", 2, "0.05")])
    _pay_gateway(tenant, oid, fee=D("1435"))
    dispatch_due()
    _ship_owned(tenant, oid)
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        pid = s.scalars(select(Payment.id).where(Payment.order_id == oid)).one()
        settlements.gateway_disbursement(s, fin, w.wa_account, "DOKU-PAYOUT-1", [(pid, D("203565"))])
    assert_fixture("F1_prepaid_whatsapp_doku", led.produced())
    assert_balanced(tenant)
    with tenant_session(tenant.org_id, fin.user_id) as s:
        o = s.get(Order, oid)
        assert o.status == "delivered"
        # contract liability fully released, gateway cleared
        assert posting._balance(s, "2200", order_id=oid) == 0


def test_F2_cod_revenue_recognised_before_remittance(tenant):
    """Acceptance #3: COD revenue at delivery while the receivable is still open; remittance clears it."""
    w = build_world(tenant)
    led = Ledger(tenant)
    oid = _order(tenant, w, [("F2", 1, "0")], method="cod")
    dispatch_due()
    sh_ids = _ship_owned(tenant, oid, cod_fee=D("4950"))
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        assert posting._balance(s, "4000", order_id=oid) == D("-150000")  # revenue booked
        assert posting._balance(s, "1100", order_id=oid) == D("148050")   # still a receivable, not cash
        assert posting._balance(s, "1000", order_id=oid) == 0
        ext = s.get(Shipment, sh_ids[0]).external_id
        settlements.cod_remittance(s, fin, w.wa_account, "COD-RPT-1", [{"shipment_external_id": ext,
                                                                          "amount_remitted": "148050"}])
        settlements.carrier_withdrawal(s, fin, w.wa_account, "WD-1", D("148050"), D("2500"), tenant.legal_entity_id)
    assert_fixture("F2_cod_biteship", led.produced())
    assert_balanced(tenant)


def _bank_account(t):
    with owner_session() as s:
        ba = BankAccount(organization_id=t.org_id, legal_entity_id=t.legal_entity_id, provider="bank",
                         institution="BCA", account_ref=f"123-{uuid.uuid4().hex[:6]}")
        s.add(ba)
    return ba.id


def _manual_pay(t, oid, *, match: bool):
    ai, fin = principal(t, "ai"), principal(t, "finance")
    with tenant_session(t.org_id, ai.user_id) as s:
        pid = payments.report_manual_transfer(s, s.get(Order, oid), {"receipt": "img://r"}).id
    with tenant_session(t.org_id, fin.user_id) as s:
        payments.attest(s, fin, pid, bank_reference="BCA-777", amount_seen=s.get(Payment, pid).amount)
    if match:
        _match(t, oid)
    return pid


def _match(t, oid):
    fin = principal(t, "finance")
    ba = _bank_account(t)
    with tenant_session(t.org_id, fin.user_id) as s:
        o = s.get(Order, oid)
        payments.import_bank_statement(s, fin, ba, [{"booked_at": utcnow().date(), "amount": o.total,
                                                     "reference": f"TRF {o.order_number}"}], batch="b1")
        assert payments.match_bank_lines(s, fin) == 1


def test_F3a_receipt_and_attestation_post_nothing_bank_match_creates_liability(tenant):
    """Acceptance #4 (manual variant): no ledger effect until the bank line is matched."""
    w = build_world(tenant)
    led = Ledger(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")], method="manual_transfer")
    _manual_pay(tenant, oid, match=False)
    assert sum(led.produced().values()) == 0  # receipt + attestation: no journals at all
    dispatch_due()
    assert _groups(tenant, oid) == []  # default policy: no fulfilment before bank match
    _match(tenant, oid)
    dispatch_due()
    _ship_owned(tenant, oid)
    assert_fixture("F3a_manual_transfer_bank_match_before_delivery", led.produced())
    assert_balanced(tenant)


def _release_policy(t, limit="1000000"):
    with owner_session() as s:
        s.add(RiskPolicy(organization_id=t.org_id, portfolio_id=t.portfolios["A"], key="release_on_attestation_limit",
                         threshold=D(limit), comparator="gt"))


def test_F3b_release_on_attestation_then_bank_match(tenant):
    w = build_world(tenant)
    _release_policy(tenant)
    led = Ledger(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")], method="manual_transfer")
    _manual_pay(tenant, oid, match=False)
    dispatch_due()
    _ship_owned(tenant, oid)
    _match(tenant, oid)
    assert_fixture("F3b_manual_transfer_release_on_attestation", led.produced())
    assert_balanced(tenant)


def test_F4_marketplace_receivable_and_settlement(tenant):
    w = build_world(tenant)
    led = Ledger(tenant)
    from app.models.platforms import PlatformAccount

    payload = {"external_order_id": "SHP-F4", "status": "ready_to_ship",
               "items": [{"external_sku_ref": "SHP-C1", "qty": 2, "unit_price": 120000}], "total": 240000}
    from app.core.db import worker_session

    with worker_session(tenant.org_id) as s:
        acc = s.get(PlatformAccount, w.shopee_seller)
        oid = orders.ingest_marketplace_order(s, acc, payload)[0].id
        orders.ingest_marketplace_order(s, acc, {**payload, "status": "delivered"})
        assert posting._balance(s, "1110", order_id=oid) == D("220800")  # receivable net of expected fee, not cash
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        settlements.marketplace_payout(s, fin, w.shopee_seller, "PAYOUT-1", [
            {"external_order_id": "SHP-F4", "gross": "240000", "fee": "20000", "tax_withheld": "1200", "net": "218800"}])
    assert_fixture("F4_marketplace_shopee", led.produced())
    assert_balanced(tenant)


def _dropship_purchase(t, w, oid):
    proc, pm = principal(t, "procurement_a"), principal(t, "pm_a")
    with owner_session() as s:
        tid = s.scalars(select(PurchaseTask.id).where(PurchaseTask.order_id == oid)).one()
    with tenant_session(t.org_id, proc.user_id) as s:
        task = procurement.observe(s, proc, tid, price=D("90000"), shipping_cost=D("10000"), in_stock=True,
                                   variant_matches=True)
        assert task.status == "awaiting_approval", task.block_reason
        aid = task.approval_id
    with tenant_session(t.org_id, pm.user_id) as s:
        approvals.decide(s, pm, aid, True, "ok")
    with tenant_session(t.org_id, proc.user_id) as s:
        item = s.scalars(select(OrderItem).where(OrderItem.order_id == oid)).one()
        purchase = procurement.record_purchase(s, proc, tid, external_order_id=f"SHOPEE-{uuid.uuid4().hex[:8]}",
                                               amount_paid=D("100000"), lines=[(item.id, 1, D("100000"))],
                                               evidence_ref="media://receipt-1")
        return tid, purchase.id


def test_F5_dropship_supplier_prepayment_to_cogs(tenant):
    w = build_world(tenant)
    led = Ledger(tenant)
    oid = _order(tenant, w, [("A2", 1, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    tid, _ = _dropship_purchase(tenant, w, oid)
    proc = principal(tenant, "procurement_a")
    with tenant_session(tenant.org_id, proc.user_id) as s:
        sh = procurement.supplier_shipped(s, proc, tid, "JNT123")
        fulfillment.apply_shipment_event(s, sh, "delivered", utcnow(), {})
    assert_fixture("F5_dropship_prepaid", led.produced())
    assert_balanced(tenant)


def test_F6_partial_fulfilment_cancel_line_refund(tenant):
    w = build_world(tenant)
    led = Ledger(tenant)
    oid = _order(tenant, w, [("A1", 2, "0.05"), ("F6", 1, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    cs, pm, fin = principal(tenant, "cs"), principal(tenant, "pm_a"), principal(tenant, "finance")
    with tenant_session(tenant.org_id, cs.user_id) as s:
        o = s.get(Order, oid)
        item = next(i for i in orders.items_of(s, oid) if i.line_total == D("110000"))
        assert orders.cancel_item_qty(s, o, item, 1, reason="customer changed mind") == D("110000")
        refund = settlements.request_refund(s, cs, oid, item.id, D("110000"), "line canceled")
    with tenant_session(tenant.org_id, pm.user_id) as s:
        approvals.decide(s, pm, refund.approval_id, True, "ok")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        settlements.approve_refund_obligation(s, fin, refund.id)
        settlements.mark_refund_paid(s, fin, refund.id, "BCA-OUT-1")
    _ship_owned(tenant, oid)
    assert_fixture("F6_partial_fulfilment", led.produced())
    assert_balanced(tenant)
    with tenant_session(tenant.org_id, fin.user_id) as s:
        assert s.get(Order, oid).status == "delivered"


def test_F7_refund_allowance_and_F8_return(tenant):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    _ship_owned(tenant, oid)
    led = Ledger(tenant)
    cs, pm, fin, packer = (principal(tenant, n) for n in ("cs", "pm_a", "finance", "packer"))
    with tenant_session(tenant.org_id, cs.user_id) as s:
        item = orders.items_of(s, oid)[0]
        r1 = settlements.request_refund(s, cs, oid, item.id, D("20000"), "late delivery allowance")
    with tenant_session(tenant.org_id, pm.user_id) as s:
        approvals.decide(s, pm, r1.approval_id, True, "ok")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        settlements.approve_refund_obligation(s, fin, r1.id)
        settlements.mark_refund_paid(s, fin, r1.id, "BCA-OUT-2")
    assert_fixture("F7_refund_allowance", led.produced())
    led8 = Ledger(tenant)
    with tenant_session(tenant.org_id, cs.user_id) as s:
        r2 = settlements.request_refund(s, cs, oid, item.id, D("100000"), "returned")
    with tenant_session(tenant.org_id, pm.user_id) as s:
        approvals.decide(s, pm, r2.approval_id, True, "ok")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        settlements.approve_refund_obligation(s, fin, r2.id)
    with tenant_session(tenant.org_id, packer.user_id) as s:
        settlements.receive_return(s, packer, oid, item.id, 1, resellable=True, warehouse_id=w.warehouse,
                                   return_shipping_cost=D("12000"))
        assert s.get(Order, oid).status == "returned"
    assert_fixture("F8_return_resellable", led8.produced())
    assert_balanced(tenant)


def test_F9_fee_deducted_at_disbursement(tenant):
    w = build_world(tenant)
    led = Ledger(tenant)
    oid = _order(tenant, w, [("A1", 2, "0.05")])
    _pay_gateway(tenant, oid, fee=None)
    dispatch_due()
    _ship_owned(tenant, oid)
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        pid = s.scalars(select(Payment.id).where(Payment.order_id == oid)).one()
        settlements.gateway_disbursement(s, fin, w.wa_account, "DOKU-PAYOUT-9", [(pid, D("203565"))])
    assert_fixture("F9_settlement_fee_deduction", led.produced())


def test_F10_failed_delivery_returns_goods_without_revenue(tenant):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    led = Ledger(tenant)
    _ship_owned(tenant, oid, events=("picked", "returned"))
    assert_fixture("F10_failed_delivery_rts", led.produced())
    with owner_session() as s:
        assert posting._balance(s, "4000", order_id=oid) == 0
        item = s.scalars(select(OrderItem).where(OrderItem.order_id == oid)).one()
        assert item.qty_shipped == 0 and item.qty_delivered == 0


def test_F11_supplier_cancellation(tenant):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A2", 1, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    led = Ledger(tenant)
    _, purchase_id = _dropship_purchase(tenant, w, oid)
    proc = principal(tenant, "procurement_a")
    with tenant_session(tenant.org_id, proc.user_id) as s:
        procurement.supplier_canceled_purchase(s, proc, purchase_id)
        procurement.supplier_refund_received(s, proc, purchase_id)
        item = s.scalars(select(OrderItem).where(OrderItem.order_id == oid)).one()
        assert item.qty_procured == 0  # trigger decremented
    assert_fixture("F11_supplier_cancellation", led.produced())


def test_F12_ad_spend_accrual_and_restatement(tenant):
    led = Ledger(tenant)
    owner = principal(tenant, "owner")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        posting.ad_spend_accrued(s, tenant.org_id, tenant.legal_entity_id, "meta:2026-11-09:A", D("500000"),
                                 tenant.portfolios["A"], None)
        posting.ad_spend_accrued(s, tenant.org_id, tenant.legal_entity_id, "meta:2026-11-09:unalloc", D("120000"),
                                 None, None)
        posting.ad_spend_accrued(s, tenant.org_id, tenant.legal_entity_id, "meta:2026-11-09:A:rev1", D("15000"),
                                 tenant.portfolios["A"], None)
        posting.ad_charge_paid(s, tenant.org_id, tenant.legal_entity_id, "card:INV-1", D("635000"), None)
    assert_fixture("F12_ad_spend_restatement", led.produced())


def test_F13_credit_losses(tenant):
    w = build_world(tenant)
    _release_policy(tenant)
    fin, owner2 = principal(tenant, "finance"), principal(tenant, "owner2")
    # F13a + F13b: allowance, later released when the bank line arrives
    led = Ledger(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")], method="manual_transfer")
    pid = _manual_pay(tenant, oid, match=False)
    dispatch_due()
    _ship_owned(tenant, oid)
    with tenant_session(tenant.org_id, fin.user_id) as s:
        settlements.credit_loss(s, fin, pid, "allowance")
    _match(tenant, oid)
    assert_fixture("F13b_allowance_released_when_matched", led.produced())
    # F13c + F13d: allowance, approved write-off, recovery
    led2 = Ledger(tenant)
    oid2 = _order(tenant, w, [("A1", 1, "0")], method="manual_transfer")
    pid2 = _manual_pay(tenant, oid2, match=False)
    dispatch_due()
    _ship_owned(tenant, oid2)
    with tenant_session(tenant.org_id, fin.user_id) as s:
        settlements.credit_loss(s, fin, pid2, "allowance")
        a = approvals.request_approval(s, fin, kind="ledger_adjustment", subject_type="payment", subject_id=str(pid2),
                                       amount=D("115000"), payload={}, portfolio_id=None,
                                       idempotency_key=f"wo:{pid2}")
    with tenant_session(tenant.org_id, owner2.user_id) as s:
        approvals.decide(s, owner2, a.id, True, "uncollectible")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        settlements.credit_loss(s, fin, pid2, "write_off", approval_id=a.id)
        settlements.credit_loss(s, fin, pid2, "recovery")
    assert_fixture("F13d_recovery_after_write_off", led2.produced())
    assert_balanced(tenant)


@pytest.mark.parametrize("name", ["F13a_allowance_on_unmatched_transfer", "F13c_write_off_with_approval"])
def test_F13_subsets_are_covered_by_full_scenarios(name):
    # F13a ⊂ F13b and F13c ⊂ F13d: their journals are produced by test_F13_credit_losses.
    assert fixture_journals(name) - fixture_journals(
        "F13b_allowance_released_when_matched" if name.startswith("F13a") else "F13d_recovery_after_write_off") == Counter()
