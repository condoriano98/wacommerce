"""WhatsApp bridge persistence (spec §12, D7): raw events stored before ack, message mappings for echo suppression,
template registry and media objects."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgMixin


class WaEvent(OrgMixin, Base):
    """One Meta webhook delivery for one WhatsApp number, persisted and committed before the 200 response."""

    __tablename__ = "wa_events"
    __table_args__ = (UniqueConstraint("organization_id", "dedupe_key"),)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    dedupe_key: Mapped[str]  # sha256(phone_number_id + raw body): Meta re-deliveries collapse
    raw: Mapped[dict]
    received_at: Mapped[datetime]
    processed_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None]


class MessageMapping(OrgMixin, Base):
    """Links one OMS message to its Meta wamid and Chatwoot message. ``source_id`` (``oms:<message id>``) is set on
    every message the OMS posts to Chatwoot; Chatwoot echoes such messages back to the API-channel webhook
    (G-CW1, documentation-verified) and the echo is recognised by it."""

    __tablename__ = "message_mappings"
    __table_args__ = (
        UniqueConstraint("message_id"),
        UniqueConstraint("platform_account_id", "wamid"),
        UniqueConstraint("organization_id", "chatwoot_message_id"),
        UniqueConstraint("source_id"),
        CheckConstraint("origin in ('meta','chatwoot','oms')"),
    )
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id"))
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    origin: Mapped[str]
    wamid: Mapped[str | None]
    chatwoot_message_id: Mapped[int | None]
    source_id: Mapped[str | None]


class MessageTemplate(OrgMixin, Base):
    __tablename__ = "message_templates"
    __table_args__ = (UniqueConstraint("platform_account_id", "name", "language"),)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    name: Mapped[str]
    language: Mapped[str]
    category: Mapped[str]  # utility | marketing | authentication
    status: Mapped[str] = mapped_column(default="pending")  # approved | pending | rejected | paused
    param_count: Mapped[int] = mapped_column(default=0)


class MediaObject(OrgMixin, Base):
    __tablename__ = "media_objects"
    __table_args__ = (UniqueConstraint("organization_id", "sha256"),)
    storage_key: Mapped[str]
    mime: Mapped[str]
    sha256: Mapped[str]
    size_bytes: Mapped[int]
    purpose: Mapped[str]  # chat_media | payment_evidence | purchase_evidence
    retention_until: Mapped[datetime | None]
