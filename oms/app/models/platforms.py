import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, LargeBinary, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgMixin

ACCOUNT_ROLES = ("advertising", "sales", "messaging", "procurement", "shipping", "payments")


class Provider(Base):
    """Static catalogue of supported providers (global, not tenant data)."""

    __tablename__ = "providers"
    code: Mapped[str] = mapped_column(primary_key=True)  # meta, whatsapp, shopee, tiktokshop, ...
    name: Mapped[str]
    category: Mapped[str]
    default_api_version: Mapped[str | None]


class PlatformAccount(OrgMixin, Base):
    __tablename__ = "platform_accounts"
    __table_args__ = (
        UniqueConstraint("organization_id", "provider", "provider_account_id"),
        CheckConstraint(
            "role in ('advertising','sales','messaging','procurement','shipping','payments')", name="role_valid"
        ),
    )
    provider: Mapped[str] = mapped_column(ForeignKey("providers.code"))
    provider_account_id: Mapped[str]
    display_name: Mapped[str]
    role: Mapped[str]
    brand_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("brands.id"))
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("legal_entities.id"))
    status: Mapped[str] = mapped_column(default="disconnected")  # disconnected | connected | error | revoked
    # Live calls are impossible unless this is true AND credentials exist (ADR-004).
    live_enabled: Mapped[bool] = mapped_column(default=False)
    scopes: Mapped[list] = mapped_column(default=list)
    api_version: Mapped[str | None]
    rate_limit_cfg: Mapped[dict] = mapped_column(default=dict)
    webhook_status: Mapped[str] = mapped_column(default="none")
    # Public identifiers only (phone_number_id, waba_id, shop_id, dataset_id...). Never secrets.
    public_config: Mapped[dict] = mapped_column(default=dict)
    last_sync_ok_at: Mapped[datetime | None]
    last_sync_failed_at: Mapped[datetime | None]
    last_error: Mapped[str | None]


class AccountPortfolioLink(OrgMixin, Base):
    __tablename__ = "account_portfolio_links"
    __table_args__ = (UniqueConstraint("platform_account_id", "portfolio_id"),)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)


class AccountCredential(OrgMixin, Base):
    """Encrypted secrets. The application DB role has NO privileges on this table (see migration)."""

    __tablename__ = "account_credentials"
    __table_args__ = (UniqueConstraint("platform_account_id", "name"),)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    name: Mapped[str]  # access_token, refresh_token, app_secret, api_key ...
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    version: Mapped[int] = mapped_column(default=1)
    expires_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None]


class IntegrationSyncRun(OrgMixin, Base):
    __tablename__ = "integration_sync_runs"
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    kind: Mapped[str]  # orders | settlements | insights | ...
    window_start: Mapped[datetime | None]
    window_end: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(default="running")
    stats: Mapped[dict] = mapped_column(default=dict)
    error: Mapped[str | None]


IMPLEMENTATION_STATUSES = ("planned", "mock_tested", "sandbox_verified", "production_verified")
ACCESS_STATES = ("available", "unsupported", "not_authorized", "temporarily_unavailable", "mocked", "planned",
                 "verification_pending")


class IntegrationCapability(OrgMixin, Base):
    """Capability registry (spec §6, §27.11). Implementation status and access state are independent:
    'operationally ready' requires production_verified AND available."""

    __tablename__ = "integration_capabilities"
    __table_args__ = (
        UniqueConstraint("platform_account_id", "capability"),
        CheckConstraint("implementation_status in ('planned','mock_tested','sandbox_verified','production_verified')"),
        CheckConstraint(
            "access_state in ('available','unsupported','not_authorized','temporarily_unavailable','mocked','planned',"
            "'verification_pending')", name="ck_capability_access_state"
        ),
    )
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    capability: Mapped[str]  # orders.read, settlements.read, payment_link.create, shipment.book ...
    implementation_status: Mapped[str] = mapped_column(default="planned")
    access_state: Mapped[str] = mapped_column(default="planned")
    scopes: Mapped[list] = mapped_column(default=list)
    api_version: Mapped[str | None]
    sandbox_available: Mapped[bool | None]
    rate_limit: Mapped[dict] = mapped_column(default=dict)
    last_contract_verified_at: Mapped[datetime | None]
    verification_source: Mapped[str | None]
    blockers: Mapped[list] = mapped_column(default=list)

    @property
    def operationally_ready(self) -> bool:
        return self.implementation_status == "production_verified" and self.access_state == "available"
