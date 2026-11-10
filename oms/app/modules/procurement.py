"""Human-operated dropship procurement (copilot) — spec §8, §27.5, ADR-005.

Nobody and nothing automates the marketplace site. The OMS opens a task per dropship fulfilment group, the operator
records what they saw, the OMS checks ceiling/margin, an approver authorises the amount, the operator buys manually
and records the external order with quantities. Over-procurement is impossible: purchase lines update
``order_items.qty_procured`` through a trigger under row lock and a CHECK caps it at the dropship allocation.
"""

import uuid
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.authz import Principal
from app.models.catalog import SupplierOffer
from app.models.fulfillment import ExternalPurchase, FulfillmentGroup, FulfillmentGroupItem, PurchaseLine, PurchaseTask
from app.models.governance import HumanApproval
from app.models.orders import Order, OrderItem
from app.models.org import Portfolio
from app.modules import approvals, capital, catalog, exceptions, orders
from app.modules.finance import posting


class OverProcurement(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status.HTTP_409_CONFLICT, detail)


def open_task(session: Session, order: Order, group: FulfillmentGroup, offer: SupplierOffer) -> PurchaseTask:
    task = PurchaseTask(organization_id=order.organization_id, portfolio_id=group.portfolio_id, order_id=order.id,
                        group_id=group.id, supplier_offer_id=offer.id, marketplace=offer.marketplace, status="open")
    session.add(task)
    session.flush()
    group.status = "purchase_pending"
    return task


def _group_lines(session: Session, task: PurchaseTask) -> list[tuple[OrderItem, int]]:
    return [(session.get(OrderItem, gi.order_item_id), gi.qty) for gi in
            session.scalars(select(FulfillmentGroupItem).where(FulfillmentGroupItem.group_id == task.group_id))]


def observe(session: Session, principal: Principal, task_id: uuid.UUID, *, price: Decimal, shipping_cost: Decimal,
            in_stock: bool, variant_matches: bool, note: str | None = None) -> PurchaseTask:
    """Operator records the listing as seen in their own browser. Blocks on stock/variant/ceiling/margin breaches."""
    task = session.get(PurchaseTask, task_id, with_for_update=True)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    obs = catalog.record_observation(session, principal, task.supplier_offer_id, price=price,
                                     shipping_cost=shipping_cost, in_stock=in_stock, variant_matches=variant_matches,
                                     note=note)
    session.flush()
    task.observation_id = obs.id
    offer = session.get(SupplierOffer, task.supplier_offer_id)
    lines = _group_lines(session, task)
    qty = sum(q for _, q in lines)
    landed = price * qty + shipping_cost
    value = sum((posting.item_value(i, q) for i, q in lines), Decimal("0"))
    floor = session.get(Portfolio, task.portfolio_id).target_contribution_margin or Decimal("0")
    margin = (value - landed) / value if value else Decimal("-1")
    task.landed_cost, task.expected_contribution = landed, value - landed
    order = session.get(Order, task.order_id)
    case = None
    if not (in_stock and variant_matches):
        task.status, task.block_reason, case = "blocked", "supplier out of stock or variant mismatch", "supplier_out_of_stock"
    elif price > offer.price_ceiling or margin < floor:
        task.status, task.block_reason, case = "blocked", f"price {price} / margin {margin:.2%} breach", \
            "supplier_price_drift"
    else:
        task.status = "awaiting_approval"
        commitment = capital.commit(session, principal.org_id, portfolio_id=task.portfolio_id, category="inventory",
                                    purpose="dropship purchase", source_type="purchase_task", source_id=str(task.id),
                                    amount=landed)
        task.commitment_id = commitment.id
        approval = approvals.request_approval(
            session, principal, kind="procurement_purchase", subject_type="purchase_task", subject_id=str(task.id),
            amount=landed, portfolio_id=task.portfolio_id, idempotency_key=f"purchase:{task.id}:{landed}",
            payload={"order": order.order_number, "marketplace": task.marketplace, "listing": offer.listing_url,
                     "qty": qty, "observed_price": str(price), "ceiling": str(offer.price_ceiling),
                     "expected_contribution": str(task.expected_contribution)})
        task.approval_id = approval.id
    if case:
        exceptions.open_case(session, order.organization_id, case, subject_type="purchase_task",
                             subject_id=str(task.id), order_id=order.id, portfolio_id=task.portfolio_id,
                             context={"reason": task.block_reason})
    return task


