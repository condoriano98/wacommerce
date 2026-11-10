import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgMixin


class Product(OrgMixin, Base):
    """Canonical product: never duplicated per marketplace."""

    __tablename__ = "products"
    name: Mapped[str]
    category: Mapped[str | None]
    description: Mapped[str | None]


class Sku(OrgMixin, Base):
    __tablename__ = "skus"
    __table_args__ = (
        UniqueConstraint("organization_id", "code"),
        CheckConstraint("inventory_policy in ('dropship_only','owned','hybrid')"),
    )
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    code: Mapped[str]  # global SKU id
    name: Mapped[str]
    attributes: Mapped[dict] = mapped_column(default=dict)  # variant (size, colour, flavour)
    weight_grams: Mapped[int] = mapped_column(default=0)
    inventory_policy: Mapped[str] = mapped_column(default="owned")
    reorder_point: Mapped[int] = mapped_column(default=0)
    safety_stock: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(default=True)


class PortfolioSku(OrgMixin, Base):
    """Portfolio membership of a SKU. A SKU may belong to several portfolios."""

    __tablename__ = "portfolio_skus"
    __table_args__ = (UniqueConstraint("portfolio_id", "sku_id"),)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    sku_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skus.id"), index=True)


class PlatformListing(OrgMixin, Base):
    """A SKU sold on one platform account by one portfolio at a channel-specific price/fee profile."""

    __tablename__ = "platform_listings"
    __table_args__ = (
        UniqueConstraint("platform_account_id", "sku_id", "portfolio_id"),
        CheckConstraint("price > 0"),
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    sku_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skus.id"), index=True)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    external_listing_id: Mapped[str | None]
    external_sku_ref: Mapped[str | None]
    price: Mapped[Decimal]
    # Fee profile used for expected contribution before actual fees arrive from settlements.
    commission_rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), default=Decimal("0"))
    transaction_fee_rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), default=Decimal("0"))
    shipping_subsidy: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    expected_return_rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), default=Decimal("0"))
    max_discount_rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), default=Decimal("0"))
    active: Mapped[bool] = mapped_column(default=True)


class Supplier(OrgMixin, Base):
    __tablename__ = "suppliers"
    name: Mapped[str]
    marketplace: Mapped[str | None]  # shopee | tokopedia | direct
    store_url: Mapped[str | None]
    approved: Mapped[bool] = mapped_column(default=False)
    reliability_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    notes: Mapped[str | None]


class SupplierOffer(OrgMixin, Base):
    """An approved supplier listing for a SKU (dropship source) with procurement ceiling."""

    __tablename__ = "supplier_offers"
    __table_args__ = (UniqueConstraint("supplier_id", "sku_id", "listing_url"),)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"))
    sku_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skus.id"), index=True)
    marketplace: Mapped[str]
    listing_url: Mapped[str]
    expected_variant: Mapped[str]
    reference_price: Mapped[Decimal]
    price_ceiling: Mapped[Decimal]
    reference_shipping: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    priority: Mapped[int] = mapped_column(default=1)
    approved: Mapped[bool] = mapped_column(default=False)
    last_observed_at: Mapped[datetime | None]
    last_observed_price: Mapped[Decimal | None]
    last_observed_in_stock: Mapped[bool | None]


class SupplierObservation(OrgMixin, Base):
    """Human-recorded observation of a supplier listing (copilot mode). Builds price history."""

    __tablename__ = "supplier_observations"
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    supplier_offer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("supplier_offers.id"), index=True)
    observed_by: Mapped[uuid.UUID]
    price: Mapped[Decimal]
    shipping_cost: Mapped[Decimal]
    in_stock: Mapped[bool]
    variant_matches: Mapped[bool]
    note: Mapped[str | None]
