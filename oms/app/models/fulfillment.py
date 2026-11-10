import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgMixin

ROUTES = ("OWNED_STOCK_BITESHIP", "SHOPEE_DROPSHIP", "TOKOPEDIA_DROPSHIP", "PLATFORM_MANAGED", "MANUAL_REVIEW")


class RoutingDecision(OrgMixin, Base):
    __tablename__ = "routing_decisions"
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    route: Mapped[str]
    inputs: Mapped[dict]
    reasons: Mapped[list]
    decided_by: Mapped[str] = mapped_column(default="rules_v1")


class FulfillmentGroup(OrgMixin, Base):
    """One route + source + portfolio per group (spec §8, ADR-010). A group belongs to exactly one portfolio so
    portfolio users can operate it without seeing other portfolios' lines (spec §27.4)."""

    __tablename__ = "fulfillment_groups"
    __table_args__ = (
        CheckConstraint("route in ('OWNED_STOCK_BITESHIP','SHOPEE_DROPSHIP','TOKOPEDIA_DROPSHIP','PLATFORM_MANAGED')"),
    )
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    route: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending")
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouses.id"))
    supplier_offer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("supplier_offers.id"))
    destination: Mapped[dict] = mapped_column(default=dict)


class FulfillmentGroupItem(OrgMixin, Base):
    __tablename__ = "fulfillment_group_items"
    __table_args__ = (UniqueConstraint("group_id", "order_item_id"), CheckConstraint("qty > 0"))
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fulfillment_groups.id"), index=True)
    order_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("order_items.id"), index=True)
    qty: Mapped[int]


class Shipment(OrgMixin, Base):
    __tablename__ = "shipments"
    __table_args__ = (
        UniqueConstraint("carrier_account_id", "external_id"),
        CheckConstraint("cod_amount >= 0"),
    )
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("fulfillment_groups.id"), index=True)
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"), index=True)
    carrier_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("platform_accounts.id"))
    carrier: Mapped[str]  # biteship | supplier | platform
    courier_code: Mapped[str | None]
    external_id: Mapped[str | None]
    waybill: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="draft")
    cod_amount: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    shipping_cost: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    booking_approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("human_approvals.id"))
    booked_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]


class ShipmentItem(OrgMixin, Base):
    __tablename__ = "shipment_items"
    __table_args__ = (UniqueConstraint("shipment_id", "order_item_id"), CheckConstraint("qty > 0"))
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    shipment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shipments.id"), index=True)
    order_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("order_items.id"), index=True)
    qty: Mapped[int]
    unit_cost: Mapped[Decimal | None]  # captured at pickup (owned) or purchase (dropship)


class ShipmentEvent(OrgMixin, Base):
    __tablename__ = "shipment_events"
    __table_args__ = (UniqueConstraint("shipment_id", "status", "occurred_at"),)
    shipment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shipments.id"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    status: Mapped[str]
    occurred_at: Mapped[datetime]
    raw: Mapped[dict] = mapped_column(default=dict)


class ReturnRecord(OrgMixin, Base):
    __tablename__ = "returns"
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    shipment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("shipments.id"))
    status: Mapped[str] = mapped_column(default="in_transit")  # in_transit | received | inspected
    condition: Mapped[str | None]  # resellable | damaged
    received_at: Mapped[datetime | None]


PROCUREMENT_STATUSES = (
    "open",
    "blocked",
    "awaiting_approval",
    "approved",
    "purchased",
    "shipped",
    "delivered",
    "canceled",
)


class ProcurementTask(OrgMixin, Base):
    """Human-operated (copilot) dropship purchase for one order line."""

    __tablename__ = "procurement_tasks"
    __table_args__ = (
        UniqueConstraint("order_item_id"),
        CheckConstraint(
            "status in ('open','blocked','awaiting_approval','approved','purchased','shipped','delivered','canceled')"
        ),
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    order_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("order_items.id"))
    marketplace: Mapped[str]
    status: Mapped[str] = mapped_column(default="open")
    selected_offer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("supplier_offers.id"))
    observation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("supplier_observations.id"))
    landed_cost: Mapped[Decimal | None]
    expected_contribution: Mapped[Decimal | None]
    block_reason: Mapped[str | None]
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("human_approvals.id"))
    assigned_to: Mapped[uuid.UUID | None]
    destination: Mapped[dict] = mapped_column(default=dict)


class ExternalPurchase(OrgMixin, Base):
    __tablename__ = "external_purchases"
    __table_args__ = (
        UniqueConstraint("marketplace", "external_order_id"),
        UniqueConstraint("procurement_task_id"),
        Index("ix_external_purchase_order", "order_id"),
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    procurement_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("procurement_tasks.id"))
    purchase_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("purchase_tasks.id"))
    marketplace_account_ref: Mapped[str | None]  # buyer account used (no credentials stored)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"))
    marketplace: Mapped[str]
    external_order_id: Mapped[str]
    amount_paid: Mapped[Decimal]
    evidence_ref: Mapped[str | None]
    purchased_by: Mapped[uuid.UUID]
    tracking_number: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="purchased")


class PurchaseTask(OrgMixin, Base):
    """Human-operated dropship purchase for one fulfillment group (spec §8; replaces ProcurementTask)."""

    __tablename__ = "purchase_tasks"
    __table_args__ = (
        CheckConstraint("status in ('open','observed','blocked','awaiting_approval','approved','purchased',"
                        "'supplier_shipped','delivered','canceled','supplier_canceled')"),
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fulfillment_groups.id"), index=True)
    supplier_offer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("supplier_offers.id"))
    marketplace: Mapped[str]
    status: Mapped[str] = mapped_column(default="open")
    observation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("supplier_observations.id"))
    landed_cost: Mapped[Decimal | None]
    expected_contribution: Mapped[Decimal | None]
    block_reason: Mapped[str | None]
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("human_approvals.id"))
    commitment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("capital_commitments.id"))


class PurchaseLine(OrgMixin, Base):
    """Quantities bought for an order line. A trigger keeps order_items.qty_procured in sync under row lock, and the
    CHECK procured <= allocated_dropship rejects over-procurement even from concurrent transactions."""

    __tablename__ = "purchase_lines"
    __table_args__ = (CheckConstraint("qty > 0"), CheckConstraint("unit_price >= 0"))
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    purchase_task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchase_tasks.id"), index=True)
    external_purchase_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("external_purchases.id"))
    order_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("order_items.id"), index=True)
    qty: Mapped[int]
    unit_price: Mapped[Decimal]
