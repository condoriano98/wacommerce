"""Order state machine, creation from quotes, and marketplace order ingestion."""

import uuid
from collections.abc import Callable
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.models.base import utcnow
from app.models.catalog import PlatformListing
from app.models.customers import CustomerAttribution
from app.models.orders import Order, OrderEvent, OrderItem, Quote, QuoteLine
from app.models.platforms import PlatformAccount
from app.modules import inventory

TRANSITIONS: dict[str, set[str]] = {
    "placed": {"confirmed", "canceled", "exception"},
    "confirmed": {"payment_pending", "cod_confirmed", "payment_verified", "canceled", "exception"},
    "payment_pending": {"payment_verified", "canceled", "exception"},
    "payment_verified": {"routed", "canceled", "exception"},
    "cod_confirmed": {"routed", "canceled", "exception"},
    "routed": {"reserved", "procurement_pending", "platform_fulfilling", "exception", "canceled"},
    "reserved": {"packing", "canceled", "exception"},
    "packing": {"packed", "exception"},
    "packed": {"shipped", "exception"},
    "procurement_pending": {"awaiting_approval", "exception", "canceled"},
    "awaiting_approval": {"purchased", "procurement_pending", "exception", "canceled"},
    "purchased": {"shipped", "exception"},
    "platform_fulfilling": {"shipped", "delivered", "canceled", "exception"},
    "shipped": {"delivered", "returned", "exception"},
    "delivered": {"settled", "reconciled", "returned", "refunded"},
    "settled": {"reconciled", "returned", "refunded"},
    "reconciled": {"returned", "refunded"},
    "exception": {"routed", "reserved", "packing", "procurement_pending", "awaiting_approval", "shipped", "delivered",
                  "canceled", "returned", "refunded"},
    "returned": {"refunded", "reconciled"},
    "canceled": {"refunded"},
    "refunded": set(),
}

# Hooks run inside the same transaction when an order enters a status (conversions, notifications...).
OnEnter = Callable[[Session, Order, str | None, dict], None]
ON_ENTER: dict[str, list[OnEnter]] = {}


def on_enter(*statuses: str) -> Callable[[OnEnter], OnEnter]:
    def register(fn: OnEnter) -> OnEnter:
        for s in statuses:
            ON_ENTER.setdefault(s, []).append(fn)
        return fn

    return register


class InvalidTransition(HTTPException):
    def __init__(self, frm: str, to: str):
        super().__init__(status.HTTP_409_CONFLICT, f"order cannot move from {frm} to {to}")


def transition(session: Session, order: Order, to: str, *, actor_user_id: uuid.UUID | None = None,
               actor_type: str = "user", reason: str | None = None, data: dict | None = None) -> Order:
    frm = order.status
    if to not in TRANSITIONS.get(frm, set()):
        raise InvalidTransition(frm, to)
    order.status = to
    order.version += 1
    if to == "confirmed":
        order.confirmed_at = utcnow()
    if to == "delivered":
        order.delivered_at = utcnow()
    session.add(OrderEvent(organization_id=order.organization_id, order_id=order.id, from_status=frm, to_status=to,
                           actor_user_id=actor_user_id, actor_type=actor_type, reason=reason, data=data or {}))
    session.flush()
    for hook in ON_ENTER.get(to, []):
        hook(session, order, frm, data or {})
    return order


def _order_number(prefix: str) -> str:
    return f"{prefix}-{utcnow():%y%m%d}-{uuid.uuid4().hex[:6].upper()}"


def freeze_attribution(session: Session, order: Order, lookback_days: int = 7) -> None:
    """Latest touch within the lookback window at confirmation time; frozen on the order."""
    from datetime import timedelta

    if order.customer_id is None:
        return
    touch = session.scalars(select(CustomerAttribution).where(
        CustomerAttribution.customer_id == order.customer_id,
        CustomerAttribution.touched_at >= utcnow() - timedelta(days=lookback_days))
        .order_by(CustomerAttribution.touched_at.desc())).first()
    order.attribution_touch_id = touch.id if touch else None


