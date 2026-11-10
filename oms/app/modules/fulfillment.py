"""Fulfilment (V3.1): per-group routing, platform-managed groups, packing, shipments with items, carrier events,
goods in transit, delivery recognition, failed delivery (spec §8, §9; ADR-010)."""

import uuid
from datetime import timedelta
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.authz import Principal
from app.core.outbox import handler
from app.models.base import utcnow
from app.models.catalog import PlatformListing, Sku, SupplierOffer
from app.models.finance import LedgerEntry
from app.models.fulfillment import (
    FulfillmentGroup,
    FulfillmentGroupItem,
    PurchaseTask,
    RoutingDecision,
    Shipment,
    ShipmentEvent,
    ShipmentItem,
)
from app.models.governance import OutboxMessage
from app.models.inventory import InventoryBalance, InventoryReservation
from app.models.orders import Order, OrderItem, Payment
from app.models.org import Portfolio, RiskPolicy
from app.modules import events, exceptions, inventory, orders
from app.modules.finance import accounts as A
from app.modules.finance import posting

OFFER_FRESHNESS = timedelta(hours=24)
DROPSHIP_ROUTE = {"shopee": "SHOPEE_DROPSHIP", "tokopedia": "TOKOPEDIA_DROPSHIP"}


def org_cash(session: Session) -> Decimal:
    return Decimal(session.scalar(select(func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0)).where(
        LedgerEntry.account_code == A.CASH)))


def _reserve_floor(session: Session) -> Decimal:
    pol = session.scalars(select(RiskPolicy).where(RiskPolicy.key == "min_cash_reserve",
                                                   RiskPolicy.portfolio_id.is_(None))).first()
    return pol.threshold if pol else Decimal("0")


def _group(session: Session, order: Order, portfolio_id, route: str, *, warehouse_id=None, offer_id=None):
    g = session.scalars(select(FulfillmentGroup).where(
        FulfillmentGroup.order_id == order.id, FulfillmentGroup.portfolio_id == portfolio_id,
        FulfillmentGroup.route == route, FulfillmentGroup.warehouse_id.is_(warehouse_id) if warehouse_id is None
        else FulfillmentGroup.warehouse_id == warehouse_id,
        FulfillmentGroup.supplier_offer_id.is_(offer_id) if offer_id is None
        else FulfillmentGroup.supplier_offer_id == offer_id)).first()
    if g is None:
        g = FulfillmentGroup(organization_id=order.organization_id, order_id=order.id, portfolio_id=portfolio_id,
                             route=route, status="ready", warehouse_id=warehouse_id, supplier_offer_id=offer_id,
                             destination=order.shipping_address)
        session.add(g)
        session.flush()
    return g


def _add_group_item(session: Session, g: FulfillmentGroup, item: OrderItem, qty: int) -> None:
    gi = session.scalars(select(FulfillmentGroupItem).where(FulfillmentGroupItem.group_id == g.id,
                                                           FulfillmentGroupItem.order_item_id == item.id)).first()
    if gi:
        gi.qty += qty
    else:
        session.add(FulfillmentGroupItem(organization_id=g.organization_id, portfolio_id=g.portfolio_id,
                                         group_id=g.id, order_item_id=item.id, qty=qty))


# --- routing (runs as a job under the org's system principal: needs org-wide cash visibility) ------------------


@handler("order.route")
def route_job(session: Session, msg: OutboxMessage) -> None:
    order = session.get(Order, uuid.UUID(msg.payload["order_id"]), with_for_update=True)
    route_order(session, order)


