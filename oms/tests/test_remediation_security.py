"""Remediation Phase C — financial authorization, portfolio-limited order visibility and runtime tenant identity."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

import app.registry  # noqa: F401
from app.core.db import tenant_session
from app.core.outbox import dispatch_due
from app.core.security import hash_password
from app.models.base import utcnow
from app.models.org import LegalEntity, RoleAssignment, User
from tests.conftest import owner_session
from tests.test_accounting_services import _order, _pay_gateway
from tests.test_v31_correctness_remediation import _owned_groups, _ship_group
from tests.world import build_world

PERIOD = {"start": "2020-01-01", "end": "2100-12-31"}


def _le_user(t, legal_entity_id, role="finance_manager") -> str:
    name = f"le_{uuid.uuid4().hex[:6]}"
    with owner_session() as s:
        u = User(organization_id=t.org_id, email=f"{name}@example.test", name=name, password_hash=hash_password("x"))
        s.add(u)
        s.flush()
        s.add(RoleAssignment(organization_id=t.org_id, user_id=u.id, role=role, scope_type="legal_entity",
                             scope_id=legal_entity_id))
        t.users[name] = u.id
    return name


def _second_entity(t) -> uuid.UUID:
    with owner_session() as s:
        le = LegalEntity(organization_id=t.org_id, name="PT Second")
        s.add(le)
        s.flush()
        return le.id


# --- C1 financial reporting permissions ---------------------------------------------------------------------------


@pytest.mark.parametrize("user", ["cs", "packer"])
def test_C1_roles_without_fpa_read_cannot_read_financial_reports(tenant, client, user):
    for path, params in (("/fpa/waterfall", PERIOD), ("/fpa/snapshots", {"period_start": "2026-11-01"}),
                         ("/fpa/cash-forecast", {}), ("/capital/summary", {})):
        r = client.get(path, headers=tenant.headers(user), params=params)
        assert r.status_code == 403, (user, path, r.status_code)


def test_C1_portfolio_manager_reads_only_own_portfolio_never_consolidated(tenant, client):
    build_world(tenant)
    pa, pb = str(tenant.portfolios["A"]), str(tenant.portfolios["B"])
    h = tenant.headers("pm_a")
    assert client.get("/fpa/waterfall", headers=h, params={**PERIOD, "portfolio_id": pa}).status_code == 200
    assert client.get("/fpa/waterfall", headers=h, params={**PERIOD, "portfolio_id": pb}).status_code == 403
    for level in ("L1", "L2", "L3"):  # omitting portfolio_id means consolidated / all portfolios
        r = client.get("/fpa/waterfall", headers=h, params={**PERIOD, "level": level})
        assert r.status_code == 403, (level, r.status_code)
    assert client.get("/fpa/waterfall", headers=h, params={**PERIOD, "legal_entity_id": str(tenant.legal_entity_id)}
                      ).status_code == 403
    assert client.get("/fpa/cash-forecast", headers=h).status_code == 403
    assert client.get("/fpa/snapshots", headers=h, params={"period_start": "2026-11-01"}).status_code == 403
    assert client.get("/fpa/snapshots", headers=h, params={"period_start": "2026-11-01", "portfolio_id": pa}
                      ).status_code == 200


def test_C1_org_finance_reads_everything(tenant, client):
    build_world(tenant)
    h = tenant.headers("finance")
    assert client.get("/fpa/waterfall", headers=h, params=PERIOD).status_code == 200
    assert client.get("/fpa/cash-forecast", headers=h).status_code == 200
    assert client.get("/fpa/snapshots", headers=h, params={"period_start": "2026-11-01"}).status_code == 200
    assert client.get("/capital/summary", headers=h).status_code == 200


def test_C1_legal_entity_scope_reads_its_entity_only(tenant, client):
    build_world(tenant)
    other = _second_entity(tenant)
    name = _le_user(tenant, tenant.legal_entity_id)
    h = tenant.headers(name)
    own = {"legal_entity_id": str(tenant.legal_entity_id)}
    assert client.get("/fpa/cash-forecast", headers=h, params=own).status_code == 200
    assert client.get("/fpa/cash-forecast", headers=h, params={"legal_entity_id": str(other)}).status_code == 403
    assert client.get("/fpa/cash-forecast", headers=h).status_code == 403  # consolidated needs org scope
    r = client.get("/fpa/waterfall", headers=h, params={**PERIOD, **own})
    assert r.status_code == 200
    org_view = client.get("/fpa/waterfall", headers=tenant.headers("finance"), params={**PERIOD, **own}).json()
    assert r.json() == org_view  # the entity's full ledger is visible to its entity-scoped finance user
    with tenant_session(tenant.org_id, tenant.users[name]) as s:
        les = {x for (x,) in s.execute(text("select distinct legal_entity_id from ledger_entries")).all()}
    assert les == {tenant.legal_entity_id}
    assert client.get("/fpa/waterfall", headers=h, params=PERIOD).status_code == 403


# --- C2 portfolio-limited order status ----------------------------------------------------------------------------


def test_C2_portfolio_user_sees_status_of_own_lines_only(tenant, client):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0"), ("B1", 1, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    groups = {wh: gid for gid, wh in _owned_groups(oid)}
    assert len(groups) == 1  # one warehouse; groups are per portfolio
    from sqlalchemy import select

    from app.models.fulfillment import FulfillmentGroup

    with owner_session() as s:
        by_portfolio = {g.portfolio_id: g.id for g in s.scalars(select(FulfillmentGroup).where(
            FulfillmentGroup.order_id == oid))}
    sh_b = _ship_group(tenant, by_portfolio[tenant.portfolios["B"]], ("picked", "delivered"))
    # A high-severity case on portfolio B's shipment makes the header status 'exception'.
    with owner_session() as s:
        from app.modules import exceptions

        exceptions.open_case(s, tenant.org_id, "lost_or_damaged", subject_type="shipment", subject_id=str(sh_b),
                             order_id=oid, portfolio_id=tenant.portfolios["B"], context={})
    from app.models.orders import Order
    from app.modules import orders as orders_mod

    with owner_session() as s:
        header = orders_mod.derive_status(s, s.get(Order, oid))
    assert header == "exception"
    rows = {r["order_id"]: r for r in client.get("/orders", headers=tenant.headers("pm_a")).json()}
    mine = rows[str(oid)]
    assert mine["status"] == "in_fulfillment"  # A's line only: allocated, not shipped; no B delivery, no B case
    assert mine["lines"] == 1 and mine["units_delivered"] == 0
    assert "total" not in mine and "shipping_address" not in mine
    rows_b = {r["order_id"]: r for r in client.get("/orders", headers=tenant.headers("pm_b")).json()}
    assert rows_b[str(oid)]["status"] == "delivered"
    assert rows_b[str(oid)]["visible_line_total"] == "80000.00"
    with tenant_session(tenant.org_id, tenant.users["pm_a"]) as s:
        cols = {r[0] for r in s.execute(text("select column_name from information_schema.columns "
                                              "where table_name = 'order_slices'")).all()}
    assert not cols & {"total", "subtotal", "shipping_fee", "shipping_address", "customer_id", "payment_status"}
    _ = utcnow() + timedelta(0)


# --- C3 runtime tenant identity -------------------------------------------------------------------------------


def _spoofed(t, other_org, user="owner"):
    """An authenticated user of ``t`` whose transaction claims another organization (e.g. a compromised code path)."""
    return tenant_session(other_org, t.users[user])


def test_C3_own_user_with_another_orgs_id_sees_and_writes_nothing(tenant):
    from tests.conftest import make_tenant

    other = make_tenant()
    build_world(other)
    with _spoofed(tenant, other.org_id) as s:
        for table in ("users", "products", "warehouses", "legal_entities", "brands", "portfolios", "orders",
                      "ledger_entries", "outbox_messages", "role_assignments"):
            n = s.execute(text(f"select count(*) from {table}")).scalar()
            assert n == 0, (table, n)
        assert s.execute(text("select app_org()")).scalar() is None
    for sql in ("insert into outbox_messages (id, organization_id, created_at, topic, dedupe_key, payload, status, "
                "attempts, next_attempt_at) values (gen_random_uuid(), :o, now(), 't', :k, '{}', 'pending', 0, now())",
                "insert into products (id, organization_id, created_at, name, category) "
                "values (gen_random_uuid(), :o, now(), 'x', 'fmcg')"):
        with pytest.raises(Exception) as e:  # noqa: PT011
            with _spoofed(tenant, other.org_id) as s:
                s.execute(text(sql), {"o": other.org_id, "k": uuid.uuid4().hex})
        assert "row-level security" in str(e.value) or "violates" in str(e.value)


def test_C3_org_context_without_a_user_has_no_tenant(tenant):
    build_world(tenant)
    with tenant_session(tenant.org_id, None) as s:
        assert s.execute(text("select app_org()")).scalar() is None
        assert s.execute(text("select count(*) from products")).scalar() == 0


def test_C3_token_for_a_user_of_another_org_is_rejected(tenant, client):
    from app.core.security import issue_token
    from tests.conftest import make_tenant

    other = make_tenant()
    forged = issue_token(tenant.users["owner"], other.org_id)  # valid signature, mismatched membership
    r = client.get("/orders", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


def test_C3_identity_is_transaction_local_on_a_pooled_connection(tenant):
    from sqlalchemy import create_engine

    from app.core.config import get_settings

    eng = create_engine(get_settings().database_url, pool_size=1, max_overflow=0)
    try:
        with eng.begin() as c:
            c.execute(text("select set_config('app.org_id', :o, true), set_config('app.user_id', :u, true)"),
                      {"o": str(tenant.org_id), "u": str(tenant.users["owner"])})
            assert c.execute(text("select app_org()")).scalar() == tenant.org_id
        with eng.begin() as c:  # same physical connection, next transaction
            assert c.execute(text("select current_setting('app.org_id', true)")).scalar() in ("", None)
            assert c.execute(text("select app_org()")).scalar() is None
    finally:
        eng.dispose()


def test_C3_security_definer_functions_pin_search_path_and_are_not_public():
    with owner_session() as s:
        rows = s.execute(text("""
            select p.oid::regprocedure::text, array_to_string(p.proconfig, ','),
                   has_function_privilege('public', p.oid, 'execute')
            from pg_proc p join pg_namespace n on n.oid = p.pronamespace
            where n.nspname = 'public' and p.prosecdef""")).all()
    bad_path = [f for f, cfg, _ in rows if "search_path=public, pg_temp" not in (cfg or "")]
    public_exec = [f for f, _, pub in rows if pub]
    assert bad_path == [], bad_path
    assert public_exec == [], public_exec


def test_C3_ambiguous_shipment_reference_resolves_to_no_tenant(tenant):
    from tests.conftest import make_tenant

    other = make_tenant()
    ref = f"BS-AMBIG-{uuid.uuid4().hex[:6]}"
    for t in (tenant, other):
        w = build_world(t)
        oid = _order(t, w, [("A1", 1, "0")])
        _pay_gateway(t, oid)
        dispatch_due()
        from app.modules import fulfillment
        from tests.world import principal

        (gid, _), = _owned_groups(oid)
        with tenant_session(t.org_id, principal(t, "packer").user_id) as s:
            from app.models.fulfillment import FulfillmentGroup

            fulfillment.create_shipment(s, s.get(FulfillmentGroup, gid), carrier="biteship", external_id=ref)
    from app.core.db import webhook_session

    with webhook_session() as s:
        assert s.execute(text("select organization_id from resolve_shipment(:r)"), {"r": ref}).scalar() is None