def record_purchase(session: Session, principal: Principal, task_id: uuid.UUID, *, external_order_id: str,
                    amount_paid: Decimal, lines: list[tuple[uuid.UUID, int, Decimal]], evidence_ref: str,
                    buyer_account_ref: str | None = None) -> ExternalPurchase:
    """Operator bought manually and records the result. lines = [(order_item_id, qty, unit_price)].
    May be called several times (split purchases) up to the approved amount and the allocated quantities."""
    if principal.is_service:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "purchases are recorded by a human operator")
    task = session.get(PurchaseTask, task_id, with_for_update=True)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    principal.require("procurement.operate", task.portfolio_id)
    approval = session.get(HumanApproval, task.approval_id) if task.approval_id else None
    if approval is None or approval.status not in ("approved", "executed"):
        raise HTTPException(status.HTTP_409_CONFLICT, "purchase requires an approved procurement approval")
    spent = session.scalar(select(func.coalesce(func.sum(ExternalPurchase.amount_paid), 0)).where(
        ExternalPurchase.purchase_task_id == task.id))
    if Decimal(spent) + amount_paid > approval.amount:
        raise HTTPException(status.HTTP_409_CONFLICT, f"amount exceeds approved {approval.amount}")
    order = session.get(Order, task.order_id)
    purchase = ExternalPurchase(organization_id=task.organization_id, portfolio_id=task.portfolio_id,
                                purchase_task_id=task.id, order_id=task.order_id, marketplace=task.marketplace,
                                external_order_id=external_order_id, amount_paid=amount_paid,
                                evidence_ref=evidence_ref, purchased_by=principal.user_id,
                                marketplace_account_ref=buyer_account_ref)
    session.add(purchase)
    try:
        session.flush()
        for item_id, qty, unit_price in lines:
            item = session.get(OrderItem, item_id)
            if item is None or item.order_id != task.order_id:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "line does not belong to this order")
            session.add(PurchaseLine(organization_id=task.organization_id, portfolio_id=task.portfolio_id,
                                     purchase_task_id=task.id, external_purchase_id=purchase.id,
                                     order_item_id=item_id, qty=qty, unit_price=unit_price))
            session.flush()  # trigger: qty_procured += qty (row lock); CHECK caps at the dropship allocation
            session.refresh(item)
            item.unit_cost = unit_price
    except IntegrityError as exc:
        msg = str(exc.orig)
        if "procured_le_allocated" in msg:
            raise OverProcurement("purchase exceeds the quantity allocated to dropship") from exc
        if "external_purchases_marketplace_external_order_id_key" in msg or "unique" in msg.lower():
            raise HTTPException(status.HTTP_409_CONFLICT, "external order already recorded") from exc
        raise
    posting.supplier_paid(session, order, str(purchase.id), amount_paid, task.portfolio_id)
    if task.commitment_id:
        capital.consume(session, task.commitment_id, amount_paid)
    remaining = sum(i.qty_allocated_dropship - i.qty_procured for i, _ in _group_lines(session, task))
    task.status = "purchased" if remaining == 0 else "approved"
    if remaining == 0 and approval.status == "approved":
        approval.status = "executed"
    group = session.get(FulfillmentGroup, task.group_id)
    group.status = "purchased" if remaining == 0 else group.status
    audit(session, principal.org_id, "procurement.purchase_recorded", "purchase_task", task.id,
          actor_user_id=principal.user_id, portfolio_id=task.portfolio_id,
          after={"external_order_id": external_order_id, "amount": amount_paid, "lines": len(lines)})
    orders.derive_status(session, order)
    return purchase


def supplier_shipped(session: Session, principal: Principal, task_id: uuid.UUID, tracking_number: str):
    from app.modules import fulfillment

    task = session.get(PurchaseTask, task_id, with_for_update=True)
    principal.require("procurement.operate", task.portfolio_id)
    group = session.get(FulfillmentGroup, task.group_id)
    task.status = group.status = "supplier_shipped"
    sh = fulfillment.create_shipment(session, group, carrier="supplier", external_id=f"{task.marketplace}:{tracking_number}",
                                     waybill=tracking_number)
    from app.models.fulfillment import ShipmentItem

    for si in session.scalars(select(ShipmentItem).where(ShipmentItem.shipment_id == sh.id)):
        si.unit_cost = session.get(OrderItem, si.order_item_id).unit_cost
    return sh


def supplier_canceled_purchase(session: Session, principal: Principal, purchase_id: uuid.UUID) -> None:
    purchase = session.get(ExternalPurchase, purchase_id, with_for_update=True)
    principal.require("procurement.operate", purchase.portfolio_id)
    order = session.get(Order, purchase.order_id)
    for line in session.scalars(select(PurchaseLine).where(PurchaseLine.external_purchase_id == purchase.id)):
        session.delete(line)  # trigger decrements qty_procured
    purchase.status = "supplier_canceled"
    posting.supplier_canceled(session, order, str(purchase.id), purchase.amount_paid, purchase.portfolio_id)
    task = session.get(PurchaseTask, purchase.purchase_task_id)
    task.status = "supplier_canceled"
    exceptions.open_case(session, order.organization_id, "supplier_canceled", subject_type="purchase_task",
                         subject_id=str(task.id), order_id=order.id, portfolio_id=task.portfolio_id,
                         context={"external_order_id": purchase.external_order_id})


def supplier_refund_received(session: Session, principal: Principal, purchase_id: uuid.UUID) -> None:
    purchase = session.get(ExternalPurchase, purchase_id)
    principal.require("procurement.operate", purchase.portfolio_id)
    posting.supplier_refunded(session, session.get(Order, purchase.order_id), str(purchase.id), purchase.amount_paid,
                              purchase.portfolio_id)