def route_order(session: Session, order: Order) -> list[FulfillmentGroup]:
    """Deterministic per-line allocation into fulfilment groups (rules_v2). Mixed sources are allowed; anything that
    cannot be allocated safely opens a manual-review case for that quantity only."""
    items = orders.items_of(session, order.id)
    reasons, inputs, groups = [], {"lines": []}, []
    floor_cash, cash = _reserve_floor(session), org_cash(session)
    dropship_spend = Decimal("0")
    for item in items:
        open_qty = item.qty - item.qty_canceled - item.qty_allocated_owned - item.qty_allocated_dropship \
            - item.qty_allocated_platform
        if open_qty <= 0:
            continue
        # 1) owned stock already reserved for this order line (quote holds converted at acceptance)
        rows = session.execute(select(InventoryBalance.warehouse_id, func.sum(InventoryReservation.qty)).join(
            InventoryBalance, InventoryBalance.id == InventoryReservation.balance_id).where(
            InventoryReservation.order_id == order.id, InventoryReservation.status == "active",
            InventoryReservation.portfolio_id == item.portfolio_id, InventoryBalance.sku_id == item.sku_id)
            .group_by(InventoryBalance.warehouse_id)).all()
        already = sum(gi.qty for gi in session.scalars(select(FulfillmentGroupItem).join(
            FulfillmentGroup, FulfillmentGroup.id == FulfillmentGroupItem.group_id).where(
            FulfillmentGroupItem.order_item_id == item.id, FulfillmentGroup.route == "OWNED_STOCK_BITESHIP")))
        reserved_free = sum(int(q) for _, q in rows) - already
        for wh, q in rows:
            take = min(open_qty, int(q), max(reserved_free, 0))
            if take <= 0:
                continue
            g = _group(session, order, item.portfolio_id, "OWNED_STOCK_BITESHIP", warehouse_id=wh)
            _add_group_item(session, g, item, take)
            item.qty_allocated_owned += take
            open_qty -= take
            reserved_free -= take
            groups.append(g)
        if open_qty <= 0:
            continue
        # 2) dropship: prepaid only, fresh approved offer within ceiling and margin floor, cash above reserve
        if order.payment_method == "cod":
            reasons.append(f"{open_qty} unit(s) of line {item.id}: COD not allowed for dropship")
            continue
        offer = session.scalars(select(SupplierOffer).where(
            SupplierOffer.sku_id == item.sku_id, SupplierOffer.portfolio_id == item.portfolio_id,
            SupplierOffer.approved.is_(True), SupplierOffer.last_observed_in_stock.is_(True),
            SupplierOffer.last_observed_at >= utcnow() - OFFER_FRESHNESS)
            .order_by(SupplierOffer.priority, SupplierOffer.last_observed_price)).first()
        if offer is None:
            reasons.append(f"{open_qty} unit(s) of line {item.id}: no fresh approved supplier offer")
            continue
        landed = offer.last_observed_price * open_qty + offer.reference_shipping
        portfolio = session.get(Portfolio, item.portfolio_id)
        value = posting.item_value(item, open_qty)
        margin = (value - landed) / value if value else Decimal("-1")
        floor = portfolio.target_contribution_margin or Decimal("0")
        inputs["lines"].append({"item": str(item.id), "landed": str(landed), "margin": str(round(margin, 4))})
        if offer.last_observed_price > offer.price_ceiling or margin < floor:
            reasons.append(f"line {item.id}: supplier price above ceiling or margin {margin:.2%} < floor {floor:.2%}")
            continue
        if cash - dropship_spend - landed < floor_cash:
            reasons.append(f"line {item.id}: purchase would breach liquidity reserve")
            continue
        g = _group(session, order, item.portfolio_id, DROPSHIP_ROUTE.get(offer.marketplace, "SHOPEE_DROPSHIP"),
                   offer_id=offer.id)
        _add_group_item(session, g, item, open_qty)
        item.qty_allocated_dropship += open_qty
        dropship_spend += landed
        groups.append(g)
        if not session.scalars(select(PurchaseTask).where(PurchaseTask.group_id == g.id)).first():
            from app.modules import procurement

            procurement.open_task(session, order, g, offer)
    session.add(RoutingDecision(organization_id=order.organization_id, order_id=order.id,
                                route="MIXED" if len({g.route for g in groups}) > 1 else
                                (groups[0].route if groups else "MANUAL_REVIEW"),
                                inputs=inputs, reasons=reasons, decided_by="rules_v2"))
    if reasons:
        exceptions.open_case(session, order.organization_id, "manual_review", subject_type="order",
                             subject_id=str(order.id), order_id=order.id, portfolio_id=None,
                             context={"reasons": reasons})
    session.flush()
    orders.derive_status(session, order)
    return groups


