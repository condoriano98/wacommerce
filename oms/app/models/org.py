import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, OrgMixin


class Organization(IdMixin, Base):
    __tablename__ = "organizations"
    name: Mapped[str]
    base_currency: Mapped[str] = mapped_column(default="IDR")


class Brand(OrgMixin, Base):
    __tablename__ = "brands"
    name: Mapped[str]


class User(OrgMixin, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email"),)
    email: Mapped[str]
    name: Mapped[str]
    password_hash: Mapped[str | None]
    is_active: Mapped[bool] = mapped_column(default=True)
    # Service principals (e.g. the AI Sales Admin) authenticate with API tokens, never passwords.
    is_service: Mapped[bool] = mapped_column(default=False)


ROLES = (
    "owner",
    "portfolio_manager",
    "ads_manager",
    "finance_manager",
    "procurement_admin",
    "packing_admin",
    "cs_admin",
    "auditor",
    "ai_sales_agent",
)


class RoleAssignment(OrgMixin, Base):
    """A user's role within a scope. Postgres RLS reads this table to decide visibility."""

    __tablename__ = "role_assignments"
    __table_args__ = (
        UniqueConstraint("user_id", "role", "scope_type", "scope_id"),
        CheckConstraint("scope_type in ('organization','portfolio','platform_account')"),
        CheckConstraint("(scope_type = 'organization') = (scope_id is null)"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str]
    scope_type: Mapped[str]
    scope_id: Mapped[uuid.UUID | None]


class Portfolio(OrgMixin, Base):
    __tablename__ = "portfolios"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)
    brand_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brands.id"))
    code: Mapped[str]
    name: Mapped[str]
    category: Mapped[str | None]
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(default="active")  # active | paused | retired
    risk_class: Mapped[str] = mapped_column(default="medium")
    target_contribution_margin: Mapped[Decimal | None] = mapped_column(default=None)
    target_roic: Mapped[Decimal | None] = mapped_column(default=None)
    max_cpa: Mapped[Decimal | None] = mapped_column(default=None)
    max_inventory_days: Mapped[int | None]
    approved_channels: Mapped[list] = mapped_column(default=list)
    approved_procurement_sources: Mapped[list] = mapped_column(default=list)


BUDGET_CATEGORIES = ("advertising", "inventory", "fulfillment", "opex", "reserve")


class PortfolioBudget(OrgMixin, Base):
    __tablename__ = "portfolio_budgets"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "period_start", "category", "provider"),
        CheckConstraint("amount >= 0"),
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    period_start: Mapped[date]
    period_end: Mapped[date]
    category: Mapped[str]
    provider: Mapped[str | None]  # optional platform split (meta, shopee, ...)
    amount: Mapped[Decimal]


class CapitalScenario(OrgMixin, Base):
    """Editable planning scenario (e.g. the Rp100M illustrative allocation). Org-level data."""

    __tablename__ = "capital_scenarios"
    name: Mapped[str]
    total_capital: Mapped[Decimal]
    allocations: Mapped[dict]  # {"advertising": 27000000, ...} and optional per-portfolio matrix
    is_active: Mapped[bool] = mapped_column(default=False)


class CapitalAllocation(OrgMixin, Base):
    """Auditable movement of capital into or between portfolios (always approval-backed)."""

    __tablename__ = "capital_allocations"
    __table_args__ = (CheckConstraint("amount > 0"),)
    from_portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"))
    to_portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"))
    amount: Mapped[Decimal]
    reason: Mapped[str]
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("human_approvals.id"))
    journal_id: Mapped[uuid.UUID | None]
    effective_at: Mapped[datetime]


class RiskPolicy(OrgMixin, Base):
    __tablename__ = "risk_policies"
    __table_args__ = (UniqueConstraint("organization_id", "portfolio_id", "key"),)
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"))
    key: Mapped[str]  # min_cash_reserve, platform_concentration_max, op_margin_min, ...
    threshold: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    comparator: Mapped[str]  # lt | gt
    severity: Mapped[str] = mapped_column(default="warning")
    active: Mapped[bool] = mapped_column(default=True)
