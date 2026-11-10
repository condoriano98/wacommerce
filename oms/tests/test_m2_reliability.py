"""Acceptance #6: failed and retried jobs apply external effects exactly once (ADR-015).

A real worker **process** runs the provider job against the mocked provider and is SIGKILLed after the provider has
accepted the request but before the OMS commits. Recovery: stale heartbeat → ``recover_stalled`` → a healthy worker runs
the job again. Provider-side effects are counted in the mock provider's own state (file-backed, independent of the OMS
database). DOKU has a status lookup → exactly one payment page and the link is completed. Biteship has no lookup by
reference (G-BS4) → the operation ends ``uncertain`` with an exception case and no second booking.
"""

import asyncio
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

import app.registry  # noqa: F401
from app.core.db import tenant_session
from app.core.outbox import dispatch_due
from app.integrations import mockstore
from app.integrations.biteship import MockBiteship
from app.models.fulfillment import FulfillmentGroup
from app.models.governance import ExceptionCase, ExternalOperation
from app.models.orders import Order, Payment
from app.modules import approvals, fulfillment, payments
from app.worker import QUEUES, procrastinate_app, recover_stalled, relay
from tests.conftest import owner_session
from tests.test_accounting_services import _groups, _order, _pay_gateway
from tests.world import build_world, principal

OMS_DIR = Path(__file__).parents[1]


@pytest.fixture
def provider_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OMS_MOCK_PROVIDER_DIR", str(tmp_path / "providers"))
    yield tmp_path / "providers"
    mockstore.reset()


def _clear_jobs() -> None:
    with owner_session() as s:
        s.execute(text("delete from procrastinate.procrastinate_jobs"))
        s.execute(text("delete from procrastinate.procrastinate_workers"))


def _relay() -> int:
    async def go():
        async with procrastinate_app.open_async():
            return await relay(procrastinate_app)

    return asyncio.run(go())


def _recover_and_drain() -> int:
    async def go():
        async with procrastinate_app.open_async():
            n = await recover_stalled(procrastinate_app, seconds_since_heartbeat=60)
            await procrastinate_app.run_worker_async(queues=QUEUES, wait=False, install_signal_handlers=False,
                                                     listen_notify=False)
            return n

    return asyncio.run(go())


def _run_worker_process_with_fault(fault: str) -> int:
    """Run one worker process until the queue is empty or the injected fault kills it."""
    code = ("import app.registry\nfrom app.worker import procrastinate_app, QUEUES\n"
            "procrastinate_app.run_worker(queues=QUEUES, wait=False, install_signal_handlers=False, "
            "listen_notify=False)\n")
    env = {**os.environ, "OMS_MOCK_FAULT": fault}
    proc = subprocess.run([sys.executable, "-c", code], cwd=OMS_DIR, env=env, timeout=120, capture_output=True)
    return proc.returncode


def _age_dead_worker_heartbeats() -> None:
    """Time passing: the killed worker stops heart-beating (we age it instead of sleeping 60 s)."""
    with owner_session() as s:
        s.execute(text("update procrastinate.procrastinate_workers set last_heartbeat = now() - interval '10 minutes'"))


def _op(provider: str, key: str):
    with owner_session() as s:
        return s.scalars(select(ExternalOperation).where(ExternalOperation.provider == provider,
                                                          ExternalOperation.idempotency_key == key)).one()


def test_6_doku_link_killed_after_provider_accept_is_completed_once_via_status_lookup(tenant, provider_dir):
    _clear_jobs()
    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")])
    with tenant_session(tenant.org_id, principal(tenant, "owner").user_id) as s:
        p = payments.request_doku_link(s, s.get(Order, oid))
        pid, invoice = p.id, p.provider_ref

    assert _relay() >= 1
    assert _run_worker_process_with_fault("doku.create_payment:kill_after_accept") == -9  # SIGKILL
    with mockstore.state("doku") as st:
        assert st["create_calls"] == 1 and invoice in st["payments"]  # provider effect exists
    assert _op("doku", invoice).status == "sent"  # committed before the call; never confirmed
    with owner_session() as s:
        assert s.get(Payment, pid).payment_url is None  # OMS transaction died with the worker

    _age_dead_worker_heartbeats()
    assert _recover_and_drain() >= 1

    with mockstore.state("doku") as st:
        assert st["create_calls"] == 1  # no second payment page
    op = _op("doku", invoice)
    assert op.status == "confirmed" and op.attempts == 1
    with owner_session() as s:
        p = s.get(Payment, pid)
        assert p.payment_url.endswith(invoice) and p.evidence["link_status"] == "created"


