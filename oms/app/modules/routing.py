"""Deterministic fulfillment router (rules_v1). Every decision is persisted with its inputs.

Rules, in order:
1. Native marketplace orders -> PLATFORM_MANAGED (the platform's fulfilment rules apply).
2. Payment readiness: prepaid must be verified; COD only for owned stock.
3. All lines covered by active owned-stock reservations -> OWNED_STOCK_BITESHIP.
4. All lines covered by an approved, recently verified supplier offer on one marketplace, with landed cost under the
   ceiling, expected contribution >= the portfolio floor and cash above the liquidity reserve -> *_DROPSHIP.
5. Anything else (mixed sources, stale offers, margin/cash breach) -> MANUAL_REVIEW with reasons.
"""

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.catalog import SupplierOffer
from app.models.finance import LedgerEntry
from app.models.fulfillment import RoutingDecision
from app.models.inventory import InventoryReservation
from app.models.orders import Order, OrderItem
from app.models.org import Portfolio, RiskPolicy
from app.modules import exceptions
from app.modules.finance import accounts as A

MARKETPLACES = {"shopee", "tokopedia", "tiktokshop", "lazada"}
OFFER_FRESHNESS_HOURS = 24


def org_cash(session: Session) -> Decimal:
    return Decimal(session.scalar(select(func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0)).where(
        LedgerEntry.account_code == A.CASH)))


def _reserve_floor(session: Session) -> Decimal:
    pol = session.scalars(select(RiskPolicy).where(RiskPolicy.key == "min_cash_reserve",
                                                   RiskPolicy.portfolio_id.is_(None))).first()
    return pol.threshold if pol else Decimal("0")


def route_order(session: Session, order: Order) -> RoutingDecision:
    items = session.scalars(select(OrderItem).where(OrderItem.order_id == order.id)).all()
    reasons: list[str] = []
    inputs: dict = {"payment_method": order.payment_method, "status": order.status, "lines": []}
    route = None

    if order.sales_channel in MARKETPLACES:
        route = "PLATFORM_MANAGED"
        reasons.append("native marketplace order")
    elif order.status not in ("payment_verified", "cod_confirmed"):
        route = "MANUAL_REVIEW"
        reasons.append(f"payment not ready ({order.status})")
    else:
        reserved = dict(session.execute(select(InventoryReservation.portfolio_id, func.sum(InventoryReservation.qty))
                                        .where(InventoryReservation.order_id == order.id,
                                               InventoryReservation.status == "active")
                                        .group_by(InventoryReservation.portfolio_id)).all())
        needed: dict[uuid.UUID, int] = {}
        for it in items:
            needed[it.portfolio_id] = needed.get(it.portfolio_id, 0) + it.qty
        inputs["reserved"] = {str(k): int(v) for k, v in reserved.items()}
        if all(reserved.get(pid, 0) >= q for pid, q in needed.items()):
            route = "OWNED_STOCK_BITESHIP"
            reasons.append("all lines covered by owned-stock reservations")
        elif any(reserved.get(pid, 0) for pid in needed):
            route = "MANUAL_REVIEW"
            reasons.append("mixed owned and non-owned lines")
        elif order.payment_method == "cod":
            route = "MANUAL_REVIEW"
            reasons.append("COD is not allowed for dropship")
        else:
            route, more = _dropship_route(session, order, items, inputs)
            reasons += more

    decision = RoutingDecision(organization_id=order.organization_id, order_id=order.id, route=route,
                               inputs=_json(inputs), reasons=reasons)
    session.add(decision)
    order.fulfillment_route = route
    if route == "MANUAL_REVIEW":
        exceptions.open_case(session, order.organization_id, "manual_review", subject_type="order",
                             subject_id=str(order.id), order_id=order.id,
                             portfolio_id=items[0].portfolio_id if items else None, context={"reasons": reasons})
    return decision


def _dropship_route(session: Session, order: Order, items, inputs) -> tuple[str, list[str]]:
    from datetime import timedelta

    reasons, marketplaces, total_cost = [], set(), Decimal("0")
    for it in items:
        offer = session.scalars(select(SupplierOffer).where(
            SupplierOffer.sku_id == it.sku_id, SupplierOffer.portfolio_id == it.portfolio_id,
            SupplierOffer.approved.is_(True), SupplierOffer.last_observed_in_stock.is_(True),
            SupplierOffer.last_observed_at >= utcnow() - timedelta(hours=OFFER_FRESHNESS_HOURS))
            .order_by(SupplierOffer.priority, SupplierOffer.last_observed_price)).first()
        if offer is None:
            return "MANUAL_REVIEW", [f"no fresh approved supplier offer for sku {it.sku_id}"]
        landed = offer.last_observed_price * it.qty + offer.reference_shipping
        if offer.last_observed_price > offer.price_ceiling:
            return "MANUAL_REVIEW", [f"supplier price above ceiling for sku {it.sku_id}"]
        portfolio = session.get(Portfolio, it.portfolio_id)
        floor = portfolio.target_contribution_margin or Decimal("0")
        margin = (it.line_total - landed) / it.line_total if it.line_total else Decimal("-1")
        inputs["lines"].append({"sku": str(it.sku_id), "landed_cost": str(landed), "margin": str(round(margin, 4)),
                                "floor": str(floor), "marketplace": offer.marketplace})
        if margin < floor:
            return "MANUAL_REVIEW", [f"expected margin {margin:.2%} below floor {floor:.2%}"]
        marketplaces.add(offer.marketplace)
        total_cost += landed
    if len(marketplaces) != 1:
        return "MANUAL_REVIEW", ["lines need different marketplaces"]
    cash, floor_cash = org_cash(session), _reserve_floor(session)
    inputs["cash"], inputs["reserve"] = str(cash), str(floor_cash)
    if cash - total_cost < floor_cash:
        return "MANUAL_REVIEW", [f"purchase would breach liquidity reserve (cash {cash}, cost {total_cost})"]
    mp = marketplaces.pop()
    reasons.append(f"approved {mp} offers within ceiling and margin floor")
    return f"{mp.upper()}_DROPSHIP", reasons


def _json(v):
    if isinstance(v, dict):
        return {k: _json(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_json(x) for x in v]
    if isinstance(v, Decimal | uuid.UUID):
        return str(v)
    return v
