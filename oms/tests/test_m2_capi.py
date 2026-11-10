"""Meta Business Messaging CAPI sender (mock): send-time eligibility, hashed identifiers, never re-sent."""

from datetime import timedelta

import pytest
from sqlalchemy import select

import app.registry  # noqa: F401
from app.core.db import tenant_session
from app.core.outbox import dispatch_due
from app.integrations import mockstore
from app.integrations.meta_capi import MockMetaCapi
from app.models.base import utcnow
from app.models.marketing import ConversionOutbox
from app.modules import conversions
from tests.conftest import owner_session
from tests.test_m2_whatsapp import _pnid, post_meta, wa_message
from tests.world import build_world, principal


@pytest.fixture(autouse=True)
def _fresh():
    mockstore.reset()
    yield
    mockstore.reset()


def _ctwa(pnid, bsuid, text_body, phone=None):
    p = wa_message(pnid, bsuid=bsuid, text_body=text_body)
    m = p["entry"][0]["changes"][0]["value"]["messages"][0]
    m["referral"] = {"source_type": "ad", "source_id": "120200001", "ctwa_clid": f"clid-{bsuid}",
                     "source_url": "https://fb.me/x"}
    if phone:
        m["from"] = phone
        p["entry"][0]["changes"][0]["value"]["contacts"][0]["wa_id"] = phone
    return p


def _conversions(t):
    with owner_session() as s:
        return s.scalars(select(ConversionOutbox).where(ConversionOutbox.organization_id == t.org_id)).all()


def test_consented_lead_is_sent_once_with_hashed_identifiers_only(tenant, client):
    w = build_world(tenant)
    post_meta(client, _ctwa(_pnid(w), "ID.capi1", "setuju data", phone="6281234567890"))
    dispatch_due()
    dispatch_due(now=utcnow() + timedelta(hours=1))
    events = [e for e in MockMetaCapi.received() if e["user_data"].get("ctwa_clid") == "clid-ID.capi1"]
    assert len(events) == 1
    e = events[0]
    assert e["event_name"] == "LeadSubmitted" and e["action_source"] == "business_messaging"
    assert "6281234567890" not in str(e) and len(e["user_data"]["ph"][0]) == 64  # hashed, never raw
    (c,) = _conversions(tenant)
    assert c.status == "sent" and str(c.event_id) == e["event_id"]


def test_without_consent_or_too_old_nothing_is_sent(tenant, client):
    w = build_world(tenant)
    post_meta(client, _ctwa(_pnid(w), "ID.capi2", "halo"))  # no consent given
    dispatch_due()
    owner = principal(tenant, "owner")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        conversions.enqueue_event(s, org_id=tenant.org_id, subject_type="customer", subject_id=w.customer,
                                  customer_id=w.customer, event_name="QualifiedLead",
                                  event_time=utcnow() - timedelta(days=8))
    dispatch_due()
    statuses = {c.event_name: (c.status, c.status_reason) for c in _conversions(tenant)}
    assert statuses["LeadSubmitted"] == ("suppressed", "consent_unknown")
    assert statuses["QualifiedLead"] == ("expired", "older_than_7_days")
    assert MockMetaCapi.received() == []


def test_uncertain_capi_delivery_is_never_resent(tenant, client, monkeypatch):
    w = build_world(tenant)
    monkeypatch.setenv("OMS_MOCK_FAULT", "meta_capi.send_event:timeout_after_accept")
    post_meta(client, _ctwa(_pnid(w), "ID.capi3", "setuju data"))
    dispatch_due()
    monkeypatch.delenv("OMS_MOCK_FAULT")
    dispatch_due(now=utcnow() + timedelta(hours=2))
    assert len(MockMetaCapi.received()) == 1  # Meta does not dedupe: one delivery at most
    (c,) = _conversions(tenant)
    assert (c.status, c.status_reason) == ("failed", "uncertain_not_resent")
