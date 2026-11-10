import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgMixin


class Quote(OrgMixin, Base):
    """Server-computed offer. Orders from WhatsApp are created *from a quote*, never from free text."""

    __tablename__ = "quotes"
    __table_args__ = (
        CheckConstraint("status in ('offered','pending_availability','accepted','expired','rejected','superseded')"),
    )
    brand_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brands.id"))
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    status: Mapped[str] = mapped_column(default="offered")
    valid_until: Mapped[datetime]
    subtotal: Mapped[Decimal]
    discount_total: Mapped[Decimal]
    shipping_fee: Mapped[Decimal]
    total: Mapped[Decimal]
    payment_method: Mapped[str | None]
    created_by: Mapped[str]  # ai | human
    customer_message: Mapped[str]  # authoritative text the AI must use verbatim
    policy_snapshot: Mapped[dict] = mapped_column(default=dict)


class QuoteLine(OrgMixin, Base):
    __tablename__ = "quote_lines"
    quote_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("quotes.id"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    sku_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skus.id"))
    listing_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_listings.id"))
    qty: Mapped[int]
    unit_price: Mapped[Decimal]
    discount_rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), default=Decimal("0"))
    line_total: Mapped[Decimal]
    availability_source: Mapped[str]  # owned | dropship | unavailable


ORDER_STATUSES = (
    "placed",
    "confirmed",
    "payment_pending",
    "payment_verified",
    "cod_confirmed",
    "routed",
    "reserved",
    "packing",
    "packed",
    "procurement_pending",
    "awaiting_approval",
    "purchased",
    "platform_fulfilling",
    "shipped",
    "delivered",
    "settled",
    "reconciled",
    "canceled",
    "returned",
    "refunded",
    "exception",
)


class Order(OrgMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("platform_account_id", "external_order_id"),
        Index("uq_order_quote", "quote_id", unique=True, postgresql_where="quote_id is not null"),
        CheckConstraint("total >= 0"),
    )
    brand_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brands.id"))
    order_number: Mapped[str]
    sales_channel: Mapped[str]  # whatsapp | shopee | tokopedia | tiktokshop | lazada
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    external_order_id: Mapped[str]
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"), index=True)
    quote_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("quotes.id"))
    status: Mapped[str] = mapped_column(default="placed")
    payment_method: Mapped[str]  # doku_link | manual_transfer | cod | marketplace
    payment_status: Mapped[str] = mapped_column(default="unpaid")  # unpaid | paid | cod_pending | collected | refunded
    fulfillment_route: Mapped[str | None]
    # Derived from ledger, reconciliation and cases (settlements.derive_settlement_status): none | expected |
    # partially_settled | matched | variance | under_review | adjusted | closed
    settlement_status: Mapped[str] = mapped_column(default="none")
    refund_status: Mapped[str] = mapped_column(default="none")
    currency: Mapped[str] = mapped_column(default="IDR")
    subtotal: Mapped[Decimal]
    discount_total: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    shipping_fee: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    total: Mapped[Decimal]
    shipping_address: Mapped[dict] = mapped_column(default=dict)
    attribution_touch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customer_attribution.id"))
    placed_at: Mapped[datetime]
    confirmed_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(default=1)  # optimistic locking