# --- platform-managed (native marketplace) -------------------------------------------------------------------


def create_platform_groups(session: Session, order: Order) -> None:
    for item in orders.items_of(session, order.id):
        g = _group(session, order, item.portfolio_id, "PLATFORM_MANAGED")
        _add_group_item(session, g, item, item.qty)
        item.qty_allocated_platform = item.qty
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
    session.flush()


def apply_marketplace_status(session: Session, order: Order, mp_status: str) -> None:
    groups = session.scalars(select(FulfillmentGroup).where(FulfillmentGroup.order_id == order.id,
                                                            FulfillmentGroup.route == "PLATFORM_MANAGED")).all()
    for g in groups:
        if mp_status == "ready_to_ship" and g.status == "ready":
            g.status = "platform_fulfilling"
        elif mp_status in ("shipped", "delivered", "completed"):
            sh = session.scalars(select(Shipment).where(Shipment.group_id == g.id)).first()
            if sh is None:
                sh = create_shipment(session, g, carrier="platform", external_id=f"{order.external_order_id}:{g.id}")
            apply_shipment_event(session, sh, "picked", order.placed_at, {"source": "marketplace"})
            if mp_status in ("delivered", "completed"):
                apply_shipment_event(session, sh, "delivered", utcnow(), {"source": "marketplace"})
        elif mp_status == "canceled":
            for gi in session.scalars(select(FulfillmentGroupItem).where(FulfillmentGroupItem.group_id == g.id)):
                item = session.get(OrderItem, gi.order_item_id)
                left = item.qty - item.qty_canceled - item.qty_shipped
                if left:
                    item.qty_allocated_platform -= left
                    orders.cancel_item_qty(session, order, item, left, reason="canceled on marketplace",
                                           actor_type="system")
            g.status = "canceled"


# --- packing and shipments (Engine A) --------------------------------------------------------------------------


def _group_for_operator(session: Session, principal: Principal, group_id: uuid.UUID) -> FulfillmentGroup:
    g = session.get(FulfillmentGroup, group_id, with_for_update=True)
    if g is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "fulfilment group not found")
    principal.require("fulfillment.operate", g.portfolio_id)
    return g


def start_packing(session: Session, principal: Principal, group_id: uuid.UUID) -> FulfillmentGroup:
    g = _group_for_operator(session, principal, group_id)
    if g.route != "OWNED_STOCK_BITESHIP" or g.status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, f"group is {g.status} ({g.route})")
    g.status = "packing"
    return g


def mark_packed(session: Session, principal: Principal, group_id: uuid.UUID, packing_cost: Decimal) -> FulfillmentGroup:
    g = _group_for_operator(session, principal, group_id)
    if g.status != "packing":
        raise HTTPException(status.HTTP_409_CONFLICT, f"group is {g.status}")
    g.status = "packed"
    order = session.get(Order, g.order_id)
    posting.expense(session, order.organization_id, account=A.PACKING, amount=packing_cost, source_type="packing",
                    source_id=str(g.id), portfolio_id=g.portfolio_id, order_id=order.id, credit_account=A.CASH,
                    legal_entity_id=posting.entity_for_order(session, order))
    return g


def group_cod_amount(session: Session, g: FulfillmentGroup) -> Decimal:
    order = session.get(Order, g.order_id)
    if order.payment_method != "cod":
        return Decimal("0")
    gis = session.scalars(select(FulfillmentGroupItem).where(FulfillmentGroupItem.group_id == g.id)).all()
    cod = sum((posting.item_value(session.get(OrderItem, gi.order_item_id), gi.qty) for gi in gis), Decimal("0"))
    return cod + sum((posting.shipping_allocation(session, session.get(OrderItem, gi.order_item_id)) for gi in gis),
                     Decimal("0"))


