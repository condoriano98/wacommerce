from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

import app.registry  # noqa: F401  (hooks + job handlers)
from app.core.db import system_session, tenant_session
from app.core.outbox import dispatch_due
from app.models.base import utcnow
from app.models.finance import LedgerEntry
from app.models.fulfillment import ProcurementTask, Shipment
from app.models.governance import ExceptionCase
from app.models.inventory import InventoryBalance
from app.models.orders import Order, OrderItem
from app.modules import fulfillment, inventory, orders, payments, quotes
from app.modules.quotes import PolicyDenied, QuoteRequestLine
from tests.world import D, build_world, principal


def _balances(session, order_id=None):
    q = select(LedgerEntry.account_code, func.sum(LedgerEntry.debit - LedgerEntry.credit)).group_by(
        LedgerEntry.account_code)
    if order_id:
        q = q.where(LedgerEntry.order_id == order_id)
    return {a: v for a, v in session.execute(q).all()}


def _quote(s, w, code, qty=1, method="doku_link", discount="0", by="ai"):
    return quotes.build_quote(s, platform_account_id=w.wa_account, customer_id=w.customer, conversation_id=None,
                              request=[QuoteRequestLine(w.sku[code].code, qty, D(discount))], payment_method=method,
                              created_by=by)


def test_owned_stock_prepaid_order_end_to_end(tenant):
    w = build_world(tenant)
    ai = principal(tenant, "ai")
    with tenant_session(tenant.org_id, ai.user_id) as s:
        res = _quote(s, w, "A1", qty=2, discount="0.05")
        assert res.quote.status == "offered"
        assert res.quote.total == D("100000") * 2 * D("0.95") + D("15000")
        assert "Rp205.000" in res.quote.customer_message
        order = orders.accept_quote(s, res.quote.id, {"city": "Jakarta"}, actor_type="ai", actor_user_id=ai.user_id)
        order_id = order.id
        assert order.status == "payment_pending"
        pay = payments.create_payment_link(s, order, provider="doku", provider_ref=f"INV-{order.order_number}",
                                           url="https://sandbox.doku/pay", expires_at=utcnow() + timedelta(hours=2))
        ref = pay.provider_ref
    with system_session(tenant.org_id) as s:  # webhook path (verified + re-checked by adapter)
        assert payments.record_gateway_notification(s, provider="doku", notification_id="N1", provider_ref=ref,
                                                    verified_status="paid", verified_amount=D("205000"), raw={}) == "paid"
        assert payments.record_gateway_notification(s, provider="doku", notification_id="N1", provider_ref=ref,
                                                    verified_status="paid", verified_amount=D("205000"),
                                                    raw={}) == "duplicate"
    dispatch_due()  # routing job
    packer = principal(tenant, "packer")
    with tenant_session(tenant.org_id, packer.user_id) as s:
        o = s.get(Order, order_id)
        assert o.status == "reserved" and o.fulfillment_route == "OWNED_STOCK_BITESHIP"
        fulfillment.start_packing(s, packer, order_id)
        fulfillment.mark_packed(s, packer, order_id, D("2000"))
        sh = Shipment(organization_id=tenant.org_id, order_id=order_id, carrier="biteship", status="confirmed",
                      external_id="BS-1", shipping_cost=D("12000"))
        s.add(sh)
        s.flush()
        now = utcnow()
        fulfillment.apply_shipment_event(s, sh, "picked", now, {})
        fulfillment.apply_shipment_event(s, sh, "picked", now, {})  # duplicate webhook ignored
        fulfillment.apply_shipment_event(s, sh, "delivered", now + timedelta(days=1), {})
    owner = principal(tenant, "owner")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        o = s.get(Order, order_id)
        assert o.status == "delivered"
        b = _balances(s, order_id)
        assert b["1300"] == 0  # advance fully released into revenue
        assert b["4000"] == D("-190000.00") and b["4100"] == D("-15000.00")
        assert b["5000"] == D("120000.00")  # COGS 2 x 60k
        assert b["1010"] == D("205000.00")  # cash still in gateway clearing until disbursement
        bal = s.scalars(select(InventoryBalance).where(InventoryBalance.sku_id == w.sku["A1"].id)).one()
        assert (bal.on_hand, bal.reserved) == (3, 0)
        assert sum(_balances(s).values()) == 0  # whole ledger balances


