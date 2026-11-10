"""Fulfillment lifecycle: routing job, packing workflow, shipment events, delivery -> revenue recognition."""

import uuid
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.authz import Principal
from app.core.outbox import enqueue, handler
from app.models.base import utcnow
from app.models.catalog import Sku
from app.models.fulfillment import ProcurementTask, Shipment, ShipmentEvent
from app.models.governance import OutboxMessage
from app.models.orders import Order, OrderItem
from app.modules import exceptions, inventory, orders, routing
from app.modules.finance import accounts as A
from app.modules.finance import posting

# --- routing is a job: it needs org-wide cash visibility regardless of who confirmed the payment -------------


@orders.on_enter("payment_verified", "cod_confirmed")
def _enqueue_routing(session: Session, order: Order, frm, data) -> None:
    if order.sales_channel == "whatsapp":
        enqueue(session, order.organization_id, "order.route", f"route:{order.id}", {"order_id": str(order.id)})


@handler("order.route")
def route_job(session: Session, msg: OutboxMessage) -> None:
    order = session.get(Order, uuid.UUID(msg.payload["order_id"]), with_for_update=True)
    if order.status not in ("payment_verified", "cod_confirmed"):
        return  # already routed or canceled: idempotent no-op
    decision = routing.route_order(session, order)
    if decision.route == "MANUAL_REVIEW":
        orders.transition(session, order, "exception", actor_type="system", reason="; ".join(decision.reasons))
        return
    orders.transition(session, order, "routed", actor_type="system", reason=decision.route)
    if decision.route == "OWNED_STOCK_BITESHIP":
        orders.transition(session, order, "reserved", actor_type="system")
    elif decision.route.endswith("_DROPSHIP"):
        marketplace = decision.route.split("_")[0].lower()
        for item in session.scalars(select(OrderItem).where(OrderItem.order_id == order.id)):
            session.add(ProcurementTask(organization_id=order.organization_id, portfolio_id=item.portfolio_id,
                                        order_id=order.id, order_item_id=item.id, marketplace=marketplace,
                                        destination=order.shipping_address))
        orders.transition(session, order, "procurement_pending", actor_type="system")


# --- native marketplace orders share owned stock: reserve when the platform asks us to ship ------------------


@orders.on_enter("platform_fulfilling")
def _reserve_for_marketplace(session: Session, order: Order, frm, data) -> None:
    for item in session.scalars(select(OrderItem).where(OrderItem.order_id == order.id)):
        sku = session.get(Sku, item.sku_id)
        if sku.inventory_policy == "dropship_only":
            continue
        try:
            with session.begin_nested():
                inventory.reserve(session, order.organization_id, portfolio_id=item.portfolio_id, sku_id=item.sku_id,
                                  qty=item.qty, kind="order", order_id=order.id)
        except inventory.InsufficientStock as exc:
            exceptions.open_case(session, order.organization_id, "oversold_marketplace", subject_type="order",
                                 subject_id=str(order.id), order_id=order.id, portfolio_id=item.portfolio_id,
                                 context={"sku": sku.code, "detail": exc.detail})


@orders.on_enter("shipped")
def _consume_stock(session: Session, order: Order, frm, data) -> None:
    costs = inventory.ship_reserved(session, order.organization_id, order.id)
    for item in session.scalars(select(OrderItem).where(OrderItem.order_id == order.id)):
        if (item.portfolio_id, item.sku_id) in costs:
            item.unit_cost = costs[(item.portfolio_id, item.sku_id)]


@orders.on_enter("delivered")
def _recognise_revenue(session: Session, order: Order, frm, data) -> None:
    dropship = (order.fulfillment_route or "").endswith("_DROPSHIP")
    unit_costs = {}
    for item in session.scalars(select(OrderItem).where(OrderItem.order_id == order.id)):
        unit_costs[item.id] = (item.unit_cost or Decimal("0"), A.SUPPLIER_PREPAYMENTS if dropship else A.INVENTORY)
    posting.revenue_on_delivery(session, order, unit_costs)
    if order.payment_method == "cod":
        order.payment_status = "collected"
    order.settlement_status = "expected" if order.payment_method in ("cod", "marketplace") else "settled"


# --- packing workflow (packing admin) ------------------------------------------------------------------------


def _order_for_packer(session: Session, principal: Principal, order_id: uuid.UUID) -> Order:
    principal.require("fulfillment.operate")
    order = session.get(Order, order_id, with_for_update=True)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "order not found")
    return order


def start_packing(session: Session, principal: Principal, order_id: uuid.UUID) -> Order:
    order = _order_for_packer(session, principal, order_id)
    return orders.transition(session, order, "packing", actor_user_id=principal.user_id)


def mark_packed(session: Session, principal: Principal, order_id: uuid.UUID, packing_cost: Decimal) -> Order:
    order = _order_for_packer(session, principal, order_id)
    orders.transition(session, order, "packed", actor_user_id=principal.user_id)
    portfolio_id = session.scalars(select(OrderItem.portfolio_id).where(OrderItem.order_id == order.id)).first()
    posting.expense(session, order.organization_id, account=A.PACKING, amount=packing_cost,
                    source_type="packing", source_id=str(order.id), portfolio_id=portfolio_id, order_id=order.id,
                    credit_account=A.CASH, memo="packing materials")
    return order


SHIPMENT_STATUS_MAP = {
    # carrier status -> (order transition target or None, exception case type or None)
    "picked": ("shipped", None),
    "dropping_off": (None, None),
    "delivered": ("delivered", None),
    "on_hold": (None, "delivery_failed"),
    "rejected": (None, "cod_refused"),
    "courier_not_found": (None, "delivery_failed"),
    "returned": ("returned", "returned_to_sender"),
    "return_in_transit": (None, "returned_to_sender"),
    "cancelled": (None, "delivery_failed"),
    "disposed": (None, "lost_or_damaged"),
}


def apply_shipment_event(session: Session, shipment: Shipment, carrier_status: str, occurred_at, raw: dict) -> None:
    """Idempotent on (shipment, status, occurred_at). Drives order state and exception cases."""
    from sqlalchemy.dialects.postgresql import insert

    res = session.execute(insert(ShipmentEvent).values(
        id=uuid.uuid4(), created_at=utcnow(), organization_id=shipment.organization_id, shipment_id=shipment.id,
        order_id=shipment.order_id, status=carrier_status, occurred_at=occurred_at, raw=raw)
        .on_conflict_do_nothing(index_elements=["shipment_id", "status", "occurred_at"]))
    if res.rowcount == 0:
        return
    shipment.status = carrier_status
    order = session.get(Order, shipment.order_id, with_for_update=True)
    target, case_type = SHIPMENT_STATUS_MAP.get(carrier_status, (None, None))
    if carrier_status == "delivered":
        shipment.delivered_at = occurred_at
    if target and target in orders.TRANSITIONS.get(order.status, set()):
        orders.transition(session, order, target, actor_type="system", reason=f"carrier {carrier_status}",
                          data={"shipment_id": str(shipment.id)})
    if case_type:
        portfolio_id = session.scalars(select(OrderItem.portfolio_id).where(OrderItem.order_id == order.id)).first()
        exceptions.open_case(session, order.organization_id, case_type, subject_type="shipment",
                             subject_id=str(shipment.id), order_id=order.id, portfolio_id=portfolio_id,
                             context={"carrier_status": carrier_status})
        can_flag = "exception" in orders.TRANSITIONS.get(order.status, set())
        if case_type in ("delivery_failed", "cod_refused") and can_flag:
            orders.transition(session, order, "exception", actor_type="system", reason=f"carrier {carrier_status}")