def request_booking(session: Session, principal: Principal, group_id: uuid.UUID, approval_id: uuid.UUID, *,
                    courier_company: str = "jne", courier_type: str = "reg") -> FulfillmentGroup:
    """Queue a paid Biteship booking for a packed group. Paid bookings need an approved ``paid_shipment_booking``
    approval (spec §14); the provider call itself runs in the worker under ADR-015 (no automatic re-booking)."""
    from app.core.outbox import enqueue
    from app.modules import approvals

    g = _group_for_operator(session, principal, group_id)
    if g.route != "OWNED_STOCK_BITESHIP" or g.status != "packed":
        raise HTTPException(status.HTTP_409_CONFLICT, f"group is {g.status} ({g.route})")
    approvals.require_approved(session, approval_id, "paid_shipment_booking", str(g.id))
    g.status = "booking"
    enqueue(session, g.organization_id, "biteship.create_order", f"book:{g.id}",
            {"group_id": str(g.id), "courier_company": courier_company, "courier_type": courier_type})
    return g


@handler("biteship.create_order")
def booking_job(session: Session, msg: OutboxMessage) -> None:
    from app.core import external_ops
    from app.integrations import factory
    from app.integrations.biteship import BookingRequest

    g = session.get(FulfillmentGroup, uuid.UUID(msg.payload["group_id"]), with_for_update=True)
    if g is None or session.scalars(select(Shipment).where(Shipment.group_id == g.id)).first():
        return
    order = session.get(Order, g.order_id)
    cod = group_cod_amount(session, g)
    gis = session.scalars(select(FulfillmentGroupItem).where(FulfillmentGroupItem.group_id == g.id)).all()
    req = BookingRequest(reference_id=f"grp-{g.id}", courier_company=msg.payload["courier_company"],
                         courier_type=msg.payload["courier_type"], destination=dict(g.destination or {}),
                         items=[{"order_item_id": str(gi.order_item_id), "quantity": gi.qty} for gi in gis],
                         cod_amount=cod)
    adapter = factory.carrier(None)
    try:
        resp = external_ops.execute(g.organization_id, "biteship", "create_order", req.reference_id,
                                    lambda: adapter.create_order(req), lookup=None,
                                    summary={"reference_id": req.reference_id, "cod_amount": str(cod)},
                                    subject_type="fulfillment_group", subject_id=str(g.id), order_id=order.id,
                                    portfolio_id=g.portfolio_id, exposure=Decimal(msg.payload.get("est_cost", "0")))
    except external_ops.OperationUncertain:
        g.status = "booking_uncertain"  # human checks the Biteship dashboard and resolves the case
        return
    except external_ops.Rejected:
        g.status = "packed"
        exceptions.open_case(session, g.organization_id, "manual_review", subject_type="fulfillment_group",
                             subject_id=str(g.id), order_id=order.id, portfolio_id=g.portfolio_id,
                             context={"reason": "carrier rejected booking"})
        return
    g.status = "packed"  # create_shipment moves it to booked
    create_shipment(session, g, carrier="biteship", external_id=resp["ref"], waybill=resp.get("waybill_id"),
                    shipping_cost=Decimal(resp.get("price") or "0"))


def create_shipment(session: Session, g: FulfillmentGroup, *, carrier: str, external_id: str | None = None,
                    shipping_cost: Decimal = Decimal("0"), carrier_account_id=None, waybill: str | None = None) -> Shipment:
    cod = group_cod_amount(session, g)
    gis = session.scalars(select(FulfillmentGroupItem).where(FulfillmentGroupItem.group_id == g.id)).all()
    sh = Shipment(organization_id=g.organization_id, order_id=g.order_id, group_id=g.id, portfolio_id=g.portfolio_id,
                  carrier=carrier, external_id=external_id, waybill=waybill, status="booked", cod_amount=cod,
                  shipping_cost=shipping_cost, carrier_account_id=carrier_account_id, booked_at=utcnow())
    session.add(sh)
    session.flush()
    for gi in gis:
        session.add(ShipmentItem(organization_id=g.organization_id, portfolio_id=g.portfolio_id, shipment_id=sh.id,
                                 order_item_id=gi.order_item_id, qty=gi.qty))
    if g.status in ("packed", "ready", "platform_fulfilling", "purchased", "supplier_shipped"):
        g.status = "booked" if g.route == "OWNED_STOCK_BITESHIP" else g.status
    session.flush()
    return sh