def test_ai_cannot_exceed_discount_or_sell_unlisted_sku(tenant):
    w = build_world(tenant)
    ai = principal(tenant, "ai")
    with tenant_session(tenant.org_id, ai.user_id) as s:
        with pytest.raises(PolicyDenied) as e:
            _quote(s, w, "A1", discount="0.08")  # listing allows 10%, AI cap is 5%
        assert e.value.code == "discount_exceeds_limit"
    with tenant_session(tenant.org_id, ai.user_id) as s:
        with pytest.raises(PolicyDenied) as e:
            _quote(s, w, "C1")  # C1 is listed only on Shopee, not on the WhatsApp channel
        assert e.value.code in ("not_listed_on_channel", "unknown_sku")


def test_no_overselling_quotes_hold_stock(tenant):
    w = build_world(tenant, stock_a1=1)
    ai = principal(tenant, "ai")
    with tenant_session(tenant.org_id, ai.user_id) as s:
        first = _quote(s, w, "A1")
        second = _quote(s, w, "A1")
        assert first.quote.status == "offered"
        assert second.quote.status == "pending_availability"  # no promise without stock
        assert "mengonfirmasi" in second.quote.customer_message
    owner = principal(tenant, "owner")
    with pytest.raises(IntegrityError):
        with tenant_session(tenant.org_id, owner.user_id) as s:
            s.execute(text("update inventory_balances set reserved = on_hand + 1 where sku_id = :s"),
                      {"s": w.sku["A1"].id})


def test_expired_quote_cannot_be_accepted(tenant):
    w = build_world(tenant)
    ai = principal(tenant, "ai")
    with tenant_session(tenant.org_id, ai.user_id) as s:
        res = _quote(s, w, "A1")
        res.quote.valid_until = utcnow() - timedelta(minutes=1)
        qid = res.quote.id
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        with tenant_session(tenant.org_id, ai.user_id) as s:
            orders.accept_quote(s, qid, {}, actor_type="ai")


def test_payment_mismatch_opens_case(tenant):
    w = build_world(tenant)
    ai = principal(tenant, "ai")
    with tenant_session(tenant.org_id, ai.user_id) as s:
        order = orders.accept_quote(s, _quote(s, w, "A1").quote.id, {}, actor_type="ai")
        pay = payments.submit_manual_transfer(s, order, {"receipt": "img://1"})
        pay_id, order_id = pay.id, order.id
    # The AI (service principal) cannot confirm payments.
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        with tenant_session(tenant.org_id, ai.user_id) as s:
            payments.verify_manual_transfer(s, ai, pay_id, D("115000"), "BCA-1")
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        assert payments.verify_manual_transfer(s, fin, pay_id, D("100000"), "BCA-1") == "mismatch"
        assert s.get(Order, order_id).status == "exception"
        case = s.scalars(select(ExceptionCase).where(ExceptionCase.order_id == order_id)).one()
        assert case.type == "payment_mismatch" and case.owner_role == "finance_manager"


