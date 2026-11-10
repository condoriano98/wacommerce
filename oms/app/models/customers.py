import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgMixin


class IdentityDomain(OrgMixin, Base):
    """Scope in which an external identifier is unique (spec §5, D6): e.g. one Meta business (BSUIDs are
    business-scoped) or one marketplace shop."""

    __tablename__ = "identity_domains"
    __table_args__ = (UniqueConstraint("organization_id", "kind", "scope_ref"),)
    kind: Mapped[str]  # meta_business | marketplace_shop | email
    scope_ref: Mapped[str]  # e.g. Meta business id / brand id / shop id
    brand_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("brands.id"))


class Customer(OrgMixin, Base):
    __tablename__ = "customers"
    brand_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brands.id"))
    display_name: Mapped[str | None]
    merged_into_customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))
    cod_blocked: Mapped[bool] = mapped_column(default=False)
    risk_score: Mapped[int] = mapped_column(default=0)


class CustomerIdentity(OrgMixin, Base):
    """BSUID is the primary WhatsApp identity; phone is optional. Scoped per brand (business):
    the same person is never merged across businesses."""

    __tablename__ = "customer_identities"
    __table_args__ = (
        UniqueConstraint("brand_id", "identity_type", "identity_value"),
        CheckConstraint("identity_type in ('bsuid','phone_e164','wa_id','marketplace_buyer','email')"),
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    brand_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brands.id"))
    identity_domain_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("identity_domains.id"))
    provider: Mapped[str | None]  # whatsapp | shopee | tiktokshop | lazada | email
    identity_type: Mapped[str]
    identity_value: Mapped[str]
    first_seen_at: Mapped[datetime | None]


class CustomerPortfolio(OrgMixin, Base):
    """Which portfolios may see a customer (drives Postgres RLS for customer data)."""

    __tablename__ = "customer_portfolios"
    __table_args__ = (UniqueConstraint("customer_id", "portfolio_id"),)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)


class Address(OrgMixin, Base):
    __tablename__ = "addresses"
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    recipient_name: Mapped[str]
    phone: Mapped[str | None]
    line1: Mapped[str]
    district: Mapped[str | None]
    city: Mapped[str]
    province: Mapped[str | None]
    postal_code: Mapped[str]
    validated: Mapped[bool] = mapped_column(default=False)


class PrivacyPreference(OrgMixin, Base):
    __tablename__ = "privacy_preferences"
    __table_args__ = (UniqueConstraint("customer_id", "purpose"),)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    purpose: Mapped[str]  # meta_ad_measurement | google_ad_measurement | marketing_messages
    lawful_basis: Mapped[str]  # consent | legitimate_interest | contract
    consent_status: Mapped[str]  # granted | denied | withdrawn | unknown
    consent_timestamp: Mapped[datetime | None]
    withdrawal_timestamp: Mapped[datetime | None]


class ConsentEvent(OrgMixin, Base):
    """Append-only evidence of consent decisions (UU PDP)."""

    __tablename__ = "consent_events"
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    purpose: Mapped[str]
    decision: Mapped[str]
    notice_version: Mapped[str]
    source: Mapped[str]  # whatsapp_reply | landing_page | agent
    source_message_id: Mapped[str | None]


class Conversation(OrgMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("platform_account_id", "customer_id"),)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    chatwoot_conversation_id: Mapped[int | None]
    last_inbound_at: Mapped[datetime | None]  # drives the 24h service window
    ai_enabled: Mapped[bool] = mapped_column(default=True)
    handoff_reason: Mapped[str | None]


class InboundWebhook(OrgMixin, Base):
    """Raw provider payloads, stored before processing (replayable)."""

    __tablename__ = "inbound_webhooks"
    platform_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("platform_accounts.id"))
    provider: Mapped[str]
    raw_body: Mapped[str]
    headers: Mapped[dict] = mapped_column(default=dict)
    processed_at: Mapped[datetime | None]
    error: Mapped[str | None]


class Message(OrgMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("platform_account_id", "external_id"),)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    direction: Mapped[str]  # inbound | outbound
    author: Mapped[str]  # customer | ai | human | system
    external_id: Mapped[str | None]  # wamid
    body: Mapped[str | None]
    payload: Mapped[dict] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(default="received")
    sent_at: Mapped[datetime | None]


class LandingSession(OrgMixin, Base):
    __tablename__ = "landing_sessions"
    __table_args__ = (UniqueConstraint("organization_id", "ref_code"),)
    brand_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brands.id"))
    ref_code: Mapped[str]
    fbp: Mapped[str | None]
    fbc: Mapped[str | None]
    fbclid: Mapped[str | None]
    gclid: Mapped[str | None]
    utm: Mapped[dict] = mapped_column(default=dict)
    landing_page: Mapped[str | None]
    client_user_agent: Mapped[str | None]


class CustomerAttribution(OrgMixin, Base):
    """One row per acquisition touch. Identifiers are captured, never fabricated."""

    __tablename__ = "customer_attribution"
    __table_args__ = (
        UniqueConstraint("organization_id", "source_message_id"),
        CheckConstraint(
            "attribution_method in ('ctwa_referral','landing_ref_code','platform_native','manual','unattributed')"
        ),
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    source_message_id: Mapped[str | None]
    attribution_method: Mapped[str]
    provider: Mapped[str | None]  # meta | google | tiktok
    ad_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("platform_accounts.id"))
    campaign_id: Mapped[str | None]
    adset_id: Mapped[str | None]
    ad_id: Mapped[str | None]
    resolution_status: Mapped[str] = mapped_column(default="unresolved")  # resolved | unresolved | foreign
    source_type: Mapped[str | None]
    source_url: Mapped[str | None]
    ctwa_clid: Mapped[str | None]
    landing_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("landing_sessions.id"))
    fbp: Mapped[str | None]
    fbc: Mapped[str | None]
    gclid: Mapped[str | None]
    raw_referral: Mapped[dict | None]
    touched_at: Mapped[datetime]