# carrier status -> rank (events never move a shipment backwards)
RANK = {"booked": 0, "allocated": 1, "picking_up": 1, "picked": 2, "dropping_off": 3, "on_hold": 3,
        "courier_not_found": 3, "rejected": 3, "return_in_transit": 4, "delivered": 5, "returned": 5, "disposed": 5}
CASE_FOR = {"on_hold": "delivery_failed", "courier_not_found": "delivery_failed", "rejected": "cod_refused",
            "disposed": "lost_or_damaged", "returned": "returned_to_sender"}


def _lines(session: Session, sh: Shipment, g: FulfillmentGroup) -> list[posting.ShippedLine]:
    out = []
    for si in session.scalars(select(ShipmentItem).where(ShipmentItem.shipment_id == sh.id)):
        out.append(posting.ShippedLine(session.get(OrderItem, si.order_item_id), si.qty, si.unit_cost or Decimal("0"),
                                       owned=g.route in ("OWNED_STOCK_BITESHIP", "PLATFORM_MANAGED")))
    return out


def _revenue_account(session: Session, order: Order) -> str:
    if order.payment_method == "cod":
        return A.COD_RECEIVABLE
    if order.payment_method == "marketplace":
        return A.MARKETPLACE_RECEIVABLE
    if order.payment_status in ("captured", "bank_matched"):
        return A.CUSTOMER_ADVANCES
    if order.payment_status == "released":  # approved release-on-attestation policy (F3b)
        return A.TRANSFER_RECEIVABLE
    raise HTTPException(status.HTTP_409_CONFLICT, f"cannot recognise revenue: payment is {order.payment_status}")


def apply_shipment_event(session: Session, sh: Shipment, carrier_status: str, occurred_at, raw: dict,
                         cod_fee: Decimal | None = None) -> bool:
    """Idempotent on (shipment, status, occurred_at); out-of-order events never regress state. Returns True when
    the event changed state."""
    res = session.execute(insert(ShipmentEvent).values(
        id=uuid.uuid4(), created_at=utcnow(), organization_id=sh.organization_id, shipment_id=sh.id,
        order_id=sh.order_id, status=carrier_status, occurred_at=occurred_at, raw=raw)
        .on_conflict_do_nothing(index_elements=["shipment_id", "status", "occurred_at"]))
    if res.rowcount == 0:
        return False
    if RANK.get(carrier_status, 0) < RANK.get(sh.status, 0) or sh.status in ("delivered", "returned"):
        return False  # stale/out-of-order or terminal
    order = session.get(Order, sh.order_id, with_for_update=True)
    g = session.get(FulfillmentGroup, sh.group_id)
    lines = _lines(session, sh, g)
    if carrier_status == "picked":
        for ln, si in zip(lines, session.scalars(select(ShipmentItem).where(ShipmentItem.shipment_id == sh.id)),
                          strict=True):
            if g.route in ("OWNED_STOCK_BITESHIP", "PLATFORM_MANAGED") and _has_reservation(session, order, ln.item):
                si.unit_cost = inventory.pick_from_reservations(session, order.organization_id, order.id,
                                                                ln.item.portfolio_id, ln.item.sku_id, ln.qty,
                                                                str(sh.id))
                ln.unit_cost = si.unit_cost
                ln.item.unit_cost = si.unit_cost  # moving-average cost at pickup (used for returns)
            ln.item.qty_shipped += ln.qty
        session.flush()
        posting.picked_up(session, order, str(sh.id), lines)
        if sh.shipping_cost:
            posting.carrier_cost(session, order, str(sh.id), sh.shipping_cost,
                                 cod_deducted=order.payment_method == "cod", portfolio_id=g.portfolio_id)
        g.status = "in_transit"
        events.emit(session, "shipment_picked", order=order, shipment=sh)
    elif carrier_status == "delivered":
        if sh.status not in ("picked", "dropping_off", "on_hold"):
            apply_shipment_event(session, sh, "picked", occurred_at - timedelta(seconds=1), {"implied": True})
            lines = _lines(session, sh, g)
        for ln in lines:
            if ln.owned and ln.unit_cost:
                inventory.deliver_in_transit(session, ln.item.portfolio_id, ln.item.sku_id, ln.qty)
            ln.item.qty_delivered += ln.qty
        session.flush()
        posting.delivered(session, order, str(sh.id), lines, revenue_debit_account=_revenue_account(session, order))
        if order.payment_method == "cod" and cod_fee:
            posting.cod_fee(session, order, str(sh.id), cod_fee, g.portfolio_id)
        if order.payment_method == "marketplace":
            for ln in lines:
                listing = session.get(PlatformListing, ln.item.listing_id) if ln.item.listing_id else None
                if listing:
                    rate = listing.commission_rate + listing.transaction_fee_rate
                    fee = posting.q(posting.item_value(ln.item, ln.qty) * rate)
                    if fee:
                        posting.marketplace_expected_fee(session, order, ln.item, fee, f"{sh.id}:{ln.item.id}")
        sh.delivered_at = occurred_at
        g.status = "delivered"
        order.delivered_at = occurred_at
        events.emit(session, "shipment_delivered", order=order, shipment=sh)
    elif carrier_status == "returned" and sh.status not in ("delivered",):
        for ln in lines:
            if ln.owned and ln.unit_cost:
                inventory.return_in_transit(session, order.organization_id, ln.item.portfolio_id, ln.item.sku_id,
                                            ln.qty, str(sh.id))
            ln.item.qty_shipped -= ln.qty
        session.flush()
        return_cost = Decimal(str(raw.get("return_cost", sh.shipping_cost)))
        posting.returned_to_sender(session, order, str(sh.id), lines, Decimal("0"), return_cost, g.portfolio_id)
        g.status = "failed"
        events.emit(session, "shipment_returned", order=order, shipment=sh)
    sh.status = carrier_status
    case = CASE_FOR.get(carrier_status)
    if case:
        exceptions.open_case(session, order.organization_id, case, subject_type="shipment", subject_id=str(sh.id),
                             order_id=order.id, portfolio_id=g.portfolio_id,
                             context={"carrier_status": carrier_status})
    orders.derive_status(session, order)
    return True


