"""Commerce core behaviour under V3.1 (quotes/AI authority, holds, routing groups, marketplace ingest, transfers).
End-to-end accounting is covered by test_accounting_services.py (acceptance #19)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

import app.registry  # noqa: F401
from app.core.db import tenant_session, worker_session
from app.core.outbox import dispatch_due
from app.models.base import utcnow
from app.models.finance import LedgerEntry
from app.models.fulfillment import FulfillmentGroup, PurchaseTask
from app.models.governance import ExceptionCase
from app.models.orders import Order, OrderItem, Payment
from app.models.platforms import PlatformAccount
from app.modules import inventory, orders, payments, quotes
from app.modules.quotes import PolicyDenied, QuoteRequestLine
from tests.world import D, build_world, principal


def _quote(s, w, code, qty=1, method="doku_link", discount="0", by="ai"):
    return quotes.build_quote(s, platform_account_id=w.wa_account, customer_id=w.customer, conversation_id=None,
                              request=[QuoteRequestLine(w.sku[code].code, qty, D(discount))], payment_method=method,
                              created_by=by)


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
        assert _quote(s, w, "A1").quote.status == "offered"
        second = _quote(s, w, "A1")
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
    with pytest.raises(HTTPException):
        with tenant_session(tenant.org_id, ai.user_id) as s:
            orders.accept_quote(s, qid, {}, actor_type="ai")


def test_attestation_amount_mismatch_opens_case_and_ai_cannot_attest(tenant):
    w = build_world(tenant)
    ai = principal(tenant, "ai")
    with tenant_session(tenant.org_id, ai.user_id) as s:
        order = orders.accept_quote(s, _quote(s, w, "A1", method="manual_transfer").quote.id, {}, actor_type="ai")
        pid, oid = payments.report_manual_transfer(s, order, {"receipt": "img://1"}).id, order.id
    with pytest.raises(HTTPException):
        with tenant_session(tenant.org_id, ai.user_id) as s:
            payments.attest(s, ai, pid, bank_reference="X", amount_seen=D("115000"))
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        assert payments.attest(s, fin, pid, bank_reference="BCA-1", amount_seen=D("100000")) == "mismatch"
        assert s.get(Order, oid).status == "exception"
        assert s.get(Payment, pid).status == "reported"
        case = s.scalars(select(ExceptionCase).where(ExceptionCase.order_id == oid)).one()
        assert case.type == "payment_mismatch" and case.owner_role == "finance_manager"


def test_dropship_routing_creates_group_and_purchase_task(tenant):
    w = build_world(tenant)
    ai = principal(tenant, "ai")
    with tenant_session(tenant.org_id, ai.user_id) as s:
        res = _quote(s, w, "A2")
        assert res.lines[0].availability_source == "dropship"
        order = orders.accept_quote(s, res.quote.id, {"city": "Bandung"}, actor_type="ai")
        p = payments.create_payment_link(s, order, provider="doku", provider_ref=f"INV-{order.id}", url="u",
                                         expires_at=utcnow() + timedelta(hours=1))
        ref, oid = p.provider_ref, order.id
    with worker_session(tenant.org_id) as s:
        payments.record_gateway_notification(s, provider="doku", notification_id="N1", provider_ref=ref,
                                             verified_status="paid", verified_amount=D("165000"), raw={})
    dispatch_due()
    with worker_session(tenant.org_id) as s:
        g = s.scalars(select(FulfillmentGroup).where(FulfillmentGroup.order_id == oid)).one()
        assert g.route == "SHOPEE_DROPSHIP" and g.status == "purchase_pending"
        assert s.scalars(select(PurchaseTask).where(PurchaseTask.group_id == g.id)).one().marketplace == "shopee"
        item = s.scalars(select(OrderItem).where(OrderItem.order_id == oid)).one()
        assert (item.qty_allocated_dropship, item.qty_procured) == (1, 0)


def test_stale_supplier_offer_is_not_promised(tenant):
    w = build_world(tenant, offer_fresh=False)
    ai = principal(tenant, "ai")
    with tenant_session(tenant.org_id, ai.user_id) as s:
        assert _quote(s, w, "A2").quote.status == "pending_availability"


def test_marketplace_order_ingest_is_idempotent_and_shares_stock(tenant):
    w = build_world(tenant)
    payload = {"external_order_id": "SHP-ORDER-1", "status": "ready_to_ship",
               "items": [{"external_sku_ref": "SHP-C1", "qty": 2, "unit_price": 120000}], "total": 240000}
    with worker_session(tenant.org_id) as s:
        acc = s.get(PlatformAccount, w.shopee_seller)
        o1, created1 = orders.ingest_marketplace_order(s, acc, payload)
        o2, created2 = orders.ingest_marketplace_order(s, acc, payload)
        assert created1 and not created2 and o1.id == o2.id
        # 2 units of shared stock are reserved; a second order for 1 more unit is oversold -> case
        o3, _ = orders.ingest_marketplace_order(s, acc, {**payload, "external_order_id": "SHP-ORDER-2",
                                                         "items": [{"external_sku_ref": "SHP-C1", "qty": 1,
                                                                    "unit_price": 120000}]})
        assert s.scalars(select(ExceptionCase).where(ExceptionCase.order_id == o3.id)).one().type == "oversold_marketplace"
        orders.ingest_marketplace_order(s, acc, {**payload, "status": "delivered"})
        bal = s.execute(select(LedgerEntry.account_code, func.sum(LedgerEntry.debit - LedgerEntry.credit)).where(
            LedgerEntry.order_id == o1.id).group_by(LedgerEntry.account_code)).all()
        b = dict(bal)
        assert b["1110"] == D("220800.00")  # receivable net of the expected 8% fee, not cash
        assert "1000" not in b


def test_portfolio_b_cannot_see_portfolio_a_orders(tenant):
    w = build_world(tenant)
    ai = principal(tenant, "ai")
    with tenant_session(tenant.org_id, ai.user_id) as s:
        order_id = orders.accept_quote(s, _quote(s, w, "A1").quote.id, {}, actor_type="ai").id
    with tenant_session(tenant.org_id, tenant.users["pm_b"]) as s:
        assert s.get(Order, order_id) is None
        assert s.scalars(select(OrderItem).where(OrderItem.order_id == order_id)).all() == []
    with tenant_session(tenant.org_id, tenant.users["pm_a"]) as s:
        assert s.get(Order, order_id) is not None


def test_inventory_transfer_between_portfolios_nets_to_zero(tenant):
    w = build_world(tenant)
    owner = principal(tenant, "owner")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        inventory.transfer_between_portfolios(s, tenant.org_id, from_portfolio=tenant.portfolios["A"],
                                              to_portfolio=tenant.portfolios["B"], sku_id=w.sku["A1"].id,
                                              warehouse_id=w.warehouse, qty=2, reference="T-1")
        rows = dict(s.execute(select(LedgerEntry.portfolio_id, func.sum(LedgerEntry.debit - LedgerEntry.credit)).where(
            LedgerEntry.source_type == "inventory_transfer").group_by(LedgerEntry.portfolio_id)).all())
        assert rows[tenant.portfolios["B"]] == D("120000.00")
        assert rows[tenant.portfolios["A"]] == D("-120000.00")
        assert sum(rows.values(), Decimal(0)) == 0
