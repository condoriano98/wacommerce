"""Orders (V3.1): line-level quantity pipeline, derived aggregate status, creation from quotes, cancellations and
idempotent marketplace ingestion (spec §8, ADR-010)."""

import uuid
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.outbox import enqueue
from app.models.base import utcnow
from app.models.catalog import PlatformListing
from app.models.customers import CustomerAttribution
from app.models.governance import ExceptionCase
from app.models.orders import Order, OrderEvent, OrderItem, Quote, QuoteLine
from app.models.org import Portfolio
from app.models.platforms import PlatformAccount
from app.modules import events, inventory
from app.modules.finance import posting

PREPAID_READY = {"captured", "bank_matched", "released"}


def _event(session: Session, order: Order, to_status: str, *, actor_type: str = "system", actor_user_id=None,
           reason: str | None = None, data: dict | None = None) -> None:
    session.add(OrderEvent(organization_id=order.organization_id, order_id=order.id, from_status=order.status,
                           to_status=to_status, actor_user_id=actor_user_id, actor_type=actor_type, reason=reason,
                           data=data or {}))


def items_of(session: Session, order_id) -> list[OrderItem]:
    return list(session.scalars(select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.created_at)))


def derive_status(session: Session, order: Order) -> str:
    """Aggregate order status is computed, never set directly (spec §8)."""
    items = items_of(session, order.id)
    live = [i for i in items if i.qty_canceled < i.qty]
    open_case = session.scalars(select(ExceptionCase.id).where(
        ExceptionCase.order_id == order.id, ExceptionCase.severity.in_(("high", "critical")),
        ExceptionCase.status.not_in(("resolved", "written_off")))).first()
    if not live:
        new = "canceled"
    elif open_case:
        new = "exception"
    else:
        ordered = sum(i.qty - i.qty_canceled for i in live)
        delivered = sum(i.qty_delivered for i in live)
        returned = sum(i.qty_returned for i in live)
        shipped = sum(i.qty_shipped for i in live)
        allocated = sum(i.qty_allocated_owned + i.qty_allocated_dropship + i.qty_allocated_platform for i in live)
        if returned and returned == delivered == ordered:
            new = "returned"
        elif returned:
            new = "partially_returned"
        elif delivered == ordered:
            new = "closed" if order.settlement_status in ("matched", "adjusted", "closed") else "delivered"
        elif delivered:
            new = "partially_delivered"
        elif shipped or allocated:
            new = "in_fulfillment"
        elif order.payment_method not in ("cod", "marketplace") and order.payment_status not in PREPAID_READY:
            new = "awaiting_payment"
        else:
            new = "confirmed"
    if new != order.status:
        _event(session, order, new)
        order.status = new
        order.version += 1
    return new


def _order_number(prefix: str) -> str:
    return f"{prefix}-{utcnow():%y%m%d}-{uuid.uuid4().hex[:6].upper()}"


def freeze_attribution(session: Session, order: Order, lookback_days: int = 7) -> None:
    from datetime import timedelta

    if order.customer_id is None:
        return
    touch = session.scalars(select(CustomerAttribution).where(
        CustomerAttribution.customer_id == order.customer_id,
        CustomerAttribution.touched_at >= utcnow() - timedelta(days=lookback_days))
        .order_by(CustomerAttribution.touched_at.desc())).first()
    order.attribution_touch_id = touch.id if touch else None