def _has_reservation(session: Session, order: Order, item: OrderItem) -> bool:
    return session.scalars(select(InventoryReservation.id).join(
        InventoryBalance, InventoryBalance.id == InventoryReservation.balance_id).where(
        InventoryReservation.order_id == order.id, InventoryReservation.status == "active",
        InventoryReservation.portfolio_id == item.portfolio_id, InventoryBalance.sku_id == item.sku_id)).first() \
        is not None


def shipped_qty(session: Session, item: OrderItem, route: str) -> int:
    stmt = select(func.coalesce(func.sum(ShipmentItem.qty), 0)).join(
        Shipment, Shipment.id == ShipmentItem.shipment_id).join(
        FulfillmentGroup, FulfillmentGroup.id == Shipment.group_id).where(
        ShipmentItem.order_item_id == item.id, FulfillmentGroup.route == route,
        Shipment.status.in_(("picked", "dropping_off", "delivered", "on_hold")))
    return int(session.scalar(stmt) or 0)


def reduce_group_item(session: Session, item: OrderItem, route: str, qty: int) -> None:
    stmt = select(FulfillmentGroupItem).join(FulfillmentGroup, FulfillmentGroup.id == FulfillmentGroupItem.group_id) \
        .where(FulfillmentGroupItem.order_item_id == item.id)
    stmt = stmt.where(FulfillmentGroup.route.like("%DROPSHIP")) if route == "DROPSHIP" else \
        stmt.where(FulfillmentGroup.route == route)
    for gi in session.scalars(stmt.with_for_update()):
        take = min(qty, gi.qty)
        if take == gi.qty:
            session.delete(gi)
        else:
            gi.qty -= take
        qty -= take
        if qty == 0:
            break


def payment_for(session: Session, order: Order) -> Payment | None:
    return session.scalars(select(Payment).where(Payment.order_id == order.id).order_by(Payment.created_at.desc())).first()
