"""FP&A API: RLS-scoped waterfall, stress runs with alerts, cash forecast, bulk-buy, allocation, close + snapshots."""

from datetime import date

import app.registry  # noqa: F401
from app.core.outbox import dispatch_due
from tests.test_accounting_services import _order, _pay_gateway, _ship_owned
from tests.world import build_world


def test_fpa_api_end_to_end(tenant, client):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    _ship_owned(tenant, oid)
    h = tenant.headers("owner")
    r = client.get("/fpa/waterfall", headers=h, params={"legal_entity_id": str(tenant.legal_entity_id)})
    assert r.status_code == 200 and float(r.json()["gross_sales"]) > 0
    assert client.get("/fpa/waterfall", headers=tenant.headers("pm_b")).json()["gross_sales"] == "0"
    assert client.get("/fpa/waterfall", headers=h, params={"level": "L1"}).json()["rows"]
    tie = client.get("/fpa/tie-out", headers=tenant.headers("finance"),
                     params={"legal_entity_id": str(tenant.legal_entity_id)}).json()
    assert tie["ok"], tie
    assert client.get("/fpa/tie-out", headers=tenant.headers("pm_a"),
                      params={"legal_entity_id": str(tenant.legal_entity_id)}).status_code == 403
    sc = client.get("/fpa/simulations/scenarios", headers=h).json()
    assert len(sc["scenarios"]) == 12 and sc["monte_carlo_enabled"] is False
    st = client.post("/fpa/simulations/stress", headers=h, json={"scenarios": ["S4_cod_delay_+14d"]}).json()
    assert "S4_cod_delay_+14d" in st
    assert client.post("/fpa/simulations/stress", headers=h, json={"scenarios": ["S99"]}).status_code == 422
    assert len(client.get("/fpa/cash-forecast", headers=h).json()["weeks"]) == 13
    bb = client.post("/fpa/bulk-buy/evaluate", headers=h, json={
        "weekly_demand": [20, 21, 19, 20], "qty": 60, "dropship_unit_cost": "100000", "bulk_unit_cost": "80000",
        "carrying_cost_per_unit_week": "500", "max_holding_weeks": 6, "supplier_reliability": "0.98",
        "return_rate": "0.03", "salvage_rate": "0.5", "post_purchase_min_cash": "30000000",
        "reserve": "15000000", "risk_limit": "2000000"})
    assert bb.json()["recommend"] is True
    # Close last month (no activity is fine) → snapshots v1; second close refused.
    last = date(date.today().year - (date.today().month == 1), (date.today().month - 2) % 12 + 1, 1)
    c = client.post(f"/fpa/periods/{tenant.legal_entity_id}/{last}/close", headers=tenant.headers("finance"))
    assert c.status_code == 200 and c.json()["snapshots"][0]["version"] == 1
    assert client.post(f"/fpa/periods/{tenant.legal_entity_id}/{last}/close",
                       headers=tenant.headers("finance")).status_code == 409
    assert client.get("/fpa/snapshots", headers=h, params={"period_start": str(last)}).json()[0]["version"] == 1


def test_reporting_login_reads_waterfall_view_for_its_org_only(tenant):
    import psycopg
    from sqlalchemy import text

    from app.models.org import RoleAssignment, User
    from tests.conftest import TEST_DB, make_tenant, owner_session

    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    _ship_owned(tenant, oid)
    other = make_tenant()
    ow = build_world(other)
    oo = _order(other, ow, [("A1", 1, "0")])
    _pay_gateway(other, oo)
    dispatch_due()
    _ship_owned(other, oo)
    role = f"rpt_{tenant.org_id.hex[:10]}w"
    with owner_session() as s:
        u = User(organization_id=tenant.org_id, email=f"{role}@oms.internal", name="reporting", is_service=True)
        s.add(u)
        s.flush()
        s.add(RoleAssignment(organization_id=tenant.org_id, user_id=u.id, role="auditor", scope_type="organization",
                             scope_id=None))
        s.flush()
        s.execute(text("select provision_reporting_principal(:r, 'rpt-pw', :o, :u)"),
                  {"r": role, "o": tenant.org_id, "u": u.id})
    with psycopg.connect(f"host=localhost port=5432 dbname={TEST_DB} user={role} password=rpt-pw",
                         autocommit=True) as c:
        rows = c.execute("select organization_id, level, sum(contribution) from reporting.monthly_waterfall "
                         "group by 1, 2").fetchall()
        assert rows and {r[0] for r in rows} == {tenant.org_id}
        assert any(r[1] == "gross_sales" and r[2] > 0 for r in rows)


def test_dashboard_read_endpoints_respect_rls(tenant, client):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0"), ("B1", 1, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    owner = client.get("/orders", headers=tenant.headers("owner")).json()
    mine = next(o for o in owner if o["order_id"] == str(oid))
    assert mine["lines"] == 2
    pm_a = next(o for o in client.get("/orders", headers=tenant.headers("pm_a")).json() if o["order_id"] == str(oid))
    assert pm_a["lines"] == 1  # slice: only portfolio A's line, no header totals
    assert client.get("/exceptions", headers=tenant.headers("owner")).status_code == 200
    caps = client.get("/integrations/capabilities", headers=tenant.headers("owner")).json()
    assert caps and not any(c["operationally_ready"] for c in caps)  # nothing is production-verified
    r = client.put(f"/portfolios/{tenant.portfolios['A']}/mandates", headers=tenant.headers("owner"), json={
        "category": "advertising", "amount": "27000000", "effective_from": "2026-11-01", "reason": "plan"})
    assert r.status_code == 200, r.text
    cap = {c["code"]: c for c in client.get("/capital/summary", headers=tenant.headers("owner")).json()}
    assert cap["A"]["categories"]["advertising"]["mandate"].startswith("27000000")
    assert set(c["code"] for c in client.get("/capital/summary", headers=tenant.headers("pm_b")).json()) == {"B"}