def request_routing(session: Session, order: Order) -> None:
    enqueue(session, order.organization_id, "order.route", f"route:{order.id}:{order.version}",
            {"order_id": str(order.id)})


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
                  shipping_address=shipping_address, placed_at=utcnow(), status="placed",
                  payment_status="cod" if quote.payment_method == "cod" else "unpaid", confirmed_at=utcnow())
    session.add(order)
    session.flush()
    for ln in lines:
        session.add(OrderItem(organization_id=order.organization_id, order_id=order.id, portfolio_id=ln.portfolio_id,
                              sku_id=ln.sku_id, listing_id=ln.listing_id, qty=ln.qty, unit_price=ln.unit_price,
                              discount=(ln.unit_price * ln.qty - ln.line_total), line_total=ln.line_total))
    session.flush()
    held = inventory.convert_quote_holds(session, quote.id, order.id)
    owned = [ln for ln in lines if ln.availability_source == "owned"]
    if owned and sum(h.qty for h in held) < sum(ln.qty for ln in owned):
        for ln in owned:  # hold expired between quote and acceptance: re-reserve or refuse (no overselling)
            inventory.reserve(session, order.organization_id, portfolio_id=ln.portfolio_id, sku_id=ln.sku_id,
                              qty=ln.qty, kind="order", order_id=order.id)
    quote.status = "accepted"
    freeze_attribution(session, order)
    posting.allocate_order_amounts(session, order)
    _event(session, order, "confirmed", actor_type=actor_type, actor_user_id=actor_user_id,
           reason="customer accepted quote", data={"quote_id": str(quote.id)})
    order.status = "confirmed"
    derive_status(session, order)
    events.emit(session, "order_confirmed", order=order)
    if order.payment_method == "cod":
        events.emit(session, "payment_released", order=order, payment=None, basis="cod")
    audit(session, order.organization_id, "order.created_from_quote", "order", order.id, actor_type=actor_type,
          actor_user_id=actor_user_id, after={"quote_id": quote.id, "total": order.total})
    return order


@events.subscribe("payment_released")
def _route_when_released(session: Session, data: dict) -> None:
    request_routing(session, data["order"])


def cancel_item_qty(session: Session, order: Order, item: OrderItem, qty: int, *, reason: str, actor_user_id=None,
                    actor_type: str = "user") -> Decimal:
    """Cancel unshipped quantity of a line. Prepaid money for it becomes a refund obligation (F6); shipping income
    is reallocated to the remaining lines. Returns the amount owed back to the customer."""
    from app.modules import fulfillment

    unshipped = item.qty - item.qty_canceled - item.qty_shipped
    if qty <= 0 or qty > unshipped:
        raise HTTPException(status.HTTP_409_CONFLICT, f"only {unshipped} unshipped units can be canceled")
    remaining = qty
    unallocated = item.qty - item.qty_canceled - item.qty_allocated_owned - item.qty_allocated_dropship \
        - item.qty_allocated_platform
    remaining -= min(remaining, unallocated)
    owned_free = item.qty_allocated_owned - fulfillment.shipped_qty(session, item, "OWNED_STOCK_BITESHIP")
    take = min(remaining, owned_free)
    if take > 0:
        item.qty_allocated_owned -= take
        _release_reservations(session, order.id, item, take)
        fulfillment.reduce_group_item(session, item, "OWNED_STOCK_BITESHIP", take)
        remaining -= take
    take = min(remaining, item.qty_allocated_dropship - item.qty_procured)
    if take > 0:
        item.qty_allocated_dropship -= take
        fulfillment.reduce_group_item(session, item, "DROPSHIP", take)
        remaining -= take
    take = min(remaining, item.qty_allocated_platform - fulfillment.shipped_qty(session, item, "PLATFORM_MANAGED"))
    if take > 0:
        item.qty_allocated_platform -= take
        _release_reservations(session, order.id, item, take)
        fulfillment.reduce_group_item(session, item, "PLATFORM_MANAGED", take)
        remaining -= take
    if remaining:
        raise HTTPException(status.HTTP_409_CONFLICT, "quantity is already procured or in transit; use a return")
    item.qty_canceled += qty
    session.flush()
    amount = posting.item_value(item, qty)
    if order.payment_status in ("captured", "bank_matched"):
        posting.canceled_before_shipment(session, order, item, amount, f"{item.id}:{item.qty_canceled}")
    posting.reallocate_shipping_after_cancel(session, order)
    fulfillment.refresh_cod_amounts(session, order)
    _event(session, order, order.status, actor_type=actor_type, actor_user_id=actor_user_id, reason=reason,
           data={"canceled_item": str(item.id), "qty": qty})
    derive_status(session, order)
    events.emit(session, "items_canceled", order=order, items=[(item, qty)])
    return amount if order.payment_status in ("captured", "bank_matched") else Decimal("0")


