"""M2 gate: WhatsApp → AI quote → acceptance → DOKU (mock) capture → mixed-group routing → Biteship (mock) booking and
carrier events + human dropship purchase → delivery → DOKU disbursement → reconciliation → ledger.

Every provider is mocked. All webhooks (Meta, DOKU, Biteship) are then delivered AGAIN — duplicated and out of order —
and every job re-run: nothing changes (no duplicate WhatsApp sends, payment pages, bookings or journals). Covers the
DOKU/Biteship part of acceptance #5."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

import app.registry  # noqa: F401
from app.core.config import get_settings
from app.core.db import tenant_session
from app.core.outbox import dispatch_due
from app.integrations import mockstore
from app.integrations.biteship import MockBiteship
from app.integrations.doku import MockDoku, signature
from app.integrations.whatsapp import MockWhatsAppSender
from app.models.base import utcnow
from app.models.finance import LedgerEntry
from app.models.fulfillment import FulfillmentGroup, PurchaseTask, Shipment
from app.models.messaging import WaEvent
from app.models.orders import Order, OrderItem, Payment
from app.modules import approvals, fulfillment, procurement, settlements
from app.modules.finance import posting
from tests.conftest import owner_session
from tests.test_accounting_services import assert_balanced
from tests.test_m2_agent import _customer_says, tool
from tests.test_m2_whatsapp import _conv, _pnid
from tests.world import build_world, principal


@pytest.fixture(autouse=True)
def _fresh():
    mockstore.reset()
    yield
    mockstore.reset()


def doku_notify(client, invoice, amount, status_, request_id):
    body = json.dumps({"order": {"invoice_number": invoice, "amount": int(amount)},
                       "transaction": {"status": status_}}).encode()
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = get_settings()
    sig = signature("MCH-1", request_id, ts, s.doku_notification_path, s.doku_notification_secret, body)
    return client.post("/webhooks/doku", content=body, headers={"Client-Id": "MCH-1", "Request-Id": request_id,
                                                                "Request-Timestamp": ts, "Signature": sig})


def biteship_notify(client, order_id, status_):
    return client.post(f"/webhooks/biteship?token={get_settings().biteship_webhook_token}", content=json.dumps({"event": "order.status", "order_id": order_id,
                                                                 "status": status_}).encode())


def _snapshot(t, pnid) -> dict:
    with owner_session() as s:
        journals = s.execute(select(LedgerEntry.source_type, LedgerEntry.source_id, LedgerEntry.account_code,
                                    func.sum(LedgerEntry.debit), func.sum(LedgerEntry.credit))
                             .where(LedgerEntry.organization_id == t.org_id)
                             .group_by(LedgerEntry.source_type, LedgerEntry.source_id, LedgerEntry.account_code)).all()
        n_lines = s.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.organization_id == t.org_id))
    with mockstore.state("doku") as d, mockstore.state("biteship") as b:
        doku_calls, bookings = d.get("create_calls", 0), len(b.get("orders", {}))
    return {"journals": sorted(map(tuple, journals)), "lines": n_lines, "doku_pages": doku_calls,
            "bookings": bookings, "wa_sends": len([m for m in MockWhatsAppSender().sent if m["pnid"] == pnid])}


def test_m2_end_to_end_whatsapp_order_to_ledger_with_duplicate_and_out_of_order_events(tenant, client):
    w = build_world(tenant)
    pnid = _pnid(w)

    # 1. Inquiry → AI quote (owned A1 + dropship A2 → mixed groups) → verbatim reply.
    _customer_says(client, w, "ID.e2e", "kak mau A1 sama A2")
    conv = _conv(w, "ID.e2e")
    q = tool(client, tenant, conv.id, "quote.create", {"lines": [{"sku_code": w.sku["A1"].code, "qty": 1},
                                                                {"sku_code": w.sku["A2"].code, "qty": 1}],
                                                      "payment_method": "doku_link"}).json()
    assert q["status"] == "offered", q
    assert tool(client, tenant, conv.id, "conversation.reply", {"text": q["customer_message"]}).status_code == 200

    # 2. Acceptance evidence → order → DOKU link (worker creates the payment page once).
    accept = _customer_says(client, w, "ID.e2e", "SETUJU")
    order = tool(client, tenant, conv.id, "order.confirm", {"quote_id": q["quote_id"], "evidence_message_id": str(
        accept), "shipping_address": {"city": "Bandung", "destination_postal_code": 40111}}).json()
    oid = order["order_id"]
    link = tool(client, tenant, conv.id, "payment_link.create", {"order_id": oid}).json()
    dispatch_due()
    invoice = link["invoice_number"]
    with owner_session() as s:
        p = s.scalars(select(Payment).where(Payment.order_id == oid)).one()
        pid, amount = p.id, p.amount
        assert p.payment_url

    # 3. Customer pays. DOKU notifies twice (retry) and then a stale FAILED arrives out of order.
    MockDoku.mark_paid(invoice)
    for rid, st in (("N-1", "SUCCESS"), ("N-1", "SUCCESS"), ("N-2", "FAILED")):
        assert doku_notify(client, invoice, amount, st, rid).status_code == 200
    dispatch_due()  # verify (status re-check) → capture → routing
    dispatch_due()
    with owner_session() as s:
        assert s.get(Order, oid).payment_status == "captured"
        groups = {g.route: g.id for g in s.scalars(select(FulfillmentGroup).where(FulfillmentGroup.order_id == oid))}
    assert set(groups) == {"OWNED_STOCK_BITESHIP", "SHOPEE_DROPSHIP"}

    # 4a. Owned group: pack → approved paid booking → Biteship (mock) booking.
    packer, owner = principal(tenant, "packer"), principal(tenant, "owner")
    gid = groups["OWNED_STOCK_BITESHIP"]
    with tenant_session(tenant.org_id, packer.user_id) as s:
        fulfillment.start_packing(s, packer, gid)
    with tenant_session(tenant.org_id, packer.user_id) as s:
        fulfillment.mark_packed(s, packer, gid, Decimal("2000"))
        aid = approvals.request_approval(s, packer, kind="paid_shipment_booking", subject_type="fulfillment_group",
                                         subject_id=str(gid), amount=None, payload={}, portfolio_id=None,
                                         idempotency_key=f"book-{gid}").id
    with tenant_session(tenant.org_id, owner.user_id) as s:
        approvals.decide(s, owner, aid, True, "ok")
    with tenant_session(tenant.org_id, packer.user_id) as s:
        fulfillment.request_booking(s, packer, gid, aid)
    dispatch_due()
    with owner_session() as s:
        sh = s.scalars(select(Shipment).where(Shipment.group_id == gid)).one()
        bs_order = sh.external_id

    # Courier progress at the provider; callbacks arrive duplicated and out of order (delivered before picked).
    t0 = datetime.now(UTC)
    MockBiteship.set_status(bs_order, "picked", (t0 + timedelta(minutes=1)).isoformat())
    MockBiteship.set_status(bs_order, "delivered", (t0 + timedelta(minutes=5)).isoformat())
    for st in ("delivered", "picked", "delivered"):
        assert biteship_notify(client, bs_order, st).status_code == 200
    dispatch_due()

    # 4b. Dropship group: human purchase (copilot) → supplier ships → delivered.
    proc, pm = principal(tenant, "procurement_a"), principal(tenant, "pm_a")
    with owner_session() as s:
        tid = s.scalars(select(PurchaseTask.id).where(PurchaseTask.order_id == oid)).one()
        a2_item = s.scalars(select(OrderItem.id).where(OrderItem.order_id == oid,
                                                       OrderItem.sku_id == w.sku["A2"].id)).one()
    with tenant_session(tenant.org_id, proc.user_id) as s:
        task = procurement.observe(s, proc, tid, price=Decimal("90000"), shipping_cost=Decimal("10000"),
                                   in_stock=True, variant_matches=True)
        paid_aid = task.approval_id
    with tenant_session(tenant.org_id, pm.user_id) as s:
        approvals.decide(s, pm, paid_aid, True, "ok")
    with tenant_session(tenant.org_id, proc.user_id) as s:
        procurement.record_purchase(s, proc, tid, external_order_id="SHOPEE-E2E-1", amount_paid=Decimal("100000"),
                                    lines=[(a2_item, 1, Decimal("100000"))], evidence_ref="media://receipt-e2e")
    with tenant_session(tenant.org_id, proc.user_id) as s:
        dsh = procurement.supplier_shipped(s, proc, tid, "JNT-E2E")
        fulfillment.apply_shipment_event(s, dsh, "delivered", utcnow(), {})

    # 5. DOKU disbursement → reconciliation.
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        settlements.gateway_disbursement(s, fin, w.wa_account, f"DOKU-PAYOUT-{invoice}", [(pid, amount)])

    with owner_session() as s:
        o = s.get(Order, oid)
        assert o.status == "delivered"
        assert posting._balance(s, "2200", order_id=o.id) == 0  # contract liability fully released
        assert posting._balance(s, "1250", order_id=o.id) == 0  # nothing left in transit
        # gateway receivable cleared (disbursements are settlement-level journals, not per order)
        assert posting._balance(s, "1010", organization_id=tenant.org_id) == 0
        assert posting._balance(s, "4000", order_id=o.id) + posting._balance(s, "4100", order_id=o.id) == -amount
    assert_balanced(tenant)
    first = _snapshot(tenant, pnid)
    assert (first["doku_pages"], first["bookings"]) == (1, 1)
    assert first["wa_sends"] == 1  # the quote reply

    # 6. Run it all again: every webhook re-delivered (and replayed), every job re-dispatched.
    with owner_session() as s:
        ev_ids = s.scalars(select(WaEvent.id).where(WaEvent.organization_id == tenant.org_id)).all()
    for ev in ev_ids:
        assert client.post(f"/wa-events/{ev}/replay", headers=tenant.headers("owner")).status_code == 202
    for rid, st in (("N-1", "SUCCESS"), ("N-3", "SUCCESS"), ("N-2", "FAILED")):
        doku_notify(client, invoice, amount, st, rid)
    for st in ("picked", "delivered", "returned"):
        biteship_notify(client, bs_order, st)
    dispatch_due()
    dispatch_due(now=utcnow() + timedelta(hours=3))
    with owner_session() as s:
        with_raw = s.execute(text("select count(*) from inbound_webhooks where organization_id = :o"),
                             {"o": tenant.org_id}).scalar()
    assert with_raw >= 9  # every callback persisted as evidence
    assert _snapshot(tenant, pnid) == first


def test_doku_callback_with_bad_signature_is_rejected(tenant, client):
    body = json.dumps({"order": {"invoice_number": "INV-X", "amount": 1}, "transaction": {"status": "SUCCESS"}})
    r = client.post("/webhooks/doku", content=body, headers={"Client-Id": "MCH-1", "Request-Id": "N",
                                                             "Request-Timestamp": "T", "Signature": "HMACSHA256=bad"})
    assert r.status_code == 401