def accept_quote(session: Session, quote_id: uuid.UUID, shipping_address: dict, *, actor_type: str,
                 actor_user_id: uuid.UUID | None = None) -> Order:
    quote = session.get(Quote, quote_id, with_for_update=True)
    if quote is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "quote not found")
    if quote.status != "offered":
        raise HTTPException(status.HTTP_409_CONFLICT, f"quote is {quote.status}; only offered quotes can be accepted")
    if quote.valid_until < utcnow():
        quote.status = "expired"
        raise HTTPException(status.HTTP_409_CONFLICT, "quote expired; request a new quote")
    lines = session.scalars(select(QuoteLine).where(QuoteLine.quote_id == quote.id)).all()
    if any(ln.availability_source == "unavailable" for ln in lines):
        raise HTTPException(status.HTTP_409_CONFLICT, "quote contains unavailable lines")
    order = Order(organization_id=quote.organization_id, brand_id=quote.brand_id, order_number=_order_number("WA"),
                  sales_channel="whatsapp", platform_account_id=quote.platform_account_id,
                  external_order_id=f"quote:{quote.id}", customer_id=quote.customer_id, quote_id=quote.id,
                  payment_method=quote.payment_method or "doku_link", subtotal=quote.subtotal,
                  discount_total=quote.discount_total, shipping_fee=quote.shipping_fee, total=quote.total,
                  shipping_address=shipping_address, placed_at=utcnow())
    session.add(order)
    session.flush()
    for ln in lines:
        session.add(OrderItem(organization_id=order.organization_id, order_id=order.id, portfolio_id=ln.portfolio_id,
                              sku_id=ln.sku_id, listing_id=ln.listing_id, qty=ln.qty, unit_price=ln.unit_price,
                              discount=(ln.unit_price * ln.qty - ln.line_total), line_total=ln.line_total))
    session.flush()
    held = inventory.convert_quote_holds(session, quote.id, order.id)
    owned_needed = {(ln.portfolio_id, ln.sku_id): ln.qty for ln in lines if ln.availability_source == "owned"}
    held_qty: dict = {}
    for h in held:
        held_qty[h.portfolio_id] = held_qty.get(h.portfolio_id, 0) + h.qty
    if owned_needed and sum(held_qty.values()) < sum(owned_needed.values()):
        # Hold expired between quote and acceptance: try to re-reserve, else refuse (no overselling).
        for (pid, sku), qty in owned_needed.items():
            inventory.reserve(session, order.organization_id, portfolio_id=pid, sku_id=sku, qty=qty, kind="order",
                              order_id=order.id)
    quote.status = "accepted"
    freeze_attribution(session, order)
    transition(session, order, "confirmed", actor_user_id=actor_user_id, actor_type=actor_type,
               reason="customer accepted quote", data={"quote_id": str(quote.id)})
    next_status = "cod_confirmed" if order.payment_method == "cod" else "payment_pending"
    transition(session, order, next_status, actor_user_id=actor_user_id, actor_type=actor_type)
    if order.payment_method == "cod":
        order.payment_status = "cod_pending"
    audit(session, order.organization_id, "order.created_from_quote", "order", order.id, actor_type=actor_type,
          actor_user_id=actor_user_id, after={"quote_id": quote.id, "total": order.total})
    return order


MARKETPLACE_STATUS = {
    # normalized marketplace status -> OMS status target
    "unpaid": "placed",
    "ready_to_ship": "platform_fulfilling",
    "shipped": "shipped",
    "delivered": "delivered",
    "completed": "delivered",
    "canceled": "canceled",
    "returned": "returned",
}

_PATHS = {
    "placed": [],
    "platform_fulfilling": ["confirmed", "payment_verified", "routed", "platform_fulfilling"],
    "shipped": ["confirmed", "payment_verified", "routed", "platform_fulfilling", "shipped"],
    "delivered": ["confirmed", "payment_verified", "routed", "platform_fulfilling", "shipped", "delivered"],
}


def ingest_marketplace_order(session: Session, account: PlatformAccount, payload: dict) -> tuple[Order, bool]:
    """Idempotent upsert of a native marketplace order (UNIQUE platform_account_id + external_order_id).

    payload (normalized by the provider adapter): external_order_id, status, buyer_ref, created_at,
    items: [{external_sku_ref, qty, unit_price}], shipping_fee, total."""
    existing = session.scalars(select(Order).where(
        Order.platform_account_id == account.id, Order.external_order_id == payload["external_order_id"])
        .with_for_update()).first()
    target = MARKETPLACE_STATUS.get(payload["status"], "exception")
    created = existing is None
    if created:
        items = []
        for it in payload["items"]:
            listing = session.scalars(select(PlatformListing).where(
                PlatformListing.platform_account_id == account.id,
                PlatformListing.external_sku_ref == it["external_sku_ref"])).first()
            if listing is None:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                    f"unmapped marketplace SKU {it['external_sku_ref']}")
            items.append((listing, it))
        subtotal = sum((Decimal(str(it["unit_price"])) * it["qty"] for _, it in items), Decimal("0"))
        from app.models.org import Portfolio

        brand_id = account.brand_id or session.get(Portfolio, items[0][0].portfolio_id).brand_id
        existing = Order(organization_id=account.organization_id, brand_id=brand_id,
                         order_number=_order_number(account.provider[:3].upper()), sales_channel=account.provider,
                         platform_account_id=account.id, external_order_id=payload["external_order_id"],
                         payment_method="marketplace", subtotal=subtotal,
                         shipping_fee=Decimal(str(payload.get("shipping_fee", 0))),
                         total=Decimal(str(payload.get("total", subtotal))), placed_at=utcnow(),
                         shipping_address={"masked": True})
        session.add(existing)
        session.flush()
        for listing, it in items:
            price = Decimal(str(it["unit_price"]))
            session.add(OrderItem(organization_id=account.organization_id, order_id=existing.id,
                                  portfolio_id=listing.portfolio_id, sku_id=listing.sku_id, listing_id=listing.id,
                                  qty=it["qty"], unit_price=price, line_total=price * it["qty"]))
        session.flush()
        existing.fulfillment_route = "PLATFORM_MANAGED"
    # Walk forward along the allowed path; never skip the state machine.
    path = _PATHS.get(target)
    if path is None:
        if target in TRANSITIONS.get(existing.status, set()):
            transition(session, existing, target, actor_type="system", reason=f"marketplace status {payload['status']}")
    else:
        for step in path:
            if step == existing.status or step not in TRANSITIONS.get(existing.status, set()):
                continue
            transition(session, existing, step, actor_type="system", reason=f"marketplace status {payload['status']}")
    if existing.status in ("payment_verified", "routed", "platform_fulfilling", "shipped", "delivered"):
        existing.payment_status = "paid"
    return existing, created
