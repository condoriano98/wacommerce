"""Accounting periods (spec §15 close & audit; acceptance #13a). A DB trigger rejects postings into closed periods;
corrections are adjustment journals in an open period that reference the closed one."""

import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.authz import Principal
from app.models.base import utcnow
from app.models.finance import AccountingPeriod, LedgerEntry
from app.modules.finance.ledger import JournalLine, post_journal


def month_bounds(d: date) -> tuple[date, date]:
    start = d.replace(day=1)
    nxt = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    from datetime import timedelta

    return start, nxt - timedelta(days=1)


def ensure_period(session: Session, org_id, legal_entity_id, d: date) -> AccountingPeriod:
    start, end = month_bounds(d)
    p = session.scalars(select(AccountingPeriod).where(AccountingPeriod.legal_entity_id == legal_entity_id,
                                                       AccountingPeriod.period_start == start)).first()
    if p is None:
        p = AccountingPeriod(organization_id=org_id, legal_entity_id=legal_entity_id, period_start=start,
                             period_end=end, status="open")
        session.add(p)
        session.flush()
    return p


def trial_balance(session: Session, legal_entity_id, start: date, end: date) -> dict[str, Decimal]:
    rows = session.execute(select(LedgerEntry.account_code, func.sum(LedgerEntry.debit - LedgerEntry.credit)).where(
        LedgerEntry.legal_entity_id == legal_entity_id, LedgerEntry.accounting_date >= start,
        LedgerEntry.accounting_date <= end).group_by(LedgerEntry.account_code)).all()
    return {a: Decimal(v) for a, v in rows}


def close_period(session: Session, principal: Principal, legal_entity_id, period_start: date) -> AccountingPeriod:
    principal.require("finance.manage")
    p = ensure_period(session, principal.org_id, legal_entity_id, period_start)
    if p.status == "closed":
        raise HTTPException(status.HTTP_409_CONFLICT, "period already closed")
    tb = trial_balance(session, legal_entity_id, p.period_start, p.period_end)
    if sum(tb.values(), Decimal("0")) != 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "trial balance does not net to zero")
    p.snapshot_hash = hashlib.sha256(json.dumps({k: str(v) for k, v in sorted(tb.items())}).encode()).hexdigest()
    p.status, p.closed_by, p.closed_at = "closed", principal.user_id, utcnow()
    audit(session, principal.org_id, "period.closed", "accounting_period", p.id, actor_user_id=principal.user_id,
          after={"period_start": p.period_start, "hash": p.snapshot_hash})
    return p


def post_adjustment(session: Session, principal: Principal, *, legal_entity_id, closed_period_start: date,
                    lines: list[JournalLine], reason: str, approval_id: uuid.UUID) -> uuid.UUID:
    """Correction for a closed period: posted today (open period), referencing the closed period; approval-backed."""
    from app.modules import approvals

    approvals.require_approved(session, approval_id, "ledger_adjustment", str(closed_period_start))
    return post_journal(session, principal.org_id, source_type="adjustment",
                        source_id=f"{closed_period_start}:{approval_id}", lines=lines, legal_entity_id=legal_entity_id,
                        memo=f"adjustment for period {closed_period_start}: {reason}")