class OrderItem(OrgMixin, Base):
    """Quantity pipeline per line (spec §8, D2). CHECKs make impossible states unrepresentable."""

    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("qty > 0"),
        CheckConstraint("qty_canceled + qty_allocated_owned + qty_allocated_dropship + qty_allocated_platform <= qty",
                        name="alloc_le_ordered"),
        CheckConstraint("qty_procured <= qty_allocated_dropship", name="procured_le_allocated"),
        CheckConstraint("qty_shipped <= qty_allocated_owned + qty_procured + qty_allocated_platform",
                        name="shipped_le_available"),
        CheckConstraint("qty_delivered <= qty_shipped", name="delivered_le_shipped"),
        CheckConstraint("qty_returned <= qty_delivered", name="returned_le_delivered"),
        CheckConstraint("qty_refunded <= qty", name="refunded_le_ordered"),
        CheckConstraint("least(qty_canceled, qty_allocated_owned, qty_allocated_dropship, qty_allocated_platform, "
                        "qty_procured, qty_shipped, qty_delivered, qty_returned, qty_refunded) >= 0",
                        name="qty_nonneg"),
    )
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    sku_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skus.id"), index=True)
    listing_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("platform_listings.id"))
    qty: Mapped[int]
    unit_price: Mapped[Decimal]
    discount: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    line_total: Mapped[Decimal]
    unit_cost: Mapped[Decimal | None]  # actual cost once known (inventory avg cost or purchase price)
    qty_canceled: Mapped[int] = mapped_column(default=0)
    qty_allocated_owned: Mapped[int] = mapped_column(default=0)
    qty_allocated_dropship: Mapped[int] = mapped_column(default=0)
    qty_allocated_platform: Mapped[int] = mapped_column(default=0)
    qty_procured: Mapped[int] = mapped_column(default=0)
    qty_shipped: Mapped[int] = mapped_column(default=0)
    qty_delivered: Mapped[int] = mapped_column(default=0)
    qty_returned: Mapped[int] = mapped_column(default=0)
    qty_refunded: Mapped[int] = mapped_column(default=0)


class OrderItemAllocation(OrgMixin, Base):
    """Order-level amounts allocated to lines (rounding remainder to the last line). Effective-dated by policy
    version so restatements never silently change history."""

    __tablename__ = "order_item_allocations"
    __table_args__ = (UniqueConstraint("order_item_id", "kind", "policy_version"),)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    order_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("order_items.id"), index=True)
    kind: Mapped[str]  # discount | shipping_income | shipping_cost | payment_fee | cod_fee | marketplace_fee | tax
    amount: Mapped[Decimal]
    method: Mapped[str] = mapped_column(default="pro_rata_line_total")
    policy_version: Mapped[int] = mapped_column(default=1)


class OrderEvent(OrgMixin, Base):
    """Append-only history; financial facts are never overwritten."""

    __tablename__ = "order_events"
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    from_status: Mapped[str | None]
    to_status: Mapped[str]
    actor_user_id: Mapped[uuid.UUID | None]
    actor_type: Mapped[str] = mapped_column(default="user")
    reason: Mapped[str | None]
    data: Mapped[dict] = mapped_column(default=dict)


class Payment(OrgMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("provider", "provider_ref"),)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    method: Mapped[str]
    provider: Mapped[str]  # doku | manual | biteship_cod | marketplace
    provider_ref: Mapped[str]
    amount: Mapped[Decimal]
    # gateway: pending -> captured -> settled ; manual: reported -> attested -> bank_matched (spec §8, §27.3)
    status: Mapped[str] = mapped_column(default="pending")
    payment_url: Mapped[str | None]
    expires_at: Mapped[datetime | None]
    paid_at: Mapped[datetime | None]
    verified_by: Mapped[uuid.UUID | None]
    evidence: Mapped[dict] = mapped_column(default=dict)


class PaymentEvent(OrgMixin, Base):
    """Append-only payment history."""

    __tablename__ = "payment_events"
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    from_status: Mapped[str | None]
    to_status: Mapped[str]
    actor_user_id: Mapped[uuid.UUID | None]
    detail: Mapped[dict] = mapped_column(default=dict)


class PaymentNotification(OrgMixin, Base):
    __tablename__ = "payment_notifications"
    __table_args__ = (UniqueConstraint("organization_id", "provider", "notification_id"),)
    provider: Mapped[str]
    notification_id: Mapped[str]
    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id"))
    raw: Mapped[dict]
    verified: Mapped[bool] = mapped_column(default=False)


class Refund(OrgMixin, Base):
    __tablename__ = "refunds"
    __table_args__ = (CheckConstraint("amount > 0"),)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    amount: Mapped[Decimal]
    reason: Mapped[str]
    status: Mapped[str] = mapped_column(default="requested")  # requested | approved | paid | rejected
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("human_approvals.id"))
    method: Mapped[str | None]
    paid_at: Mapped[datetime | None]
    requested_by: Mapped[uuid.UUID | None]

