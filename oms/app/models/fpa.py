"""FP&A persistence (spec §15): cost-to-level map, immutable snapshots, simulation runs."""

import uuid
from datetime import date

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgMixin


class CostLevel(Base):
    __tablename__ = "cost_level_map"
    account_code: Mapped[str] = mapped_column(ForeignKey("ledger_accounts.code"), primary_key=True)
    level: Mapped[str]
    sort: Mapped[int]


class FpaSnapshot(OrgMixin, Base):
    """Append-only (trigger rejects UPDATE/DELETE). A restatement is a new version, never an edit."""

    __tablename__ = "fpa_snapshots"
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("legal_entities.id"))
    scope_type: Mapped[str]  # portfolio | legal_entity
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"))
    period_start: Mapped[date]
    version: Mapped[int]
    payload: Mapped[dict]
    sha256: Mapped[str]
    created_by: Mapped[uuid.UUID | None]
    reason: Mapped[str | None]


class SimulationRun(OrgMixin, Base):
    __tablename__ = "simulation_runs"
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"))
    kind: Mapped[str]  # base | stress | forecast
    scenario: Mapped[str]
    engine_version: Mapped[str]
    inputs: Mapped[dict]
    outputs: Mapped[dict]
    created_by: Mapped[uuid.UUID | None]
