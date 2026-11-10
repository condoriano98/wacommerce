"""Owned inventory: receipts, available-to-promise, holds/reservations, shipment consumption, transfers.

Overselling is impossible at the database level (CHECK reserved <= on_hand). Service code additionally
locks balance rows (SELECT ... FOR UPDATE) so concurrent channels serialise on the same stock.
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.inventory import InventoryBalance, InventoryMovement, InventoryReservation
from app.modules.finance import accounts as A
from app.modules.finance import posting
from app.modules.finance.ledger import JournalLine, post_journal


class InsufficientStock(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status.HTTP_409_CONFLICT, detail)


def _balance(session: Session, org_id, portfolio_id, sku_id, warehouse_id, lock=True) -> InventoryBalance:
    stmt = select(InventoryBalance).where(InventoryBalance.portfolio_id == portfolio_id,
                                          InventoryBalance.sku_id == sku_id,
                                          InventoryBalance.warehouse_id == warehouse_id)
    bal = session.scalars(stmt.with_for_update() if lock else stmt).first()
    if bal is None:
        bal = InventoryBalance(organization_id=org_id, portfolio_id=portfolio_id, sku_id=sku_id,
                               warehouse_id=warehouse_id, on_hand=0, reserved=0, avg_unit_cost=Decimal("0"))
        session.add(bal)
        session.flush()
    return bal


def receive(session: Session, org_id: uuid.UUID, *, portfolio_id, sku_id, warehouse_id, qty: int,
            unit_cost: Decimal, reference: str, paid: bool = True, legal_entity_id=None) -> InventoryBalance:
    if qty <= 0:
        raise ValueError("qty must be positive")
    bal = _balance(session, org_id, portfolio_id, sku_id, warehouse_id)
    total_cost = bal.avg_unit_cost * bal.on_hand + unit_cost * qty
    bal.on_hand += qty
    bal.avg_unit_cost = (total_cost / bal.on_hand).quantize(Decimal("0.0001"))
    session.add(InventoryMovement(organization_id=org_id, portfolio_id=portfolio_id, balance_id=bal.id,
                                  movement_type="receive", qty=qty, unit_cost=unit_cost,
                                  reference_type="receipt", reference_id=reference))
    if legal_entity_id is None:
        from app.models.inventory import Warehouse

        wh = session.get(Warehouse, warehouse_id)
        legal_entity_id = getattr(wh, "legal_entity_id", None)
    bal.legal_entity_id = bal.legal_entity_id or legal_entity_id
    posting.inventory_purchase(session, org_id, amount=(unit_cost * qty).quantize(Decimal("0.01")),
                               portfolio_id=portfolio_id, source_id=reference, paid=paid,
                               legal_entity_id=legal_entity_id)
    return bal


def available_to_promise(session: Session, portfolio_id: uuid.UUID, sku_id: uuid.UUID, safety_stock: int = 0) -> int:
    expire_holds(session)
    row = session.execute(
        select(func.coalesce(func.sum(InventoryBalance.on_hand - InventoryBalance.reserved), 0)).where(
            InventoryBalance.portfolio_id == portfolio_id, InventoryBalance.sku_id == sku_id)
    ).scalar()
    return max(0, int(row) - safety_stock)


def reserve(session: Session, org_id: uuid.UUID, *, portfolio_id, sku_id, qty: int, kind: str, quote_id=None,
            order_id=None, ttl: timedelta | None = None, safety_stock: int = 0) -> list[InventoryReservation]:
    """Reserve across warehouses (largest free stock first). All-or-nothing."""
    expire_holds(session)
    balances = session.scalars(
        select(InventoryBalance).where(InventoryBalance.portfolio_id == portfolio_id, InventoryBalance.sku_id == sku_id)
        .order_by((InventoryBalance.on_hand - InventoryBalance.reserved).desc()).with_for_update()
    ).all()
    free = sum(b.on_hand - b.reserved for b in balances) - safety_stock
    if free < qty:
        raise InsufficientStock(f"available {max(free, 0)} < requested {qty}")
    remaining, out = qty, []
    for bal in balances:
        take = min(remaining, bal.on_hand - bal.reserved)
        if take <= 0:
            continue
        bal.reserved += take
        r = InventoryReservation(organization_id=org_id, portfolio_id=portfolio_id, balance_id=bal.id, kind=kind,
                                 quote_id=quote_id, order_id=order_id, qty=take,
                                 expires_at=(utcnow() + ttl) if ttl else None)
        session.add(r)
        out.append(r)
        remaining -= take
        if remaining == 0:
            break
    session.flush()  # the CHECK constraint is evaluated here
    return out


def release(session: Session, reservations: list[InventoryReservation], status_: str = "released") -> None:
    for r in reservations:
        if r.status != "active":
            continue
        bal = session.get(InventoryBalance, r.balance_id, with_for_update=True)
        bal.reserved -= r.qty
        r.status = status_


def expire_holds(session: Session, now: datetime | None = None) -> int:
    expired = session.scalars(select(InventoryReservation).where(
        InventoryReservation.status == "active", InventoryReservation.expires_at.is_not(None),
        InventoryReservation.expires_at < (now or utcnow()))).all()
    release(session, list(expired), "expired")
    return len(expired)


def convert_quote_holds(session: Session, quote_id: uuid.UUID, order_id: uuid.UUID) -> list[InventoryReservation]:
    holds = session.scalars(select(InventoryReservation).where(
        InventoryReservation.quote_id == quote_id, InventoryReservation.status == "active")).all()
    for h in holds:
        h.kind, h.order_id, h.expires_at = "order", order_id, None
    return list(holds)


def pick_from_reservations(session: Session, org_id: uuid.UUID, order_id: uuid.UUID, portfolio_id: uuid.UUID,
                           sku_id: uuid.UUID, qty: int, reference: str) -> Decimal:
    """Carrier pickup: consume order reservations, on_hand -> in_transit. Returns moving-average unit cost."""
    reservations = session.scalars(select(InventoryReservation).join(
        InventoryBalance, InventoryBalance.id == InventoryReservation.balance_id).where(
        InventoryReservation.order_id == order_id, InventoryReservation.status == "active",
        InventoryReservation.portfolio_id == portfolio_id, InventoryBalance.sku_id == sku_id)
        .with_for_update()).all()
    remaining, cost = qty, Decimal("0")
    for r in reservations:
        if remaining == 0:
            break
        take = min(remaining, r.qty)
        bal = session.get(InventoryBalance, r.balance_id, with_for_update=True)
        bal.reserved -= take
        bal.on_hand -= take
        bal.in_transit += take
        cost = bal.avg_unit_cost
        r.qty -= take
        if r.qty == 0:
            r.qty, r.status = take, "consumed"
        session.add(InventoryMovement(organization_id=org_id, portfolio_id=portfolio_id, balance_id=bal.id,
                                      movement_type="ship", qty=-take, unit_cost=bal.avg_unit_cost,
                                      reference_type="shipment", reference_id=reference))
        remaining -= take
    if remaining:
        raise InsufficientStock(f"no reserved stock left for {remaining} units")
    session.flush()
    return cost


def _transit_balance(session: Session, portfolio_id, sku_id) -> InventoryBalance:
    bal = session.scalars(select(InventoryBalance).where(
        InventoryBalance.portfolio_id == portfolio_id, InventoryBalance.sku_id == sku_id,
        InventoryBalance.in_transit > 0).with_for_update()).first()
    if bal is None:
        raise InsufficientStock("no goods in transit for this SKU")
    return bal


def deliver_in_transit(session: Session, portfolio_id, sku_id, qty: int) -> None:
    bal = _transit_balance(session, portfolio_id, sku_id)
    bal.in_transit -= qty


def return_in_transit(session: Session, org_id, portfolio_id, sku_id, qty: int, reference: str) -> Decimal:
    """Failed delivery: goods come back to the warehouse (in_transit -> on_hand)."""
    bal = _transit_balance(session, portfolio_id, sku_id)
    bal.in_transit -= qty
    bal.on_hand += qty
    session.add(InventoryMovement(organization_id=org_id, portfolio_id=portfolio_id, balance_id=bal.id,
                                  movement_type="return", qty=qty, unit_cost=bal.avg_unit_cost,
                                  reference_type="rts", reference_id=reference))
    return bal.avg_unit_cost


def restock_return(session: Session, org_id: uuid.UUID, *, portfolio_id, sku_id, warehouse_id, qty: int,
                   unit_cost: Decimal, order_id: uuid.UUID, resellable: bool) -> Decimal:
    """Returned goods (after delivery): resellable stock goes back on hand; the journal is posted by
    ``posting.return_received``."""
    bal = _balance(session, org_id, portfolio_id, sku_id, warehouse_id)
    amount = (unit_cost * qty).quantize(Decimal("0.01"))
    if resellable:
        total_cost = bal.avg_unit_cost * bal.on_hand + unit_cost * qty
        bal.on_hand += qty
        bal.avg_unit_cost = (total_cost / bal.on_hand).quantize(Decimal("0.0001"))
        session.add(InventoryMovement(organization_id=org_id, portfolio_id=portfolio_id, balance_id=bal.id,
                                      movement_type="return", qty=qty, unit_cost=unit_cost,
                                      reference_type="order", reference_id=str(order_id)))
    return amount


def transfer_between_portfolios(session: Session, org_id: uuid.UUID, *, from_portfolio, to_portfolio, sku_id,
                                warehouse_id, qty: int, reference: str) -> None:
    """Inventory changes owner at cost; paired journals net to zero on consolidation."""
    src = _balance(session, org_id, from_portfolio, sku_id, warehouse_id)
    if src.on_hand - src.reserved < qty:
        raise InsufficientStock("not enough free stock to transfer")
    cost = src.avg_unit_cost
    src.on_hand -= qty
    dst = _balance(session, org_id, to_portfolio, sku_id, warehouse_id)
    total = dst.avg_unit_cost * dst.on_hand + cost * qty
    dst.on_hand += qty
    dst.avg_unit_cost = (total / dst.on_hand).quantize(Decimal("0.0001"))
    for bal, mtype, q in ((src, "transfer_out", -qty), (dst, "transfer_in", qty)):
        session.add(InventoryMovement(organization_id=org_id, portfolio_id=bal.portfolio_id, balance_id=bal.id,
                                      movement_type=mtype, qty=q, unit_cost=cost, reference_type="transfer",
                                      reference_id=reference))
    amount = (cost * qty).quantize(Decimal("0.01"))
    # Same legal entity: inventory changes portfolio dimension at cost; the pair nets to zero on consolidation.
    jid = post_journal(session, org_id, source_type="inventory_transfer", source_id=reference,
                       legal_entity_id=src.legal_entity_id, lines=[
        JournalLine(A.INVENTORY, debit=amount, portfolio_id=to_portfolio),
        JournalLine(A.INVENTORY, credit=amount, portfolio_id=from_portfolio)])
    if jid is None:  # the stock moves above roll back with the transaction
        raise ValueError(f"inventory transfer {reference} was already posted")
