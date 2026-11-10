import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgMixin


class AuditLog(OrgMixin, Base):
    """Append-only (UPDATE/DELETE revoked from the application role)."""

    __tablename__ = "audit_logs"
    actor_user_id: Mapped[uuid.UUID | None]
    actor_type: Mapped[str] = mapped_column(default="user")  # user | service | system
    action: Mapped[str]
    entity: Mapped[str]
    entity_id: Mapped[str | None]
    portfolio_id: Mapped[uuid.UUID | None]
    before: Mapped[dict | None]
    after: Mapped[dict | None]


APPROVAL_KINDS = (
    "procurement_purchase",
    "refund",
    "capital_reallocation",
    "price_change",
    "discount_override",
    "ad_budget_change",
    "credential_change",
    "ledger_adjustment",
    "inventory_write_off",
    "variance_acceptance",
    "paid_shipment_booking",
    "risk_threshold_override",
)


class HumanApproval(OrgMixin, Base):
    __tablename__ = "human_approvals"
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key"),
        CheckConstraint("status in ('pending','approved','rejected','expired','executed')"),
        # Separation of duties is enforced by the database, not just the UI.
        CheckConstraint("decided_by is null or decided_by <> requested_by", name="approver_differs"),
    )
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"))
    kind: Mapped[str]
    subject_type: Mapped[str]
    subject_id: Mapped[str]
    amount: Mapped[Decimal | None]
    currency: Mapped[str] = mapped_column(default="IDR")
    payload: Mapped[dict] = mapped_column(default=dict)
    requested_by: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(default="pending")
    decided_by: Mapped[uuid.UUID | None]
    decided_at: Mapped[datetime | None]
    reason: Mapped[str | None]
    idempotency_key: Mapped[str]
    expires_at: Mapped[datetime | None]


class IntegrationError(OrgMixin, Base):
    __tablename__ = "integration_errors"
    platform_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("platform_accounts.id"))
    kind: Mapped[str]
    severity: Mapped[str] = mapped_column(default="error")
    message: Mapped[str]
    detail: Mapped[dict] = mapped_column(default=dict)
    resolved_at: Mapped[datetime | None]


class AgentExecutionLog(OrgMixin, Base):
    __tablename__ = "agent_execution_logs"
    agent: Mapped[str]  # ai_sales_admin
    conversation_id: Mapped[uuid.UUID | None]
    tool: Mapped[str]
    request: Mapped[dict] = mapped_column(default=dict)
    decision: Mapped[str]  # allowed | denied | escalated
    reason: Mapped[str | None]
    response: Mapped[dict] = mapped_column(default=dict)


class OutboxMessage(OrgMixin, Base):
    """Transactional outbox: written in the same transaction as the business change (ADR-003)."""

    __tablename__ = "outbox_messages"
    __table_args__ = (
        UniqueConstraint("organization_id", "dedupe_key"),
        Index("ix_outbox_due", "status", "next_attempt_at"),
    )
    topic: Mapped[str]
    dedupe_key: Mapped[str]
    payload: Mapped[dict]
    status: Mapped[str] = mapped_column(default="pending")  # pending | done | failed | dead
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime]
    last_error: Mapped[str | None]
    processed_at: Mapped[datetime | None]


class ExceptionCase(OrgMixin, Base):
    __tablename__ = "exception_cases"
    __table_args__ = (
        CheckConstraint(
            "status in ('open','in_progress','awaiting_approval','resolved','written_off')", name="case_status"
        ),
        # One open case per (type, subject): repeated triggers append events instead of new cases.
        Index(
            "uq_open_case",
            "organization_id",
            "type",
            "subject_type",
            "subject_id",
            unique=True,
            postgresql_where="status not in ('resolved','written_off')",
        ),
    )
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"))
    type: Mapped[str]
    severity: Mapped[str]
    subject_type: Mapped[str]  # order | shipment | settlement | procurement_task | payment
    subject_id: Mapped[str]
    order_id: Mapped[uuid.UUID | None]
    status: Mapped[str] = mapped_column(default="open")
    owner_role: Mapped[str]
    sla_due_at: Mapped[datetime]
    decision: Mapped[str | None]
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("human_approvals.id"))
    exposure: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    resolution_note: Mapped[str | None]
    resolved_at: Mapped[datetime | None]
    context: Mapped[dict] = mapped_column(default=dict)


class ExceptionEvent(OrgMixin, Base):
    """Append-only decision log for a case."""

    __tablename__ = "exception_events"
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exception_cases.id"), index=True)
    portfolio_id: Mapped[uuid.UUID | None]
    actor_user_id: Mapped[uuid.UUID | None]
    event: Mapped[str]
    detail: Mapped[dict] = mapped_column(default=dict)


class ExternalOperation(OrgMixin, Base):
    """Every external side effect is recorded and committed *before* the call (spec §27.6, ADR-015)."""

    __tablename__ = "external_operations"
    __table_args__ = (
        UniqueConstraint("organization_id", "provider", "operation", "idempotency_key"),
        CheckConstraint("status in ('prepared','sent','confirmed','uncertain','failed')"),
    )
    platform_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("platform_accounts.id"))
    provider: Mapped[str]
    operation: Mapped[str]
    idempotency_key: Mapped[str]
    status: Mapped[str] = mapped_column(default="prepared")
    request_summary: Mapped[dict] = mapped_column(default=dict)  # never secrets
    provider_ref: Mapped[str | None]
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None]
    sent_at: Mapped[datetime | None]
    confirmed_at: Mapped[datetime | None]
    response: Mapped[dict] = mapped_column(default=dict)  # provider response summary (ids, waybill, url) — no secrets
    subject_type: Mapped[str | None]
    subject_id: Mapped[str | None]
    claim_token: Mapped[uuid.UUID | None]  # owner of the current ``sent`` claim (fencing token)
    claimed_at: Mapped[datetime | None]


class ExternalOperationAttempt(OrgMixin, Base):
    """One row per claimed attempt of an external operation (call or takeover of a dead claim)."""

    __tablename__ = "external_operation_attempts"
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("external_operations.id"), index=True)
    attempt_no: Mapped[int]
    claim_token: Mapped[uuid.UUID] = mapped_column(unique=True)
    kind: Mapped[str]  # call | takeover
    finished_at: Mapped[datetime | None]
    outcome: Mapped[str | None]
    error: Mapped[str | None]


class NotificationDelivery(OrgMixin, Base):
    """One risk-alert notification per (case, channel): status, attempts, last error, correlation id (E2)."""

    __tablename__ = "notification_deliveries"
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exception_cases.id"), index=True)
    channel: Mapped[str]
    recipient_role: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending")  # pending | sent | failed | dead
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None]
    provider_ref: Mapped[str | None]
    correlation_id: Mapped[str | None]
    sent_at: Mapped[datetime | None]


class DeadLetter(OrgMixin, Base):
    __tablename__ = "dead_letters"
    outbox_message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("outbox_messages.id"), unique=True)
    topic: Mapped[str]
    last_error: Mapped[str | None]
    attempts: Mapped[int]
    reviewed_by: Mapped[uuid.UUID | None]
    resolution: Mapped[str | None]  # requeued | discarded
    resolved_at: Mapped[datetime | None]
