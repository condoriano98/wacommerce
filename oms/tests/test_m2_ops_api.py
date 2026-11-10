"""Operations API: dead-letter review/requeue, human resolution of uncertain external operations (ADR-015),
procurement copilot endpoints. Portfolio users cannot review org-wide operations."""

from datetime import timedelta

import pytest
from sqlalchemy import select

import app.registry  # noqa: F401
from app.core.db import tenant_session
from app.core.outbox import HANDLERS, dispatch_due, enqueue
from app.integrations import mockstore
from app.integrations.biteship import MockBiteship
from app.models.base import utcnow
from app.models.fulfillment import FulfillmentGroup, Shipment
from app.models.governance import ExternalOperation, OutboxMessage
from app.modules import fulfillment
from tests.conftest import owner_session
from tests.test_m2_reliability import _packed_group
from tests.world import build_world, principal


@pytest.fixture(autouse=True)
def _fresh():
    mockstore.reset()
    yield
    mockstore.reset()


def test_dead_letter_review_and_requeue(tenant, client):
    def always_fails(session, msg):
        raise RuntimeError("poison")

    HANDLERS["test.poison"] = always_fails
    with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
        enqueue(s, tenant.org_id, "test.poison", f"poison-{tenant.org_id}", {})
    now = utcnow()
    for i in range(10):
        dispatch_due(now=now + timedelta(hours=2 * i))
    r = client.get("/dead-letters", headers=tenant.headers("owner"))
    mine = [d for d in r.json() if d["topic"] == "test.poison"]
    assert r.status_code == 200 and len(mine) == 1 and "poison" in mine[0]["last_error"]
    assert client.get("/dead-letters", headers=tenant.headers("pm_a")).status_code == 403
    assert client.post(f"/dead-letters/{mine[0]['id']}/requeue", headers=tenant.headers("owner")).status_code == 200
    with owner_session() as s:
        msg = s.scalars(select(OutboxMessage).where(OutboxMessage.organization_id == tenant.org_id,
                                                    OutboxMessage.topic == "test.poison")).one()
        assert (msg.status, msg.attempts) == ("pending", 0)
    # Dies again after requeue: the same dead letter is re-opened (no duplicate row).
    for i in range(10):
        dispatch_due(now=now + timedelta(hours=40 + 2 * i))
    again = [d for d in client.get("/dead-letters", headers=tenant.headers("owner")).json()
             if d["topic"] == "test.poison"]
    assert [d["id"] for d in again] == [mine[0]["id"]]
    with owner_session() as s:  # leave nothing pending for other tests' dispatchers
        s.scalars(select(OutboxMessage).where(OutboxMessage.organization_id == tenant.org_id,
                                              OutboxMessage.topic == "test.poison")).one().status = "done"


def test_uncertain_booking_resolved_by_human_then_shipment_created_without_rebooking(tenant, client, monkeypatch):
    w = build_world(tenant)
    oid, gid, aid = _packed_group(tenant, w)
    packer = principal(tenant, "packer")
    with tenant_session(tenant.org_id, packer.user_id) as s:
        fulfillment.request_booking(s, packer, gid, aid)
    monkeypatch.setenv("OMS_MOCK_FAULT", "biteship.create_order:timeout_after_accept")
    dispatch_due()
    monkeypatch.delenv("OMS_MOCK_FAULT")
    dispatch_due(now=utcnow() + timedelta(hours=2))  # retry: no lookup → uncertain, no second booking
    (booking,) = MockBiteship.bookings_for(f"grp-{gid}")

    listed = client.get("/external-operations", headers=tenant.headers("owner")).json()
    op = next(o for o in listed if o["idempotency_key"] == f"grp-{gid}")
    assert client.get("/external-operations", headers=tenant.headers("packer")).status_code == 403
    # Human checks the Biteship dashboard, finds the booking and confirms it.
    r = client.post(f"/external-operations/{op['id']}/resolve", headers=tenant.headers("owner"),
                    json={"effect_exists": True, "provider_ref": booking["ref"]})
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    dispatch_due()
    assert len(MockBiteship.bookings_for(f"grp-{gid}")) == 1
    with owner_session() as s:
        sh = s.scalars(select(Shipment).where(Shipment.group_id == gid)).one()
        assert sh.external_id == booking["ref"] and s.get(FulfillmentGroup, gid).status == "booked"
        assert s.scalars(select(ExternalOperation).where(ExternalOperation.idempotency_key == f"grp-{gid}")
                         ).one().status == "confirmed"
    assert client.post(f"/external-operations/{op['id']}/resolve", headers=tenant.headers("owner"),
                       json={"effect_exists": True, "provider_ref": "x"}).status_code == 409


def test_procurement_copilot_endpoints_respect_portfolio_scope(tenant, client):
    from tests.test_accounting_services import _order, _pay_gateway

    w = build_world(tenant)
    oid = _order(tenant, w, [("A2", 1, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    tasks = client.get("/procurement/tasks", headers=tenant.headers("procurement_a")).json()
    task = next(t for t in tasks if t["order_id"] == str(oid))
    assert all(t["order_id"] != str(oid) for t in client.get("/procurement/tasks",
                                                             headers=tenant.headers("pm_b")).json())
    r = client.post(f"/procurement/tasks/{task['id']}/observations", headers=tenant.headers("procurement_a"),
                    json={"price": "90000", "shipping_cost": "10000", "in_stock": True, "variant_matches": True})
    assert r.status_code == 200 and r.json()["status"] == "awaiting_approval"
    # Purchase before approval is refused; injected fields are rejected by the schema.
    r = client.post(f"/procurement/tasks/{task['id']}/purchases", headers=tenant.headers("procurement_a"),
                    json={"external_order_id": "SHP-1", "amount_paid": "100000", "evidence_ref": "media://r",
                          "lines": [{"order_item_id": str(oid), "qty": 1, "unit_price": "100000"}]})
    assert r.status_code in (403, 409, 422)
    r = client.post(f"/procurement/tasks/{task['id']}/observations", headers=tenant.headers("procurement_a"),
                    json={"price": "1", "shipping_cost": "0", "in_stock": True, "variant_matches": True,
                          "approved": True})
    assert r.status_code == 422
