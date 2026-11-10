"""Capital management ledger (spec §10, ADR-009). Mandates and commitments are management records, never GL."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgMixin


class CapitalAllocationEvent(OrgMixin, Base):
    """Append-only mandate changes. A portfolio's mandate = sum of its events (effective-dated)."""

    __tablename__ = "capital_allocation_events"
    __table_args__ = (CheckConstraint("event_type in ('set','increase','decrease','reallocate_in','reallocate_out')"),)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    category: Mapped[str]  # advertising | inventory | fulfillment | opex | reserve
    event_type: Mapped[str]
    amount: Mapped[Decimal]  # signed
    effective_from: Mapped[date]
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("human_approvals.id"))
    counterpart_portfolio_id: Mapped[uuid.UUID | None]
    reason: Mapped[str]
    actor_user_id: Mapped[uuid.UUID | None]


class CapitalCommitment(OrgMixin, Base):
    __tablename__ = "capital_commitments"
    __table_args__ = (
        CheckConstraint("amount > 0"),
        CheckConstraint("consumed_amount >= 0 and consumed_amount <= amount"),
        CheckConstraint("status in ('proposed','approved','partially_consumed','consumed','released','expired')"),
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    category: Mapped[str]
    purpose: Mapped[str]
    source_type: Mapped[str]  # purchase_task | inventory_po | ad_budget | opex
    source_id: Mapped[str]
    amount: Mapped[Decimal]
    consumed_amount: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    status: Mapped[str] = mapped_column(default="proposed")
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("human_approvals.id"))
    expires_at: Mapped[datetime | None]
    legal_entity_id: Mapped[uuid.UUID | None]  # entity that will pay (portfolio → brand → legal entity)
    expected_payment_date: Mapped[date | None]  # when the cash is expected to leave (cash forecast week)


class CommitmentConsumption(OrgMixin, Base):
    __tablename__ = "commitment_consumptions"
    __table_args__ = (CheckConstraint("amount > 0"),)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    commitment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capital_commitments.id"), index=True)
    amount: Mapped[Decimal]
    journal_id: Mapped[uuid.UUID | None]
