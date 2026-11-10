import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from app.core.db import tenant_session
from app.core.outbox import HANDLERS, dispatch_due, enqueue
from app.models.finance import LedgerEntry
from app.models.governance import OutboxMessage
from app.models.org import Portfolio
from app.modules.finance import accounts as acct
from app.modules.finance.ledger import JournalLine, post_journal
from tests.conftest import make_tenant


def _visible_portfolio_codes(t, user):
    with tenant_session(t.org_id, t.users[user]) as s:
        return sorted(s.scalars(select(Portfolio.code)).all())


def test_rls_portfolio_isolation_is_enforced_by_postgres(tenant):
    assert _visible_portfolio_codes(tenant, "owner") == ["A", "B", "C"]
    assert _visible_portfolio_codes(tenant, "pm_a") == ["A"]
    assert _visible_portfolio_codes(tenant, "pm_b") == ["B"]
    # Raw SQL by Portfolio-A manager against Portfolio-B returns nothing — no app filter involved.
    with tenant_session(tenant.org_id, tenant.users["pm_a"]) as s:
        n = s.execute(text("select count(*) from portfolios where id = :b"), {"b": tenant.portfolios["B"]}).scalar()
        assert n == 0


def test_rls_blocks_other_organizations():
    t1, t2 = make_tenant(), make_tenant()
    with tenant_session(t1.org_id, t1.users["owner"]) as s:
        n = s.execute(text("select count(*) from portfolios where organization_id = :o"), {"o": t2.org_id}).scalar()
        assert n == 0
    # Spoofing another org id with a user who has no role there sees nothing portfolio-scoped.
    with tenant_session(t2.org_id, t1.users["owner"]) as s:
        assert s.scalars(select(Portfolio)).all() == []


def test_portfolio_user_cannot_insert_into_foreign_portfolio(tenant):
    with pytest.raises(ProgrammingError):
        with tenant_session(tenant.org_id, tenant.users["pm_a"]) as s:
            s.execute(text(
                "insert into portfolio_budgets (id, organization_id, created_at, portfolio_id, period_start, "
                "period_end, category, amount) values (gen_random_uuid(), :o, now(), :p, '2026-11-01', "
                "'2026-11-30', 'advertising', 1000)"), {"o": tenant.org_id, "p": tenant.portfolios["B"]})


def test_app_role_has_no_access_to_credential_table(tenant):
    with pytest.raises(ProgrammingError):
        with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
            s.execute(text("select * from account_credentials"))


def test_credentials_api_stores_encrypted_and_never_returns_secret(tenant, client):
    r = client.post("/platform-accounts", headers=tenant.headers("owner"), json={
        "provider": "doku", "provider_account_id": "MCH-1", "display_name": "DOKU main", "role": "payments",
        "portfolio_ids": [str(tenant.portfolios["A"])]})
    assert r.status_code == 201, r.text
    account_id = r.json()["id"]
    r = client.put(f"/platform-accounts/{account_id}/credentials", headers=tenant.headers("owner"),
                   json={"name": "secret_key", "secret": "SK-super-secret"})
    assert r.status_code == 204, r.text
    r = client.put(f"/platform-accounts/{account_id}/credentials", headers=tenant.headers("pm_a"),
                   json={"name": "secret_key", "secret": "x"})
    assert r.status_code == 403
    listed = client.get(f"/platform-accounts/{account_id}/credentials", headers=tenant.headers("owner")).json()
    assert listed[0]["name"] == "secret_key" and "SK-super-secret" not in str(listed)
    from app.modules.platforms import read_secret

    assert read_secret(tenant.org_id, uuid.UUID(account_id), "secret_key") == "SK-super-secret"
    logs = client.get("/audit-logs", headers=tenant.headers("owner")).json()
    assert "SK-super-secret" not in str(logs)


def test_unbalanced_journal_rejected_by_database(tenant):
    with pytest.raises((IntegrityError, DBAPIError)):
        with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
            s.execute(text(
                "insert into ledger_entries (id, organization_id, created_at, journal_id, line_no, account_code, "
                "debit, credit, currency, accounting_date, source_type, source_id) values "
                "(gen_random_uuid(), :o, now(), gen_random_uuid(), 1, '1000', 100, 0, 'IDR', current_date, 't', 't')"),
                {"o": tenant.org_id})


def test_ledger_is_append_only_for_app_role(tenant):
    with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
        post_journal(s, tenant.org_id, source_type="test", source_id="1", lines=[
            JournalLine(acct.CASH, debit=Decimal("10"), portfolio_id=tenant.portfolios["A"]),
            JournalLine(acct.OWNER_CAPITAL, credit=Decimal("10"), portfolio_id=tenant.portfolios["A"])])
    with pytest.raises(ProgrammingError):
        with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
            s.execute(text("update ledger_entries set debit = 0"))


