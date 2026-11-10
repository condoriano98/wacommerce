"""M5 security (PR #1 hardening tests 1, 2, 7): property-based RLS fuzzing across roles and tables, tenant FK catalogue,
route authorization matrix, secret hygiene, credential key rotation."""

import re
import uuid
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text

import app.registry  # noqa: F401
from app.core.config import get_settings
from app.core.db import tenant_session
from app.core.rls import PORTFOLIO, PORTFOLIO_SHARED, PORTFOLIO_STRICT, TABLE_POLICIES
from app.models.base import utcnow
from app.modules import platforms
from tests.conftest import make_tenant, owner_session
from tests.test_accounting_services import _order, _pay_gateway, _ship_owned
from tests.world import build_world, principal

PORTFOLIO_TABLES = sorted(t for t, (kind, col) in TABLE_POLICIES.items()
                          if kind in (PORTFOLIO, PORTFOLIO_STRICT, PORTFOLIO_SHARED) and col == "portfolio_id")
ROLES = ["owner", "pm_a", "pm_b", "finance", "procurement_a", "packer", "cs", "ai"]
_STATE: dict = {}


def _populated():
    """Two organizations with activity in portfolios A and B (built once for the fuzz run)."""
    if not _STATE:
        from app.core.outbox import dispatch_due

        a, b = make_tenant(), make_tenant()
        for t in (a, b):
            w = build_world(t)
            for lines in ([("A1", 1, "0")], [("B1", 1, "0")]):
                oid = _order(t, w, lines)
                _pay_gateway(t, oid)
                dispatch_due()
                _ship_owned(t, oid)
        _STATE.update(a=a, b=b)
    return _STATE["a"], _STATE["b"]


def _visible_portfolios(t, user) -> set:
    with tenant_session(t.org_id, t.users[user]) as s:
        return {r[0] for r in s.execute(text("select id from portfolios")).all()}


@settings(max_examples=60, deadline=None, suppress_health_check=list(HealthCheck))
@given(role=st.sampled_from(ROLES), table=st.sampled_from(PORTFOLIO_TABLES), spoof=st.booleans())
def test_1_rls_fuzz_reads_and_writes_never_cross_org_or_portfolio(role, table, spoof):
    t, other = _populated()
    visible = _visible_portfolios(t, role)
    foreign = set(t.portfolios.values()) - visible
    with tenant_session(t.org_id, t.users[role]) as s:
        if spoof:  # own user, another organization's id: no membership there → nothing portfolio-scoped visible
            s.execute(text("select set_config('app.org_id', :o, true)"), {"o": str(other.org_id)})
        rows = s.execute(text(f"select organization_id, portfolio_id from {table}")).all()
        orgs = {r[0] for r in rows}
        if spoof:
            assert t.org_id not in orgs
            assert not [r for r in rows if r[1] is not None], (role, table)  # no portfolio rows of the other org
        else:
            assert orgs <= {t.org_id}
            assert {r[1] for r in rows} - {None} <= visible, (role, table)
        if foreign and not spoof:
            from sqlalchemy.exc import ProgrammingError

            try:
                with s.begin_nested():
                    n = s.execute(text(f"update {table} set portfolio_id = portfolio_id where portfolio_id = any(:f) "
                                       f"returning 1"), {"f": list(foreign)}).all()
                assert n == [], (role, table)
            except ProgrammingError as exc:  # append-only tables: no UPDATE privilege at all (stronger than RLS)
                assert "permission denied" in str(exc)


def test_2_every_tenant_foreign_key_is_tenant_scoped():
    """Each FK from a tenant table to a tenant parent (by id) must have a composite (organization_id, fk) twin."""
    with owner_session() as s:
        fks = s.execute(text("""
            select tc.table_name, kcu.column_name, ccu.table_name as parent
            from information_schema.table_constraints tc
            join information_schema.key_column_usage kcu on kcu.constraint_name = tc.constraint_name
                 and kcu.table_schema = tc.table_schema
            join information_schema.constraint_column_usage ccu on ccu.constraint_name = tc.constraint_name
                 and ccu.table_schema = tc.table_schema
            where tc.constraint_type = 'FOREIGN KEY' and tc.table_schema = 'public' and ccu.column_name = 'id'
              and kcu.column_name <> 'organization_id'""")).all()
        tenant_tables = {r[0] for r in s.execute(text(
            "select table_name from information_schema.columns where table_schema = 'public' "
            "and column_name = 'organization_id'")).all()} - {"organizations"}
        composite = {(r[0], r[1]) for r in s.execute(text("""
            select conrelid::regclass::text, a.attname from pg_constraint c
            join lateral unnest(c.conkey) k(n) on true
            join pg_attribute a on a.attrelid = c.conrelid and a.attnum = k.n
            where c.contype = 'f' and array_length(c.conkey, 1) = 2 and a.attname <> 'organization_id'""")).all()}
    missing = sorted({(tbl, col, parent) for tbl, col, parent in fks
                      if tbl in tenant_tables and parent in tenant_tables and (tbl, col) not in composite})
    assert missing == [], missing