def _release_reservations(session: Session, order_id, item: OrderItem, qty: int) -> None:
    from app.models.inventory import InventoryBalance, InventoryReservation

    rows = session.scalars(select(InventoryReservation).join(
        InventoryBalance, InventoryBalance.id == InventoryReservation.balance_id).where(
        InventoryReservation.order_id == order_id, InventoryReservation.status == "active",
        InventoryReservation.portfolio_id == item.portfolio_id, InventoryBalance.sku_id == item.sku_id)).all()
    remaining = qty
    for r in rows:
        if remaining == 0:
            break
        take = min(r.qty, remaining)
        bal = session.get(InventoryBalance, r.balance_id, with_for_update=True)
        bal.reserved -= take
        if take == r.qty:
            r.status = "released"
        else:
            r.qty -= take
        remaining -= take


MARKETPLACE_STATUS = {"unpaid", "ready_to_ship", "shipped", "delivered", "completed", "canceled", "returned"}


def ingest_marketplace_order(session: Session, account: PlatformAccount, payload: dict) -> tuple[Order, bool]:
    """Idempotent upsert of a native marketplace order (UNIQUE platform_account_id + external_order_id).

    payload (normalized by the adapter): external_order_id, status, items[{external_sku_ref, qty, unit_price}],
    shipping_fee, total. Fulfilment of native orders is platform-managed; owned stock is reserved and consumed so
    shared inventory stays correct across channels."""
    from app.modules import fulfillment

    existing = session.scalars(select(Order).where(
        Order.platform_account_id == account.id, Order.external_order_id == payload["external_order_id"])
        .with_for_update()).first()
    created = existing is None
    if created:
        resolved = []
        for it in payload["items"]:
            listing = session.scalars(select(PlatformListing).where(
                PlatformListing.platform_account_id == account.id,
                PlatformListing.external_sku_ref == it["external_sku_ref"])).first()
            if listing is None:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                    f"unmapped marketplace SKU {it['external_sku_ref']}")
            resolved.append((listing, it))
        subtotal = sum((Decimal(str(it["unit_price"])) * it["qty"] for _, it in resolved), Decimal("0"))
        brand_id = account.brand_id or session.get(Portfolio, resolved[0][0].portfolio_id).brand_id
        existing = Order(organization_id=account.organization_id, brand_id=brand_id,
                         order_number=_order_number(account.provider[:3].upper()), sales_channel=account.provider,
                         platform_account_id=account.id, external_order_id=payload["external_order_id"],
                         payment_method="marketplace", payment_status="marketplace", subtotal=subtotal,
                         shipping_fee=Decimal(str(payload.get("shipping_fee", 0))),
                         total=Decimal(str(payload.get("total", subtotal))), placed_at=utcnow(),
                         shipping_address={"masked": True}, status="placed", confirmed_at=utcnow())
        session.add(existing)
        session.flush()
        for listing, it in resolved:
            price = Decimal(str(it["unit_price"]))
            session.add(OrderItem(organization_id=account.organization_id, order_id=existing.id,
                                  portfolio_id=listing.portfolio_id, sku_id=listing.sku_id, listing_id=listing.id,
                                  qty=it["qty"], unit_price=price, line_total=price * it["qty"]))
        session.flush()
        posting.allocate_order_amounts(session, existing)
        _event(session, existing, "confirmed", reason="marketplace order ingested")
        existing.status = "confirmed"
        fulfillment.create_platform_groups(session, existing)
    fulfillment.apply_marketplace_status(session, existing, payload["status"])
    derive_status(session, existing)
    return existing, created