def test_approval_separation_of_duties_and_capital_reallocation(tenant, client):
    # Seed portfolio A with cash via an approved injection.
    r = client.post("/capital/reallocations", headers=tenant.headers("owner"), json={
        "to_portfolio_id": str(tenant.portfolios["A"]), "amount": "50000000", "reason": "initial funding"})
    approval_id = r.json()["approval_id"]
    # Requester cannot approve their own request.
    r = client.post(f"/approvals/{approval_id}/decision", headers=tenant.headers("owner"),
                    json={"approve": True, "reason": "self"})
    assert r.status_code == 403
    r = client.post(f"/approvals/{approval_id}/decision", headers=tenant.headers("owner2"),
                    json={"approve": True, "reason": "ok"})
    assert r.status_code == 200, r.text
    assert client.post(f"/capital/reallocations/{approval_id}/execute",
                       headers=tenant.headers("owner")).status_code == 200
    # Executing twice is refused (approval consumed).
    assert client.post(f"/capital/reallocations/{approval_id}/execute",
                       headers=tenant.headers("owner")).status_code == 409

    # Move Rp10M from A to B.
    r = client.post("/capital/reallocations", headers=tenant.headers("owner"), json={
        "from_portfolio_id": str(tenant.portfolios["A"]), "to_portfolio_id": str(tenant.portfolios["B"]),
        "amount": "10000000", "reason": "scale B"})
    aid = r.json()["approval_id"]
    # Finance manager may approve capital (tier 0 requires owner) -> refused; second owner approves.
    assert client.post(f"/approvals/{aid}/decision", headers=tenant.headers("finance"),
                       json={"approve": True, "reason": "ok"}).status_code == 403
    assert client.post(f"/approvals/{aid}/decision", headers=tenant.headers("owner2"),
                       json={"approve": True, "reason": "ok"}).status_code == 200
    assert client.post(f"/capital/reallocations/{aid}/execute", headers=tenant.headers("owner")).status_code == 200

    from app.modules.org import portfolio_cash

    with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
        assert portfolio_cash(s, tenant.portfolios["A"]) == Decimal("40000000")
        assert portfolio_cash(s, tenant.portfolios["B"]) == Decimal("10000000")
        # Inter-portfolio capital nets to zero on consolidation.
        net = s.execute(text("select coalesce(sum(debit - credit), 0) from ledger_entries "
                             "where account_code = '3100'")).scalar()
        assert net == 0
    # Portfolio-B manager cannot see Portfolio-A ledger lines.
    with tenant_session(tenant.org_id, tenant.users["pm_b"]) as s:
        rows = s.scalars(select(LedgerEntry.portfolio_id).distinct()).all()
        assert set(rows) == {tenant.portfolios["B"]}


def test_outbox_dedupes_and_retries(tenant):
    calls = []

    def flaky(session, msg):
        calls.append(msg.payload["n"])
        if len(calls) == 1:
            raise RuntimeError("transient")

    HANDLERS["test.flaky"] = flaky
    with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
        enqueue(s, tenant.org_id, "test.flaky", f"k-{tenant.org_id}", {"n": 1})
        enqueue(s, tenant.org_id, "test.flaky", f"k-{tenant.org_id}", {"n": 2})  # duplicate ignored
    dispatch_due()
    from datetime import timedelta

    from app.models.base import utcnow

    dispatch_due(now=utcnow() + timedelta(hours=2))
    from app.core.db import system_session

    with system_session() as s:
        msg = s.scalars(select(OutboxMessage).where(OutboxMessage.organization_id == tenant.org_id)).one()
        assert msg.status == "done" and msg.attempts == 2
    assert calls == [1, 1]


def test_login_flow(tenant, client):
    with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
        email = s.execute(text("select email from users where id = :u"), {"u": tenant.users["pm_a"]}).scalar()
    r = client.post("/auth/login", json={"email": email, "password": "pw-pm_a"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    codes = [p["code"] for p in client.get("/portfolios", headers={"Authorization": f"Bearer {token}"}).json()]
    assert codes == ["A"]
    assert client.post("/auth/login", json={"email": email, "password": "wrong"}).status_code == 401


def test_failed_commit_is_not_reported_as_success(tenant):
    """A deferred constraint failure at commit must surface as an error, not a 200."""
    from fastapi.testclient import TestClient

    from app.core.deps import DbSession
    from app.main import app

    @app.post("/_test/unbalanced")
    def _unbalanced(session: DbSession) -> dict:
        session.execute(text(
            "insert into ledger_entries (id, organization_id, created_at, journal_id, line_no, account_code, "
            "debit, credit, currency, accounting_date, source_type, source_id) values "
            "(gen_random_uuid(), app_org(), now(), gen_random_uuid(), 1, '1000', 5, 0, 'IDR', current_date, 't', 't')"))
        return {"ok": True}

    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/_test/unbalanced", headers=tenant.headers("owner"))
    assert r.status_code == 500
