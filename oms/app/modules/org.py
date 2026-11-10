"""Organizations, portfolios, budgets, risk policies and capital allocation."""

import uuid
from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.authz import Principal
from app.models.base import utcnow
from app.models.org import CapitalAllocation, CapitalScenario, Portfolio, PortfolioBudget, RiskPolicy
from app.modules import approvals
from app.modules.finance import accounts as acct
from app.modules.finance.ledger import JournalLine, post_journal

DEFAULT_RISK_POLICIES = [
    # key, threshold, comparator, severity (editable scenario inputs, not benchmarks)
    ("min_cash_reserve", Decimal("12000000"), "lt", "critical"),
    ("platform_concentration_max", Decimal("0.35"), "gt", "warning"),
    ("operating_margin_min", Decimal("0.05"), "lt", "warning"),
    ("roas_below_break_even", Decimal("1.0"), "lt", "warning"),
    ("supplier_price_increase_max", Decimal("0.10"), "gt", "warning"),
    ("return_rate_max", Decimal("0.08"), "gt", "warning"),
    ("settlement_delay_days_max", Decimal("14"), "gt", "warning"),
    ("inventory_days_max", Decimal("60"), "gt", "warning"),
]

ILLUSTRATIVE_SCENARIO = {
    "advertising": 27_000_000,
    "inventory_and_supplier_float": 45_000_000,
    "fulfillment_working_capital": 10_000_000,
    "software_salaries_opex": 6_000_000,
    "liquidity_reserve": 12_000_000,
}


def create_portfolio(session: Session, principal: Principal, **fields) -> Portfolio:
    principal.require("portfolio.manage")
    portfolio = Portfolio(organization_id=principal.org_id, **fields)
    session.add(portfolio)
    session.flush()
    audit(session, principal.org_id, "portfolio.created", "portfolio", portfolio.id,
          actor_user_id=principal.user_id, portfolio_id=portfolio.id, after=fields)
    return portfolio


def seed_org_defaults(session: Session, org_id: uuid.UUID) -> None:
    for key, threshold, comparator, severity in DEFAULT_RISK_POLICIES:
        session.add(RiskPolicy(organization_id=org_id, portfolio_id=None, key=key, threshold=threshold,
                               comparator=comparator, severity=severity))
    session.add(CapitalScenario(organization_id=org_id, name="Illustrative Rp100M plan",
                                total_capital=Decimal("100000000"), allocations=ILLUSTRATIVE_SCENARIO,
                                is_active=True))


def set_budget(session: Session, principal: Principal, portfolio_id: uuid.UUID, period_start: date,
               period_end: date, category: str, amount: Decimal, provider: str | None = None) -> PortfolioBudget:
    principal.require("budget.manage", portfolio_id)
    budget = session.scalars(select(PortfolioBudget).where(
        PortfolioBudget.portfolio_id == portfolio_id, PortfolioBudget.period_start == period_start,
        PortfolioBudget.category == category,
        PortfolioBudget.provider.is_(None) if provider is None else PortfolioBudget.provider == provider,
    )).first()
    before = {"amount": budget.amount} if budget else None
    if budget is None:
        budget = PortfolioBudget(organization_id=principal.org_id, portfolio_id=portfolio_id,
                                 period_start=period_start, period_end=period_end, category=category,
                                 provider=provider, amount=amount)
        session.add(budget)
    else:
        budget.amount = amount
    audit(session, principal.org_id, "budget.set", "portfolio_budget", budget.id, actor_user_id=principal.user_id,
          portfolio_id=portfolio_id, before=before, after={"category": category, "amount": amount})
    return budget


def override_risk_threshold(session: Session, principal: Principal, policy_id: uuid.UUID,
                            threshold: Decimal, reason: str) -> RiskPolicy:
    principal.require("finance.manage")
    policy = session.get(RiskPolicy, policy_id)
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "policy not found")
    before = {"threshold": policy.threshold}
    policy.threshold = threshold
    audit(session, principal.org_id, "risk_policy.override", "risk_policy", policy.id,
          actor_user_id=principal.user_id, portfolio_id=policy.portfolio_id, before=before,
          after={"threshold": threshold, "reason": reason})
    return policy


def request_capital_reallocation(session: Session, principal: Principal, from_portfolio_id: uuid.UUID | None,
                                 to_portfolio_id: uuid.UUID, amount: Decimal, reason: str):
    principal.require("portfolio.read", to_portfolio_id)
    return approvals.request_approval(
        session, principal, kind="capital_reallocation", subject_type="portfolio",
        subject_id=str(to_portfolio_id), amount=amount,
        payload={"from_portfolio_id": str(from_portfolio_id) if from_portfolio_id else None,
                 "to_portfolio_id": str(to_portfolio_id), "reason": reason},
        portfolio_id=None, idempotency_key=f"capital:{from_portfolio_id}:{to_portfolio_id}:{amount}:{reason}",
    )


def execute_capital_reallocation(session: Session, principal: Principal, approval_id: uuid.UUID) -> CapitalAllocation:
    """Moves capital between portfolios as paired journals that net to zero on consolidation."""
    from app.models.governance import HumanApproval

    pending = session.get(HumanApproval, approval_id)
    if pending is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    approval = approvals.require_approved(session, approval_id, "capital_reallocation", pending.subject_id)
    p = approval.payload
    from_id = uuid.UUID(p["from_portfolio_id"]) if p.get("from_portfolio_id") else None
    to_id = uuid.UUID(p["to_portfolio_id"])
    amount = approval.amount
    if from_id is not None:
        available = portfolio_cash(session, from_id)
        if available < amount:
            raise HTTPException(status.HTTP_409_CONFLICT, f"source portfolio cash {available} < {amount}")
    if from_id is not None:
        # Paired journals: cash moves between portfolios; 3100 nets to zero on consolidation.
        lines = [
            JournalLine(acct.CASH, debit=amount, portfolio_id=to_id),
            JournalLine(acct.INTER_PORTFOLIO_CAPITAL, credit=amount, portfolio_id=to_id),
            JournalLine(acct.INTER_PORTFOLIO_CAPITAL, debit=amount, portfolio_id=from_id),
            JournalLine(acct.CASH, credit=amount, portfolio_id=from_id),
        ]
        source_type = "capital_transfer"
    else:
        lines = [
            JournalLine(acct.CASH, debit=amount, portfolio_id=to_id),
            JournalLine(acct.OWNER_CAPITAL, credit=amount, portfolio_id=to_id),
        ]
        source_type = "capital_injection"
    journal_id = post_journal(session, principal.org_id, source_type=source_type, source_id=str(approval.id),
                              memo=p.get("reason"), lines=lines)
    allocation = CapitalAllocation(organization_id=principal.org_id, from_portfolio_id=from_id, to_portfolio_id=to_id,
                                   amount=amount, reason=p.get("reason", ""), approval_id=approval.id,
                                   journal_id=journal_id, effective_at=utcnow())
    session.add(allocation)
    audit(session, principal.org_id, "capital.reallocated", "capital_allocation", allocation.id,
          actor_user_id=principal.user_id, after={"from": from_id, "to": to_id, "amount": amount})
    return allocation


def portfolio_cash(session: Session, portfolio_id: uuid.UUID) -> Decimal:
    from app.models.finance import LedgerEntry

    total = session.scalar(
        select(func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0)).where(
            LedgerEntry.portfolio_id == portfolio_id, LedgerEntry.account_code == acct.CASH
        )
    )
    return Decimal(total)