def test_2_cross_org_reference_is_rejected_by_the_database(tenant):
    other = make_tenant()
    with pytest.raises(Exception) as e:  # noqa: PT011
        with owner_session() as s:
            s.execute(text("insert into portfolio_budgets (id, organization_id, created_at, portfolio_id, "
                           "period_start, period_end, category, amount) values (gen_random_uuid(), :o, now(), :p, "
                           "'2026-11-01', '2026-11-30', 'advertising', 1)"),
                      {"o": tenant.org_id, "p": other.portfolios["A"]})
    assert "fkt_" in str(e.value) or "foreign key" in str(e.value).lower()


PUBLIC = {("GET", "/healthz"), ("GET", "/readyz")}


def _probe_url(path: str) -> str:
    return re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path).replace(f"/{uuid.UUID(int=0)}", "")


def test_route_matrix_no_unauthenticated_access(client):
    from app.main import app

    leaks = []
    for path, ops in app.openapi()["paths"].items():
        for method in ops:
            if (method.upper(), path) in PUBLIC:
                continue
            r = client.request(method.upper(), _probe_url(path), json={})
            if r.status_code < 400:
                leaks.append((method.upper(), path, r.status_code))
    assert leaks == [], leaks


SENSITIVE_DENIED_TO_PM = [
    ("GET", "/dead-letters"), ("GET", "/external-operations"), ("GET", "/audit-logs"),
    ("POST", "/ops/invariants/check"), ("POST", "/fpa/simulations/stress"),
]


def test_route_matrix_sensitive_operations_denied_to_portfolio_managers(tenant, client):
    for method, path in SENSITIVE_DENIED_TO_PM:
        r = client.request(method, path, headers=tenant.headers("pm_a"), json={})
        assert r.status_code == 403, (method, path, r.status_code)
    bid = uuid.uuid4()
    r = client.post(f"/platform-accounts/{bid}/credentials", headers=tenant.headers("pm_a"), json={})
    assert r.status_code in (403, 404, 405, 422)


def test_7_secret_never_in_responses_audit_logs_or_logs(tenant, client, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    w = build_world(tenant)
    secret = f"SK-live-{uuid.uuid4().hex}"
    r = client.put(f"/platform-accounts/{w.shopee_seller}/credentials", headers=tenant.headers("owner"),
                   json={"name": "access_token", "secret": secret})
    assert r.status_code == 204, r.text
    assert platforms.read_secret(tenant.org_id, w.shopee_seller, "access_token") == secret  # worker path works
    from app.main import app

    for path, ops in app.openapi()["paths"].items():
        if "get" in ops and "{" not in path and path not in ("/metrics",):
            body = client.get(path, headers=tenant.headers("owner")).text
            assert secret not in body, path
    body = client.get(f"/platform-accounts/{w.shopee_seller}/credentials", headers=tenant.headers("owner")).text
    assert secret not in body
    with owner_session() as s:
        assert s.execute(text("select count(*) from audit_logs where after::text like :p"),
                         {"p": f"%{secret}%"}).scalar() == 0
        cipher = s.execute(text("select ciphertext from account_credentials where platform_account_id = :a"),
                           {"a": w.shopee_seller}).scalar()
        assert secret.encode() not in bytes(cipher)
    assert secret not in caplog.text


def test_7_credential_master_key_rotation(tenant, monkeypatch):
    w = build_world(tenant)
    owner = principal(tenant, "owner")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        platforms.store_credential(s, owner, w.shopee_seller, "api_key", "rotate-me")
    cfg = get_settings()
    old_key, new_key = cfg.credentials_master_key, Fernet.generate_key().decode()
    monkeypatch.setattr(cfg, "credentials_master_key", new_key)
    monkeypatch.setattr(cfg, "credentials_previous_keys", old_key)
    assert platforms.read_secret(tenant.org_id, w.shopee_seller, "api_key") == "rotate-me"  # old key still decrypts
    assert platforms.reencrypt_credentials(tenant.org_id, w.shopee_seller) == 1
    monkeypatch.setattr(cfg, "credentials_previous_keys", "")  # old key retired
    assert platforms.read_secret(tenant.org_id, w.shopee_seller, "api_key") == "rotate-me"
    monkeypatch.setattr(cfg, "credentials_master_key", old_key)  # the old key can no longer read it
    with pytest.raises(Exception):  # noqa: B017,PT011 - cryptography InvalidToken
        platforms.read_secret(tenant.org_id, w.shopee_seller, "api_key")
    _ = utcnow() - timedelta(0)


def test_production_refuses_development_secrets():
    from app.core.config import Settings, production_problems

    dev = Settings(env="production")
    problems = production_problems(dev)
    assert any("jwt_secret" in p for p in problems) and any("credentials_master_key" in p for p in problems)
    assert production_problems(Settings(env="dev")) == []
    ok = Settings(env="production", jwt_secret="x" * 40, credentials_master_key=Fernet.generate_key().decode(),
                  meta_app_secret="m", meta_verify_token="v", chatwoot_webhook_secret="c",
                  doku_notification_secret="d", biteship_webhook_token="b",
                  database_url="postgresql+psycopg://oms_app:REAL@db/oms",
                  worker_database_url="postgresql+psycopg://oms_worker:REAL@db/oms",
                  webhook_database_url="postgresql+psycopg://oms_webhook:REAL@db/oms")
    assert production_problems(ok) == []
