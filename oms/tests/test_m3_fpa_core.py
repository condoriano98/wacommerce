"""M3: contribution waterfall from the ledger (spec §15), effective-dated ad allocation (#12), snapshot immutability
after close (#13b). Activity is produced by the real services (F1-style order + ad accrual + opex)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

import app.registry  # noqa: F401
from app.core.db import tenant_session
from app.core.outbox import dispatch_due
from app.models.base import utcnow
from app.models.finance import LedgerEntry
from app.models.fpa import FpaSnapshot
from app.models.marketing import AdCampaign, DailyAdInsight
from app.modules import marketing
from app.modules.finance import accounts as A
from app.modules.finance import periods, posting
from app.modules.fpa import snapshots, waterfall
from tests.conftest import owner_session
from tests.test_accounting_services import _order, _pay_gateway, _ship_owned
from tests.world import D, build_world, principal


def _campaign(t, w, shares: dict, ext="CMP-1") -> AdCampaign:
    with owner_session() as s:
        c = AdCampaign(organization_id=t.org_id, platform_account_id=w.wa_account, provider="meta", external_id=ext,
                       name="CTWA test")
        s.add(c)
        s.flush()
        cid = c.id
    owner = principal(t, "owner")
    with tenant_session(t.org_id, owner.user_id) as s:
        marketing.set_allocation(s, owner, cid, shares, date(2000, 1, 1), t.legal_entity_id)
    return cid


def _insight(t, w, day: date, spend: str, ext="CMP-1") -> None:
    with owner_session() as s:
        row = s.scalars(select(DailyAdInsight).where(DailyAdInsight.platform_account_id == w.wa_account,
                                                     DailyAdInsight.report_date == day,
                                                     DailyAdInsight.ad_external_id == f"{ext}-ad")).first()
        if row is None:
            s.add(DailyAdInsight(organization_id=t.org_id, platform_account_id=w.wa_account, provider="meta",
                                 report_date=day, campaign_external_id=ext, ad_external_id=f"{ext}-ad",
                                 spend=D(spend), pulled_at=utcnow()))
        else:  # 28-day re-pull revises spend in place
            row.spend, row.pulled_at = D(spend), utcnow()


def _accrue(t, cid, day):
    owner = principal(t, "owner")
    with tenant_session(t.org_id, owner.user_id) as s:
        return marketing.accrue_day(s, t.org_id, t.legal_entity_id, s.get(AdCampaign, cid), day)


def test_waterfall_ties_to_ledger_and_keeps_unallocated_separate(tenant):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 2, "0.05")])
    _pay_gateway(tenant, oid, fee=D("1435"))
    dispatch_due()
    _ship_owned(tenant, oid)
    cid = _campaign(tenant, w, {tenant.portfolios["A"]: D("0.6"), tenant.portfolios["B"]: D("0.3")})
    today = utcnow().date()
    _insight(tenant, w, today, "100000")
    _accrue(tenant, cid, today)
    owner = principal(tenant, "owner")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        posting.expense(s, tenant.org_id, account=A.OPEX, amount=D("50000"), source_type="opex", source_id="rent-1",
                        portfolio_id=None, order_id=None, credit_account=A.CASH, legal_entity_id=tenant.legal_entity_id)
    start, end = periods.month_bounds(today)
    with tenant_session(tenant.org_id, owner.user_id) as s:
        tie = waterfall.tie_out(s, start, end, tenant.legal_entity_id)
        assert tie["ok"], tie
        l3 = waterfall.compute(s, start, end, legal_entity_id=tenant.legal_entity_id)
        assert l3.by_level["gross_sales"] == D("205000")  # F1: goods 190,000 + shipping income 15,000
        assert l3.by_level["cm2"] == D("-100000")
        assert l3.unallocated == {"cm2": D("-10000"), "opex": D("-50000")}  # 10% unallocated ads, shared opex
        l2 = waterfall.by_portfolio(s, start, end, tenant.legal_entity_id)
        assert l2[tenant.portfolios["A"]].by_level["cm2"] == D("-60000")
        # L2 + unallocated = L3, per level
        for lv in waterfall.LEVELS:
            total = sum((wf.by_level[lv] for wf in l2.values()), D("0")) + l3.unallocated.get(lv, D("0"))
            assert total == l3.by_level[lv], lv
        rows = waterfall.l1(s, start, end, tenant.legal_entity_id)
        assert any(r["channel"] == "whatsapp" and D(r["gross_sales"]) > 0 for r in rows)
        assert waterfall.unmapped_pnl_accounts(s) == []
    # Portfolio B manager sees only B (RLS): no A revenue, no unallocated lines.
    with tenant_session(tenant.org_id, tenant.users["pm_b"]) as s:
        b = waterfall.compute(s, start, end, legal_entity_id=tenant.legal_entity_id)
        assert b.by_level["gross_sales"] == 0 and b.unallocated == {} and b.by_level["cm2"] == D("-30000")


def test_12_allocation_change_is_effective_dated_and_13b_snapshots_survive_later_changes(tenant):
    w = build_world(tenant)
    pa, pb = tenant.portfolios["A"], tenant.portfolios["B"]
    cid = _campaign(tenant, w, {pa: D("0.5"), pb: D("0.5")})
    today = utcnow().date()
    last_month = periods.month_bounds(today)[0] - timedelta(days=20)
    lm_start, lm_end = periods.month_bounds(last_month)
    _insight(tenant, w, last_month, "100000")
    _insight(tenant, w, today, "40000")
    _accrue(tenant, cid, last_month)
    _accrue(tenant, cid, today)

    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        _, snaps = snapshots.close_and_snapshot(s, fin, tenant.legal_entity_id, lm_start)
        v1 = {(x.scope_type, x.portfolio_id): (x.id, x.sha256, x.payload) for x in snaps}
    with owner_session() as s:
        closed_lines = s.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.organization_id == tenant.org_id, LedgerEntry.accounting_date.between(lm_start, lm_end)))

    owner = principal(tenant, "owner")
    with pytest.raises(HTTPException):  # cannot change history inside a closed period
        with tenant_session(tenant.org_id, owner.user_id) as s:
            marketing.set_allocation(s, owner, cid, {pa: D("1")}, last_month, tenant.legal_entity_id)
    with tenant_session(tenant.org_id, owner.user_id) as s:
        assert marketing.set_allocation(s, owner, cid, {pa: D("0.25"), pb: D("0.75")}, today,
                                        tenant.legal_entity_id) == 2
    _accrue(tenant, cid, today)  # today's spend re-split: deltas in the open period
    _insight(tenant, w, last_month, "110000")  # 28-day re-pull revises a closed day
    deltas = _accrue(tenant, cid, last_month)
    assert deltas and all(closed for _, _, closed in deltas)  # booked today, not in the closed period

    with owner_session() as s:
        assert s.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.organization_id == tenant.org_id,
            LedgerEntry.accounting_date.between(lm_start, lm_end))) == closed_lines
        today_a = posting._balance(s, A.ADVERTISING, organization_id=tenant.org_id, portfolio_id=pa,
                                   accounting_date=today)
        # today's 40,000 at 25% = 10,000, plus the closed day's +10,000 re-pull at the closed-day policy (50%) = 5,000
        assert today_a == D("15000")

    # #13b: the close snapshot is unchanged and cannot be edited or deleted, even by the schema owner.
    with tenant_session(tenant.org_id, fin.user_id) as s:
        for (scope, pid), (sid, sha, payload) in v1.items():
            snap = s.get(FpaSnapshot, sid)
            assert (snap.sha256, snap.payload, snap.version) == (sha, payload, 1)
        recomputed = waterfall.compute(s, lm_start, lm_end, legal_entity_id=tenant.legal_entity_id).as_dict()
        assert recomputed == v1[("legal_entity", None)][2]["waterfall"]
    for stmt in ("update fpa_snapshots set payload = '{}' where id = :i", "delete from fpa_snapshots where id = :i"):
        with pytest.raises(DBAPIError):
            with owner_session() as s:
                s.execute(text(stmt), {"i": v1[("legal_entity", None)][0]})
    # A restatement is a new version, never an edit.
    with tenant_session(tenant.org_id, fin.user_id) as s:
        (v2,) = [x for x in snapshots.snapshot(s, fin, tenant.legal_entity_id, lm_start, reason="restatement review")
                 if x.scope_type == "legal_entity"]
        assert v2.version == 2 and v2.sha256 == v1[("legal_entity", None)][1]
    # Portfolio B users see only their own portfolio snapshot.
    with tenant_session(tenant.org_id, tenant.users["pm_b"]) as s:
        seen = {(x.scope_type, x.portfolio_id) for x in s.scalars(select(FpaSnapshot))}
        assert seen == {("portfolio", pb)}
