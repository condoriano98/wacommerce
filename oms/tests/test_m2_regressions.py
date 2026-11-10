"""Regression: dedupe results must be reported correctly. SQLAlchemy 2.1 returns rowcount = -1 for ORM-enabled
``INSERT … ON CONFLICT DO NOTHING``; the services now use RETURNING. Found during M2 (CAPI events were never queued)."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text

import app.registry  # noqa: F401
from app.core.db import tenant_session
from app.models.base import utcnow
from app.models.fulfillment import Shipment
from app.models.org import BankAccount
from app.modules import conversions, fulfillment, payments
from tests.conftest import owner_session
from tests.test_accounting_services import _order, _pay_gateway, _ship_owned
from tests.world import build_world, principal


def test_duplicate_gateway_notification_is_reported_as_duplicate(tenant):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")])
    ref = _pay_gateway(tenant, oid)
    from app.core.db import act_as_system, webhook_session

    with webhook_session() as s:
        act_as_system(s, tenant.org_id)
        assert payments.record_gateway_notification(s, provider="doku", notification_id=f"N-{ref}", provider_ref=ref,
                                                    verified_status="paid", verified_amount=Decimal("1"),
                                                    raw={}) == "duplicate"


def test_duplicate_shipment_event_reports_no_change(tenant):
    from app.core.outbox import dispatch_due

    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    (sid,) = _ship_owned(tenant, oid, events=("picked",))
    packer = principal(tenant, "packer")
    with tenant_session(tenant.org_id, packer.user_id) as s:
        sh = s.get(Shipment, sid)
        ev = s.execute(text("select occurred_at from shipment_events where shipment_id = :s"), {"s": sid}).scalar()
        assert fulfillment.apply_shipment_event(s, sh, "picked", ev, {}) is False


def test_bank_import_counts_only_new_lines_and_capi_enqueue_reports_new(tenant):
    w = build_world(tenant)
    fin, owner = principal(tenant, "finance"), principal(tenant, "owner")
    with owner_session() as s:
        ba = BankAccount(organization_id=tenant.org_id, legal_entity_id=tenant.legal_entity_id, provider="bank",
                         institution="BCA", account_ref="BCA-REG")
        s.add(ba)
        s.flush()
        ba_id = ba.id
    rows = [{"booked_at": date.today(), "amount": Decimal("1000"), "reference": "R1"},
            {"booked_at": date.today(), "amount": Decimal("2000"), "reference": "R2"}]
    with tenant_session(tenant.org_id, fin.user_id) as s:
        assert payments.import_bank_statement(s, fin, ba_id, rows, "b1") == 2
    with tenant_session(tenant.org_id, fin.user_id) as s:
        assert payments.import_bank_statement(s, fin, ba_id, rows, "b1-again") == 0
    with tenant_session(tenant.org_id, owner.user_id) as s:
        kw = dict(org_id=tenant.org_id, subject_type="customer", subject_id=w.customer, customer_id=w.customer,
                  event_name="QualifiedLead", event_time=utcnow() - timedelta(minutes=1))
        assert conversions.enqueue_event(s, **kw) is True
        assert conversions.enqueue_event(s, **kw) is False