def test_dropship_routing_creates_procurement_tasks(tenant):
    w = build_world(tenant)
    ai = principal(tenant, "ai")
    with tenant_session(tenant.org_id, ai.user_id) as s:
        res = _quote(s, w, "A2")
        assert res.lines[0].availability_source == "dropship"
        order = orders.accept_quote(s, res.quote.id, {"city": "Bandung"}, actor_type="ai")
        pay = payments.submit_manual_transfer(s, order, {})
        pay_id, order_id = pay.id, order.id
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        assert payments.verify_manual_transfer(s, fin, pay_id, D("165000"), "BCA-2") == "paid"
    dispatch_due()
    with tenant_session(tenant.org_id, fin.user_id) as s:
        o = s.get(Order, order_id)
        assert o.fulfillment_route == "SHOPEE_DROPSHIP" and o.status == "procurement_pending"
        assert s.scalars(select(ProcurementTask).where(ProcurementTask.order_id == order_id)).one().marketplace == "shopee"


def test_stale_supplier_offer_is_not_promised(tenant):
    w = build_world(tenant, offer_fresh=False)
    ai = principal(tenant, "ai")
    with tenant_session(tenant.org_id, ai.user_id) as s:
        res = _quote(s, w, "A2")
        assert res.quote.status == "pending_availability"


def test_marketplace_order_ingest_is_idempotent_and_shares_stock(tenant):
    w = build_world(tenant)
    from app.models.platforms import PlatformAccount

    payload = {"external_order_id": "SHP-ORDER-1", "status": "ready_to_ship",
               "items": [{"external_sku_ref": "SHP-C1", "qty": 2, "unit_price": 120000}], "total": 240000}
    with system_session(tenant.org_id) as s:
        acc = s.get(PlatformAccount, w.shopee_seller)
        o1, created1 = orders.ingest_marketplace_order(s, acc, payload)
        o2, created2 = orders.ingest_marketplace_order(s, acc, payload)
        assert created1 and not created2 and o1.id == o2.id
        assert o1.status == "platform_fulfilling"
        # A second order for the same SKU exceeds the 2 units of shared stock -> oversold case.
        o3, _ = orders.ingest_marketplace_order(s, acc, {**payload, "external_order_id": "SHP-ORDER-2",
                                                         "items": [{"external_sku_ref": "SHP-C1", "qty": 1,
                                                                    "unit_price": 120000}]})
        assert s.scalars(select(ExceptionCase).where(ExceptionCase.order_id == o3.id)).one().type == "oversold_marketplace"
        orders.ingest_marketplace_order(s, acc, {**payload, "status": "delivered"})
        b = _balances(s, o1.id)
        assert b["1110"] == D("240000.00")  # receivable, not cash
        assert "1000" not in b


def test_portfolio_b_cannot_see_portfolio_a_orders(tenant):
    w = build_world(tenant)
    ai = principal(tenant, "ai")
    with tenant_session(tenant.org_id, ai.user_id) as s:
        order_id = orders.accept_quote(s, _quote(s, w, "A1").quote.id, {}, actor_type="ai").id
    pm_b = principal(tenant, "pm_b")
    with tenant_session(tenant.org_id, pm_b.user_id) as s:
        assert s.get(Order, order_id) is None
        assert s.scalars(select(OrderItem).where(OrderItem.order_id == order_id)).all() == []
    pm_a = principal(tenant, "pm_a")
    with tenant_session(tenant.org_id, pm_a.user_id) as s:
        assert s.get(Order, order_id) is not None


def test_inventory_transfer_between_portfolios_nets_to_zero(tenant):
    w = build_world(tenant)
    owner = principal(tenant, "owner")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        inventory.transfer_between_portfolios(s, tenant.org_id, from_portfolio=tenant.portfolios["A"],
                                              to_portfolio=tenant.portfolios["B"], sku_id=w.sku["A1"].id,
                                              warehouse_id=w.warehouse, qty=2, reference="T-1")
        b = _balances(s)
        assert b["3100"] == 0
        inv_b = s.execute(select(func.sum(LedgerEntry.debit - LedgerEntry.credit)).where(
            LedgerEntry.account_code == "1200", LedgerEntry.portfolio_id == tenant.portfolios["B"])).scalar()
        assert inv_b == D("400000.00") + D("120000.00")  # 10 x 40k own stock + 2 x 60k transferred
