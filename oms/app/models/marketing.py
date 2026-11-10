import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgMixin


class AdCampaign(OrgMixin, Base):
    __tablename__ = "ad_campaigns"
    __table_args__ = (UniqueConstraint("platform_account_id", "external_id"),)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    provider: Mapped[str]
    external_id: Mapped[str]
    name: Mapped[str]
    objective: Mapped[str | None]
    status: Mapped[str | None]


class AdGroup(OrgMixin, Base):
    __tablename__ = "ad_groups"
    __table_args__ = (UniqueConstraint("platform_account_id", "external_id"),)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ad_campaigns.id"))
    external_id: Mapped[str]
    name: Mapped[str]
    status: Mapped[str | None]
    targeting: Mapped[dict] = mapped_column(default=dict)


class Advertisement(OrgMixin, Base):
    __tablename__ = "advertisements"
    __table_args__ = (UniqueConstraint("platform_account_id", "external_id"),)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    ad_group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ad_groups.id"))
    external_id: Mapped[str]
    name: Mapped[str]
    status: Mapped[str | None]
    creative_ref: Mapped[dict] = mapped_column(default=dict)


class DailyAdInsight(OrgMixin, Base):
    """Generic, provider-neutral daily performance at ad level (Meta, Google, TikTok, marketplace ads)."""

    __tablename__ = "daily_ad_insights"
    __table_args__ = (UniqueConstraint("platform_account_id", "report_date", "ad_external_id"),)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    provider: Mapped[str]
    report_date: Mapped[date]
    campaign_external_id: Mapped[str]
    ad_group_external_id: Mapped[str | None]
    ad_external_id: Mapped[str]
    currency: Mapped[str] = mapped_column(default="IDR")
    spend: Mapped[Decimal]
    impressions: Mapped[int] = mapped_column(default=0)
    reach: Mapped[int] = mapped_column(default=0)
    clicks: Mapped[int] = mapped_column(default=0)
    link_clicks: Mapped[int] = mapped_column(default=0)
    messaging_conversations: Mapped[int] = mapped_column(default=0)
    reported_conversions: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    reported_conversion_value: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    attribution_setting: Mapped[str | None]
    data_source: Mapped[str] = mapped_column(default="api")  # api | csv
    pulled_at: Mapped[datetime]


class CampaignPortfolioAllocation(OrgMixin, Base):
    """How a campaign's cost maps to portfolios. Shares for one campaign may total < 1; the remainder stays
    unallocated (never silently spread)."""

    __tablename__ = "campaign_portfolio_allocations"
    __table_args__ = (
        UniqueConstraint("campaign_id", "portfolio_id"),
        CheckConstraint("share > 0 and share <= 1"),
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ad_campaigns.id"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    method: Mapped[str]  # direct | fixed_share
    share: Mapped[Decimal] = mapped_column(Numeric(7, 4))


class ConversionOutbox(OrgMixin, Base):
    __tablename__ = "conversion_outbox"
    __table_args__ = (
        UniqueConstraint("subject_type", "subject_id", "event_name", "destination", "occurrence_key"),
        UniqueConstraint("event_id"),
        CheckConstraint("status in ('pending','sent','failed','expired','suppressed')"),
    )
    event_id: Mapped[uuid.UUID]  # stable across retries
    subject_type: Mapped[str]  # customer | order
    subject_id: Mapped[uuid.UUID]
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))
    event_name: Mapped[str]
    destination: Mapped[str]  # meta_business_messaging | meta_website | google_data_manager
    dataset_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("platform_accounts.id"))
    occurrence_key: Mapped[str] = mapped_column(default="")
    event_time: Mapped[datetime]
    value: Mapped[Decimal | None]
    currency: Mapped[str] = mapped_column(default="IDR")
    status: Mapped[str] = mapped_column(default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime]
    last_error: Mapped[str | None]
    status_reason: Mapped[str | None]
    sent_at: Mapped[datetime | None]
    external_ack: Mapped[dict | None]


class ConversionDeliveryLog(OrgMixin, Base):
    __tablename__ = "conversion_delivery_logs"
    conversion_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversion_outbox.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    attempt: Mapped[int]
    request: Mapped[dict]  # hashed identifiers only
    response: Mapped[dict]
    http_status: Mapped[int | None]
