"""Advertising cost accrual with effective-dated portfolio allocation (spec §9 policy 8, §15; acceptance #12).

* Spend for a campaign-day comes from ``daily_ad_insights`` (re-pulls overwrite the row; up to 28 days back).
* The allocation in force *on that day* splits it across portfolios; shares totalling < 1 leave the remainder
  **unallocated** (portfolio NULL) — never spread silently.
* Accrual is a running target: each run posts only the delta between target and what is already booked for
  (campaign, day, portfolio). Deltas for a day whose period is closed are booked today (open period) with
  ``event_time`` = the original day; closed periods are never modified (the DB trigger refuses it anyway).
* A policy change is a new version with ``effective_from``; history rows are end-dated, not edited. Changes effective in
  a closed period are refused.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.authz import Principal
from app.models.base import utcnow
from app.models.finance import AccountingPeriod, LedgerEntry
from app.models.marketing import AdCampaign, CampaignPortfolioAllocation, DailyAdInsight
from app.modules.finance import accounts as A
from app.modules.finance import posting
from app.modules.finance.ledger import JournalLine, post_journal

Z = Decimal("0")


def _closed(session: Session, legal_entity_id, d: date) -> bool:
    return session.scalar(select(func.count()).select_from(AccountingPeriod).where(
        AccountingPeriod.legal_entity_id == legal_entity_id, AccountingPeriod.status == "closed",
        AccountingPeriod.period_start <= d, AccountingPeriod.period_end >= d)) > 0


def allocations_on(session: Session, campaign_id: uuid.UUID, day: date) -> dict[uuid.UUID, Decimal]:
    rows = session.scalars(select(CampaignPortfolioAllocation).where(
        CampaignPortfolioAllocation.campaign_id == campaign_id, CampaignPortfolioAllocation.effective_from <= day,
        or_(CampaignPortfolioAllocation.effective_to.is_(None), CampaignPortfolioAllocation.effective_to > day))).all()
    return {r.portfolio_id: r.share for r in rows}


def set_allocation(session: Session, principal: Principal, campaign_id: uuid.UUID, shares: dict[uuid.UUID, Decimal],
                   effective_from: date, legal_entity_id: uuid.UUID) -> int:
    """New allocation version from ``effective_from``. Returns the new policy version."""
    principal.require("marketing.manage")
    if _closed(session, legal_entity_id, effective_from):
        raise HTTPException(status.HTTP_409_CONFLICT, "allocation changes cannot take effect in a closed period")
    if sum(shares.values(), Z) > 1 or any(s <= 0 for s in shares.values()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "shares must be > 0 and total at most 1")
    current = session.scalars(select(CampaignPortfolioAllocation).where(
        CampaignPortfolioAllocation.campaign_id == campaign_id,
        or_(CampaignPortfolioAllocation.effective_to.is_(None),
            CampaignPortfolioAllocation.effective_to > effective_from))).all()
    version = max((r.policy_version for r in current), default=0) + 1
    for r in current:
        if r.effective_from >= effective_from:
            raise HTTPException(status.HTTP_409_CONFLICT, "a later allocation version already exists")
        r.effective_to = effective_from
    campaign = session.get(AdCampaign, campaign_id)
    for pid, share in shares.items():
        session.add(CampaignPortfolioAllocation(organization_id=campaign.organization_id, campaign_id=campaign_id,
                                                portfolio_id=pid, method="fixed_share", share=share,
                                                effective_from=effective_from, policy_version=version))
    session.flush()
    audit(session, principal.org_id, "marketing.allocation_changed", "ad_campaign", campaign_id,
          actor_user_id=principal.user_id, after={"version": version, "effective_from": str(effective_from),
                                                  "shares": {str(k): str(v) for k, v in shares.items()}})
    return version


def _key(campaign_id, day: date, pid) -> str:
    return f"ad:{campaign_id}:{day}:{pid or 'unallocated'}"


def _booked(session: Session, key: str) -> tuple[Decimal, int]:
    rows = session.execute(select(func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0),
                                  func.count(func.distinct(LedgerEntry.journal_id))).where(
        LedgerEntry.source_type == "ad_spend_accrued", LedgerEntry.account_code == A.ADVERTISING,
        LedgerEntry.source_id.like(key + "%"))).one()
    return Decimal(rows[0]), int(rows[1])


def accrue_day(session: Session, org_id: uuid.UUID, legal_entity_id: uuid.UUID, campaign: AdCampaign, day: date) -> list:
    """Bring booked advertising for (campaign, day) to the current target; returns the deltas posted."""
    spend = Decimal(session.scalar(select(func.coalesce(func.sum(DailyAdInsight.spend), 0)).where(
        DailyAdInsight.platform_account_id == campaign.platform_account_id,
        DailyAdInsight.campaign_external_id == campaign.external_id, DailyAdInsight.report_date == day)))
    shares = allocations_on(session, campaign.id, day)
    pids = sorted(shares, key=str)
    parts = posting.split(spend, [shares[p] for p in pids] + [Decimal(1) - sum(shares.values(), Z)]) \
        if spend else [Z] * (len(pids) + 1)
    targets: dict = dict(zip([*pids, None], parts, strict=True))
    # Portfolios that held a share under an earlier version must be brought to zero too.
    for (prev_key,) in session.execute(select(func.distinct(LedgerEntry.source_id)).where(
            LedgerEntry.source_type == "ad_spend_accrued", LedgerEntry.source_id.like(f"ad:{campaign.id}:{day}:%"))):
        pid_s = prev_key.split(":")[3]
        pid = None if pid_s == "unallocated" else uuid.UUID(pid_s)
        targets.setdefault(pid, Z)
    closed = _closed(session, legal_entity_id, day)
    posted = []
    for pid, target in targets.items():
        key = _key(campaign.id, day, pid)
        booked, n = _booked(session, key)
        delta = target - booked
        if not delta:
            continue
        kw = {"portfolio_id": pid, "platform_account_id": campaign.platform_account_id}
        lines = ([JournalLine(A.ADVERTISING, debit=delta, **kw), JournalLine(A.ADS_PAYABLES, credit=delta, **kw)]
                 if delta > 0 else
                 [JournalLine(A.ADS_PAYABLES, debit=-delta, **kw), JournalLine(A.ADVERTISING, credit=-delta, **kw)])
        post_journal(session, org_id, source_type="ad_spend_accrued", source_id=f"{key}:r{n}", lines=lines,
                     legal_entity_id=legal_entity_id, accounting_date=utcnow().date() if closed else day,
                     event_time=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                     memo=("restatement of closed-period ad spend" if closed else None))
        posted.append((pid, delta, closed))
    return posted
