"""Double-entry posting. Journals are immutable; corrections are reversing journals.
The database rejects unbalanced journals at commit (deferred constraint trigger) and duplicate postings of one business
event (``journals`` header, unique on organization + source_type + source_id; remediation A2)."""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.finance import LedgerEntry

ZERO = Decimal("0")


@dataclass
class JournalLine:
    account: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    portfolio_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    order_item_id: uuid.UUID | None = None
    platform_account_id: uuid.UUID | None = None
    memo: str | None = None


def post_journal(
    session: Session,
    org_id: uuid.UUID,
    *,
    source_type: str,
    source_id: str,
    lines: list[JournalLine],
    accounting_date: date | None = None,
    memo: str | None = None,
    reverses_journal_id: uuid.UUID | None = None,
    legal_entity_id: uuid.UUID | None = None,
    event_time=None,
) -> uuid.UUID | None:
    """Post one balanced journal for the business event ``(source_type, source_id)``.

    Returns the journal id, or ``None`` if that event is already posted (possibly by a concurrent transaction: the
    header insert waits on the unique index and then inserts nothing). Exactly one journal exists per event key."""
    lines = [ln for ln in lines if ln.debit or ln.credit]
    debit = sum((ln.debit for ln in lines), ZERO)
    credit = sum((ln.credit for ln in lines), ZERO)
    if debit != credit:
        raise ValueError(f"unbalanced journal {source_type}:{source_id} debit={debit} credit={credit}")
    journal_id = uuid.uuid4()
    claimed = session.execute(text("select claim_journal(:id, :le, :st, :sid, :rev)"),
                              {"id": journal_id, "le": legal_entity_id, "st": source_type, "sid": source_id,
                               "rev": reverses_journal_id}).scalar()
    if not claimed:
        return None
    for i, ln in enumerate(lines, start=1):
        session.add(
            LedgerEntry(
                organization_id=org_id,
                journal_id=journal_id,
                line_no=i,
                account_code=ln.account,
                portfolio_id=ln.portfolio_id,
                platform_account_id=ln.platform_account_id,
                order_id=ln.order_id,
                order_item_id=ln.order_item_id,
                debit=ln.debit,
                credit=ln.credit,
                accounting_date=accounting_date or utcnow().date(),
                source_type=source_type,
                source_id=source_id,
                memo=ln.memo or memo,
                reverses_journal_id=reverses_journal_id,
                legal_entity_id=legal_entity_id,
                event_time=event_time or utcnow(),
                posting_rule_version=2,
            )
        )
    return journal_id


def journal_exists(session: Session, source_type: str, source_id: str) -> bool:
    """Idempotency guard for posting rules triggered by retried events."""
    stmt = select(LedgerEntry.id).where(LedgerEntry.source_type == source_type, LedgerEntry.source_id == source_id)
    return session.scalar(stmt.limit(1)) is not None


def reverse_journal(session: Session, org_id: uuid.UUID, journal_id: uuid.UUID, reason: str) -> uuid.UUID | None:
    """Reverse a journal once (a second reversal of the same journal returns ``None``)."""
    original = session.scalars(select(LedgerEntry).where(LedgerEntry.journal_id == journal_id)).all()
    if not original:
        raise ValueError("journal not found")
    return post_journal(
        session, org_id, source_type="reversal", source_id=str(journal_id), memo=reason,
        reverses_journal_id=journal_id, legal_entity_id=original[0].legal_entity_id,
        lines=[JournalLine(e.account_code, debit=e.credit, credit=e.debit, portfolio_id=e.portfolio_id,
                           order_id=e.order_id, order_item_id=e.order_item_id,
                           platform_account_id=e.platform_account_id) for e in original],
    )
