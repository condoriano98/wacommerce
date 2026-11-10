import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgMixin


class Warehouse(OrgMixin, Base):
    """Physical location; may hold stock owned by several portfolios."""

    __tablename__ = "warehouses"
    name: Mapped[str]
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("legal_entities.id"))
    city: Mapped[str]
    postal_code: Mapped[str]
    address: Mapped[str]


class InventoryBalance(OrgMixin, Base):
    """Stock owned by one portfolio in one warehouse. CHECK constraints make overselling impossible:
    a reservation that would push reserved above on_hand fails in the database."""

    __tablename__ = "inventory_balances"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "sku_id", "warehouse_id"),
        CheckConstraint("in_transit >= 0", name="in_transit_nonneg"),
        CheckConstraint("on_hand >= 0", name="on_hand_nonneg"),
        CheckConstraint("reserved >= 0", name="reserved_nonneg"),
        CheckConstraint("reserved <= on_hand", name="no_oversell"),
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    sku_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skus.id"), index=True)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("legal_entities.id"))
    on_hand: Mapped[int] = mapped_column(default=0)
    reserved: Mapped[int] = mapped_column(default=0)
    # Goods picked by the carrier but not yet delivered: valued in GL account 1250 (spec §9 policy 4).
    in_transit: Mapped[int] = mapped_column(default=0)
    # Moving-average unit cost (documented valuation method; FIFO layers are a backlog item).
    avg_unit_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))


class InventoryMovement(OrgMixin, Base):
    """Append-only stock ledger."""

    __tablename__ = "inventory_movements"
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    balance_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("inventory_balances.id"), index=True)
    movement_type: Mapped[str]  # receive | ship | adjust | transfer_in | transfer_out | return | write_off
    qty: Mapped[int]
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    reference_type: Mapped[str | None]
    reference_id: Mapped[str | None]
    note: Mapped[str | None]


class InventoryReservation(OrgMixin, Base):
    __tablename__ = "inventory_reservations"
    __table_args__ = (
        CheckConstraint("qty > 0"),
        CheckConstraint("status in ('active','consumed','released','expired')"),
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    balance_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("inventory_balances.id"), index=True)
    kind: Mapped[str]  # quote_hold (TTL) | order
    quote_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("quotes.id"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))
    qty: Mapped[int]
    status: Mapped[str] = mapped_column(default="active")
    expires_at: Mapped[datetime | None]


class StockCount(OrgMixin, Base):
    __tablename__ = "stock_counts"
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    counted_by: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(default="open")  # open | posted


class StockCountLine(OrgMixin, Base):
    __tablename__ = "stock_count_lines"
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    stock_count_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stock_counts.id"), index=True)
    balance_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("inventory_balances.id"))
    system_qty: Mapped[int]
    counted_qty: Mapped[int]
