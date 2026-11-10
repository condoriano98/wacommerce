"""Capital management ledger (spec §10, ADR-009, acceptance #16).

Mandates are management budgets per portfolio/category; reallocating them changes mandates only. No GL journal is
ever created here — real money moves only between legal-entity bank accounts (intercompany journals).
"""

import uuid
from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.authz import Principal
from app.models.base import utcnow
from app.models.capital import CapitalAllocationEvent, CapitalCommitment, CommitmentConsumption
from app.models.governance import HumanApproval
from app.modules import approvals

OPEN = ("proposed", "approved", "partially_consumed")


def mandate(session: Session, portfolio_id, category: str, as_of: date | None = None) -> Decimal:
    stmt = select(func.coalesce(func.sum(CapitalAllocationEvent.amount), 0)).where(
        CapitalAllocationEvent.portfolio_id == portfolio_id, CapitalAllocationEvent.category == category,
        CapitalAllocationEvent.effective_from <= (as_of or utcnow().date()))
    return Decimal(session.scalar(stmt))


def has_mandate(session: Session, portfolio_id, category: str) -> bool:
    return session.scalar(select(func.count()).select_from(CapitalAllocationEvent).where(
        CapitalAllocationEvent.portfolio_id == portfolio_id, CapitalAllocationEvent.category == category)) > 0


def committed(session: Session, portfolio_id, category: str) -> tuple[Decimal, Decimal]:
    """(outstanding open commitments, consumed to date)."""
    rows = session.execute(select(
        func.coalesce(func.sum(CapitalCommitment.amount - CapitalCommitment.consumed_amount).filter(
            CapitalCommitment.status.in_(OPEN)), 0),
        func.coalesce(func.sum(CapitalCommitment.consumed_amount), 0)).where(
        CapitalCommitment.portfolio_id == portfolio_id, CapitalCommitment.category == category)).one()
    return Decimal(rows[0]), Decimal(rows[1])


def available(session: Session, portfolio_id, category: str) -> Decimal:
    outstanding, consumed = committed(session, portfolio_id, category)
    return mandate(session, portfolio_id, category) - outstanding - consumed


def set_mandate(session: Session, principal: Principal, portfolio_id, category: str, amount: Decimal,
                effective_from: date, reason: str) -> CapitalAllocationEvent:
    principal.require("budget.manage", portfolio_id)
    delta = amount - mandate(session, portfolio_id, category, effective_from)
    ev = CapitalAllocationEvent(organization_id=principal.org_id, portfolio_id=portfolio_id, category=category,
                                event_type="increase" if delta >= 0 else "decrease", amount=delta,
                                effective_from=effective_from, reason=reason, actor_user_id=principal.user_id)
    session.add(ev)
    audit(session, principal.org_id, "capital.mandate_set", "portfolio", portfolio_id, actor_user_id=principal.user_id,
          portfolio_id=portfolio_id, after={"category": category, "amount": amount, "delta": delta})
    return ev


def request_reallocation(session: Session, principal: Principal, *, from_portfolio_id, to_portfolio_id,
                         category: str, amount: Decimal, reason: str) -> HumanApproval:
    principal.require("portfolio.read", to_portfolio_id)
    if amount <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "amount must be positive")
    return approvals.request_approval(
        session, principal, kind="capital_reallocation", subject_type="portfolio", subject_id=str(to_portfolio_id),
        amount=amount, portfolio_id=None,
        payload={"from_portfolio_id": str(from_portfolio_id) if from_portfolio_id else None,
                 "to_portfolio_id": str(to_portfolio_id), "category": category, "reason": reason},
        idempotency_key=f"capital:{from_portfolio_id}:{to_portfolio_id}:{category}:{amount}:{reason}")


def execute_reallocation(session: Session, principal: Principal, approval_id: uuid.UUID) -> list[CapitalAllocationEvent]:
    pending = session.get(HumanApproval, approval_id)
    if pending is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    approval = approvals.require_approved(session, approval_id, "capital_reallocation", pending.subject_id)
    p = approval.payload
    src = uuid.UUID(p["from_portfolio_id"]) if p.get("from_portfolio_id") else None
    dst = uuid.UUID(p["to_portfolio_id"])
    cat, amount, today = p["category"], approval.amount, utcnow().date()
    out = []
    if src is not None:
        free = available(session, src, cat)
        if free < amount:
            raise HTTPException(status.HTTP_409_CONFLICT, f"source mandate available {free} < {amount}")
        out.append(CapitalAllocationEvent(organization_id=principal.org_id, portfolio_id=src, category=cat,
                                          event_type="reallocate_out", amount=-amount, effective_from=today,
                                          approval_id=approval.id, counterpart_portfolio_id=dst, reason=p["reason"],
                                          actor_user_id=principal.user_id))
    out.append(CapitalAllocationEvent(organization_id=principal.org_id, portfolio_id=dst, category=cat,
                                      event_type="reallocate_in" if src else "increase", amount=amount,
                                      effective_from=today, approval_id=approval.id, counterpart_portfolio_id=src,
                                      reason=p["reason"], actor_user_id=principal.user_id))
    session.add_all(out)
    audit(session, principal.org_id, "capital.reallocated", "human_approval", approval.id,
          actor_user_id=principal.user_id, after={"from": src, "to": dst, "category": cat, "amount": amount})
    return out


def commit(session: Session, org_id, *, portfolio_id, category: str, purpose: str, source_type: str, source_id: str,
           amount: Decimal, expected_payment_date: date | None = None) -> CapitalCommitment:
    """Reserve mandate for a purpose. Approved when no mandate is configured (tracking only) or it fits; otherwise
    'proposed' and an exception case is opened for the portfolio manager."""
    existing = session.scalars(select(CapitalCommitment).where(
        CapitalCommitment.source_type == source_type, CapitalCommitment.source_id == source_id,
        CapitalCommitment.status.in_(OPEN))).first()
    if existing:
        return existing
    fits = not has_mandate(session, portfolio_id, category) or available(session, portfolio_id, category) >= amount
    from app.models.org import Brand, Portfolio

    portfolio = session.get(Portfolio, portfolio_id)
    brand = session.get(Brand, portfolio.brand_id) if portfolio is not None else None
    c = CapitalCommitment(organization_id=org_id, portfolio_id=portfolio_id, category=category, purpose=purpose,
                          source_type=source_type, source_id=source_id, amount=amount,
                          legal_entity_id=brand.legal_entity_id if brand else None,
                          expected_payment_date=expected_payment_date,
                          status="approved" if fits else "proposed")
    session.add(c)
    session.flush()
    if not fits:
        from app.modules import exceptions

        exceptions.open_case(session, org_id, "manual_review", subject_type="capital_commitment",
                             subject_id=str(c.id), portfolio_id=portfolio_id,
                             context={"reason": "commitment exceeds available mandate", "amount": str(amount)})
    return c


def consume(session: Session, commitment_id, amount: Decimal, journal_id=None) -> CapitalCommitment:
    c = session.get(CapitalCommitment, commitment_id, with_for_update=True)
    take = min(amount, c.amount - c.consumed_amount)
    if take > 0:
        c.consumed_amount += take
        session.add(CommitmentConsumption(organization_id=c.organization_id, portfolio_id=c.portfolio_id,
                                          commitment_id=c.id, amount=take, journal_id=journal_id))
    c.status = "consumed" if c.consumed_amount == c.amount else "partially_consumed"
    return c


def release(session: Session, commitment_id) -> None:
    c = session.get(CapitalCommitment, commitment_id, with_for_update=True)
    if c.status in OPEN:
        c.status = "released"
