"""M5 observability & reliability: correlation IDs (request → outbox job), JSON log redaction, /metrics and /readyz,
failure injection visible in metrics (hardening test 9), circuit breaker, webhook replay window (test 5), invariants
(test 3), alert rules reference real metrics."""

import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from sqlalchemy import select, text

import app.registry  # noqa: F401
from app.core import observability
from app.core.config import get_settings
from app.core.db import tenant_session
from app.core.external_ops import NotSent
from app.core.outbox import HANDLERS, dispatch_due, enqueue
from app.integrations import http as oms_http
from app.integrations import mockstore
from app.models.base import utcnow
from app.models.governance import ExceptionCase, OutboxMessage
from app.modules import invariants
from tests.conftest import owner_session
from tests.test_accounting_services import _order, _pay_gateway, _ship_owned
from tests.test_m2_whatsapp import _pnid, wa_message
from tests.world import build_world, principal

ROOT = Path(__file__).parents[2]


@pytest.fixture
def metrics_token(monkeypatch):
    monkeypatch.setattr(get_settings(), "metrics_token", "test-metrics-token")
    yield "test-metrics-token"


def test_correlation_id_flows_from_request_to_outbox_job(tenant, client):
    w = build_world(tenant)
    mockstore.reset()
    raw = json.dumps(wa_message(_pnid(w), bsuid="ID.cid1")).encode()
    sig = hmac.new(get_settings().meta_app_secret.encode(), raw, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": f"sha256={sig}",
                                                                "X-Request-ID": "cid-test-123"})
    assert r.status_code == 200 and r.headers["x-request-id"] == "cid-test-123"
    with owner_session() as s:
        payload = s.scalars(select(OutboxMessage.payload).where(OutboxMessage.organization_id == tenant.org_id,
                                                               OutboxMessage.topic == "wa.process")).one()
    assert payload["_cid"] == "cid-test-123"
    seen = []
    HANDLERS["test.cid"] = lambda session, msg: seen.append(observability.correlation_id.get())
    token = observability.correlation_id.set("cid-job-9")
    with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
        enqueue(s, tenant.org_id, "test.cid", f"cid-{tenant.org_id}", {})
    observability.correlation_id.reset(token)
    dispatch_due()
    assert "cid-job-9" in seen  # worker re-binds the request's correlation ID
    assert client.get("/healthz").headers["x-request-id"]  # generated when absent


def test_json_logs_redact_secret_fields(caplog):
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "provider call", None, None)
    rec.fields = {"provider": "doku", "api_key": "SK-123", "Authorization": "Bearer x", "status": 200}
    out = json.loads(observability.JsonFormatter().format(rec))
    assert out["api_key"] == "[redacted]" and out["Authorization"] == "[redacted]" and out["status"] == 200
    assert "SK-123" not in json.dumps(out)


def test_metrics_endpoint_protection_and_injected_failures(tenant, client, metrics_token):
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    # Inject a poison message → dead letter (queue failure) and provider errors (integration failure).
    HANDLERS["test.poison9"] = lambda s, m: (_ for _ in ()).throw(RuntimeError("poison"))
    with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
        enqueue(s, tenant.org_id, "test.poison9", f"p9-{tenant.org_id}", {})
    from datetime import timedelta

    for i in range(10):
        dispatch_due(now=utcnow() + timedelta(hours=2 * i))
    with respx.mock:
        respx.post("https://provider.invalid/x").mock(return_value=httpx.Response(503))
        oms_http.request("POST", "https://provider.invalid/x", breaker_key="t9", provider="doku")
    body = client.get("/metrics", headers={"Authorization": f"Bearer {metrics_token}"}).text
    assert re.search(r'oms_dead_letters_open\{label="test.poison9"\} [1-9]', body)
    assert 'oms_provider_errors_total{kind="503",provider="doku"}' in body
    assert "oms_http_requests_total" in body and "oms_outbox_pending" in body and "oms_jobs_stalled" in body
    with owner_session() as s:  # leave nothing failing for other tests' dispatchers
        s.execute(text("update outbox_messages set status = 'done' where topic = 'test.poison9'"))


def test_metrics_disabled_without_token_and_readyz(client):
    assert client.get("/metrics").status_code == 404
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["schema"] == "0016"