def _packed_group(t, w):
    oid = _order(t, w, [("A1", 1, "0")])
    _pay_gateway(t, oid)
    dispatch_due()  # routing job
    gid = next(g for g, route in _groups(t, oid) if route == "OWNED_STOCK_BITESHIP")
    packer = principal(t, "packer")
    with tenant_session(t.org_id, packer.user_id) as s:
        fulfillment.start_packing(s, packer, gid)
    with tenant_session(t.org_id, packer.user_id) as s:
        fulfillment.mark_packed(s, packer, gid, packing_cost=Decimal("2000"))
        a = approvals.request_approval(s, packer, kind="paid_shipment_booking", subject_type="fulfillment_group",
                                       subject_id=str(gid), amount=None, payload={}, portfolio_id=None,
                                       idempotency_key=f"book-{gid}")
        aid = a.id
    owner = principal(t, "owner")
    with tenant_session(t.org_id, owner.user_id) as s:
        approvals.decide(s, owner, aid, True, "ok")
    return oid, gid, aid


def test_6_biteship_booking_killed_after_accept_becomes_uncertain_never_rebooked(tenant, provider_dir):
    _clear_jobs()
    w = build_world(tenant)
    oid, gid, aid = _packed_group(tenant, w)
    packer = principal(tenant, "packer")
    with tenant_session(tenant.org_id, packer.user_id) as s:
        fulfillment.request_booking(s, packer, gid, aid)

    assert _relay() >= 1
    assert _run_worker_process_with_fault("biteship.create_order:kill_after_accept") == -9
    assert len(MockBiteship.bookings_for(f"grp-{gid}")) == 1
    _age_dead_worker_heartbeats()
    _recover_and_drain()

    assert len(MockBiteship.bookings_for(f"grp-{gid}")) == 1  # never re-booked
    op = _op("biteship", f"grp-{gid}")
    assert op.status == "uncertain"
    with owner_session() as s:
        assert s.get(FulfillmentGroup, gid).status == "booking_uncertain"
        case = s.scalars(select(ExceptionCase).where(ExceptionCase.subject_id == str(op.id))).one()
        assert case.type == "external_uncertain" and case.order_id == oid


def test_6_biteship_booking_without_fault_books_once_and_creates_shipment(tenant, provider_dir):
    _clear_jobs()
    w = build_world(tenant)
    oid, gid, aid = _packed_group(tenant, w)
    packer = principal(tenant, "packer")
    with tenant_session(tenant.org_id, packer.user_id) as s:
        fulfillment.request_booking(s, packer, gid, aid)
    with pytest.raises(HTTPException):  # approval already executed: cannot be reused for a second booking
        with tenant_session(tenant.org_id, packer.user_id) as s:
            g = s.get(FulfillmentGroup, gid)
            g.status = "packed"
            fulfillment.request_booking(s, packer, gid, aid)
    _relay()
    _recover_and_drain()
    _relay()
    _recover_and_drain()
    assert len(MockBiteship.bookings_for(f"grp-{gid}")) == 1
    assert _op("biteship", f"grp-{gid}").status == "confirmed"
    with owner_session() as s:
        g = s.get(FulfillmentGroup, gid)
        assert g.status == "booked"
        assert s.execute(text("select count(*) from shipments where group_id = :g"), {"g": gid}).scalar() == 1


def test_6_doku_timeout_after_accept_retry_uses_lookup_not_a_second_call(tenant, provider_dir, monkeypatch):
    """Same policy in-process: a timeout after the provider accepted → retry finds the payment by invoice."""
    from datetime import timedelta

    from app.models.base import utcnow

    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")])
    with tenant_session(tenant.org_id, principal(tenant, "owner").user_id) as s:
        p = payments.request_doku_link(s, s.get(Order, oid))
        pid, invoice = p.id, p.provider_ref
    monkeypatch.setenv("OMS_MOCK_FAULT", "doku.create_payment:timeout_after_accept")
    dispatch_due()
    assert _op("doku", invoice).status == "sent"
    monkeypatch.delenv("OMS_MOCK_FAULT")
    dispatch_due(now=utcnow() + timedelta(hours=2))  # outbox backoff elapsed
    with mockstore.state("doku") as st:
        assert st["create_calls"] == 1
    assert _op("doku", invoice).status == "confirmed"
    with owner_session() as s:
        assert s.get(Payment, pid).payment_url is not None
