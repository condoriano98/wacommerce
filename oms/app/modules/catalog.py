"""Canonical products/SKUs, per-channel listings, suppliers, supplier offers and copilot observations."""

import uuid
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.authz import Principal
from app.models.base import utcnow
from app.models.catalog import PlatformListing, PortfolioSku, Product, Sku, Supplier, SupplierObservation, SupplierOffer


def create_product_with_skus(session: Session, principal: Principal, *, name: str, category: str | None,
                             portfolio_ids: list[uuid.UUID], skus: list[dict]) -> tuple[Product, list[Sku]]:
    for pid in portfolio_ids:
        principal.require("catalog.manage", pid)
    product = Product(organization_id=principal.org_id, name=name, category=category)
    session.add(product)
    session.flush()
    out = []
    for spec in skus:
        sku = Sku(organization_id=principal.org_id, product_id=product.id, **spec)
        session.add(sku)
        session.flush()
        for pid in portfolio_ids:
            session.add(PortfolioSku(organization_id=principal.org_id, portfolio_id=pid, sku_id=sku.id))
        out.append(sku)
    audit(session, principal.org_id, "catalog.product_created", "product", product.id, actor_user_id=principal.user_id,
          after={"name": name, "skus": [s["code"] for s in skus]})
    return product, out


def upsert_listing(session: Session, principal: Principal, *, portfolio_id: uuid.UUID, sku_id: uuid.UUID,
                   platform_account_id: uuid.UUID, price: Decimal, **fees) -> PlatformListing:
    principal.require("pricing.manage", portfolio_id)
    from sqlalchemy import select

    listing = session.scalars(select(PlatformListing).where(
        PlatformListing.platform_account_id == platform_account_id, PlatformListing.sku_id == sku_id,
        PlatformListing.portfolio_id == portfolio_id)).first()
    before = {"price": listing.price} if listing else None
    if listing is None:
        listing = PlatformListing(organization_id=principal.org_id, portfolio_id=portfolio_id, sku_id=sku_id,
                                  platform_account_id=platform_account_id, price=price, **fees)
        session.add(listing)
    else:
        listing.price = price
        for k, v in fees.items():
            setattr(listing, k, v)
    audit(session, principal.org_id, "listing.upserted", "platform_listing", listing.id,
          actor_user_id=principal.user_id, portfolio_id=portfolio_id, before=before, after={"price": price, **fees})
    return listing


def create_supplier_offer(session: Session, principal: Principal, *, supplier: dict | None = None,
                          supplier_id: uuid.UUID | None = None, **offer) -> SupplierOffer:
    principal.require("catalog.manage", offer["portfolio_id"])
    if supplier_id is None:
        sup = Supplier(organization_id=principal.org_id, approved=True, **(supplier or {}))
        session.add(sup)
        session.flush()
        supplier_id = sup.id
    o = SupplierOffer(organization_id=principal.org_id, supplier_id=supplier_id, **offer)
    session.add(o)
    session.flush()
    audit(session, principal.org_id, "supplier_offer.created", "supplier_offer", o.id, actor_user_id=principal.user_id,
          portfolio_id=offer["portfolio_id"], after={k: v for k, v in offer.items() if k != "portfolio_id"})
    return o


def record_observation(session: Session, principal: Principal, offer_id: uuid.UUID, *, price: Decimal,
                       shipping_cost: Decimal, in_stock: bool, variant_matches: bool,
                       note: str | None = None) -> SupplierObservation:
    """A human looked at the supplier listing (copilot mode) and recorded what they saw."""
    offer = session.get(SupplierOffer, offer_id)
    if offer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "offer not found")
    if not (principal.can("procurement.operate", offer.portfolio_id) or principal.can("catalog.manage",
                                                                                     offer.portfolio_id)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "procurement or catalog authority required")
    obs = SupplierObservation(organization_id=principal.org_id, portfolio_id=offer.portfolio_id,
                              supplier_offer_id=offer.id, observed_by=principal.user_id, price=price,
                              shipping_cost=shipping_cost, in_stock=in_stock and variant_matches,
                              variant_matches=variant_matches, note=note)
    session.add(obs)
    offer.last_observed_at = utcnow()
    offer.last_observed_price = price
    offer.last_observed_in_stock = in_stock and variant_matches
    return obs