def test_circuit_breaker_fails_fast_without_sending():
    calls = []
    with respx.mock:
        respx.post("https://provider.invalid/cb").mock(side_effect=lambda req: calls.append(1) or httpx.Response(502))
        for _ in range(5):
            oms_http.request("POST", "https://provider.invalid/cb", breaker_key="cb-test", provider="biteship")
        with pytest.raises(NotSent):
            oms_http.request("POST", "https://provider.invalid/cb", breaker_key="cb-test", provider="biteship")
    assert len(calls) == 5  # the sixth request never left the process
    b = oms_http.breaker("cb-test")
    b.opened_at -= b.cooldown  # cooldown elapsed → one half-open probe allowed
    with respx.mock:
        respx.post("https://provider.invalid/cb").mock(return_value=httpx.Response(200))
        assert oms_http.request("POST", "https://provider.invalid/cb", breaker_key="cb-test",
                                provider="biteship").status_code == 200
    assert b.opened_at is None


def test_webhook_replay_window_rejects_stale_signed_callbacks(client):
    raw = json.dumps({"event": "message_created"}).encode()
    ts = str(int(time.time()) - 3600)
    sig = hmac.new(get_settings().chatwoot_webhook_secret.encode(), ts.encode() + b"." + raw,
                   hashlib.sha256).hexdigest()
    r = client.post("/webhooks/chatwoot", content=raw, headers={"X-Chatwoot-Timestamp": ts,
                                                                "X-Chatwoot-Signature": f"sha256={sig}"})
    assert r.status_code == 401 and "stale" in r.text
    from app.integrations.doku import signature

    body = json.dumps({"order": {"invoice_number": "INV-OLD"}, "transaction": {"status": "SUCCESS"}}).encode()
    s = get_settings()
    old = "2026-01-01T00:00:00Z"
    sig = signature("MCH-1", "N-OLD", old, s.doku_notification_path, s.doku_notification_secret, body)
    r = client.post("/webhooks/doku", content=body, headers={"Client-Id": "MCH-1", "Request-Id": "N-OLD",
                                                             "Request-Timestamp": old, "Signature": sig})
    assert r.status_code == 401 and "stale" in r.text


def test_invariants_clean_after_full_flow_and_violation_opens_case(tenant, client):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    _ship_owned(tenant, oid)
    r = client.post("/ops/invariants/check", headers=tenant.headers("finance"))
    assert r.status_code == 200 and r.json()["ok"] is True, r.json()
    with owner_session() as s:  # inject: a "matched" item that is actually Rp500 short
        s.execute(text("insert into reconciliation_items (id, organization_id, created_at, kind, expected_ref, "
                       "expected_amount, actual_amount, difference, expected_at, status, evidence) "
                       "values (gen_random_uuid(), :o, now(), 'gateway', 'X', 1000, 500, -500, current_date, "
                       "'matched', '{}')"), {"o": tenant.org_id})
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        found = invariants.run(s, tenant.org_id)
    assert list(found) == ["matched_reconciliation_with_difference"]
    with owner_session() as s:
        case = s.scalars(select(ExceptionCase).where(ExceptionCase.organization_id == tenant.org_id,
                                                     ExceptionCase.type == "invariant_violation")).one()
        assert case.exposure == 500
    assert client.post("/ops/invariants/check", headers=tenant.headers("pm_a")).status_code == 403


def test_alert_rules_reference_exported_metrics():
    rules = yaml.safe_load((ROOT / "deploy" / "observability" / "alerts.yml").read_text())
    exported = {"oms_outbox_oldest_due_seconds", "oms_jobs_stalled", "oms_dead_letters_open",
                "oms_external_operations", "oms_provider_errors_total", "oms_provider_circuit_open_total",
                "oms_reconciliation_open_exposure", "oms_exception_cases_open", "oms_http_requests_total",
                "oms_outbox_pending", "oms_integration_errors_24h", "oms_outbox_processed_total"}
    for g in rules["groups"]:
        for r in g["rules"]:
            names = set(re.findall(r"oms_[a-z0-9_]+", r["expr"]))
            assert names and names <= exported, (r["alert"], names - exported)
