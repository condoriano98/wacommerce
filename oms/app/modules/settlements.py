"""Settlements and refunds (spec §9, §11, §16).

* Gateway disbursement (DOKU payout) clears 1010 to bank and books any fee not yet known.
* Biteship COD remittance: the COD fund report is dashboard/CSV-only (G-BS1, verified 2026-11-10 as no documented
  API) → imported rows clear 1100 into the carrier balance; withdrawals move it to bank.
* Marketplace payout: clears 1110, trues up fees, books withheld tax (PPh 22) as prepaid tax.
* Refunds: approval-backed obligations; payout is a human bank action recorded with evidence.
* Returns after delivery: quantities, restock or loss, return shipping.
"""

import uuid
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.authz import Principal
from app.models.base import utcnow
from app.models.finance import LedgerEntry, Settlement, SettlementLine
from app.models.fulfillment import FulfillmentGroup, Shipment
from app.models.orders import Order, OrderItem, Payment, Refund
from app.modules import approvals, events, exceptions, inventory, orders, reconciliation
from app.modules.finance import accounts as A
from app.modules.finance import posting


def _settlement(session: Session, org_id, account_id, kind: str, external_id: str, gross, fees, net,
                source: str = "api") -> tuple[Settlement, bool]:
    s = session.scalars(select(Settlement).where(Settlement.platform_account_id == account_id,
                                                 Settlement.external_id == external_id)).first()
    if s:
        return s, False
    s = Settlement(organization_id=org_id, platform_account_id=account_id, kind=kind, external_id=external_id,
                   gross_amount=gross, fees_amount=fees, net_amount=net, paid_at=utcnow(), data_source=source)
    session.add(s)
    session.flush()
    return s, True


def gateway_disbursement(session: Session, principal: Principal, account_id, external_id: str,
                         payments: list[tuple[uuid.UUID, Decimal]]) -> Settlement:
    """payments: [(payment_id, net amount received for it)]."""
    principal.require("finance.manage")
    gross = Decimal("0")
    fees_unbooked = Decimal("0")
    for pid, net in payments:
        p = session.get(Payment, pid)
        gross += p.amount
        booked_fee = Decimal(session.scalar(select(func.coalesce(func.sum(LedgerEntry.debit), 0)).where(
            LedgerEntry.source_type == "gateway_fee", LedgerEntry.source_id == str(p.id),
            LedgerEntry.account_code == A.PAYMENT_FEES)))
        fees_unbooked += p.amount - booked_fee - net
    s, new = _settlement(session, principal.org_id, account_id, "gateway_disbursement", external_id, gross,
                         fees_unbooked, gross - fees_unbooked)
    if not new:
        return s
    first = session.get(Order, session.get(Payment, payments[0][0]).order_id)
    total_net = sum((n for _, n in payments), Decimal("0"))
    # 1010 holds amount − fees already booked; it is cleared by net received + fees not yet booked (F1, F9).
    posting.gateway_disbursement(session, principal.org_id, posting.entity_for_order(session, first), str(s.id),
                                 total_net + fees_unbooked, fees_unbooked, platform_account_id=account_id)
    for pid, net in payments:
        p = session.get(Payment, pid)
        if p.status == "captured":
            p.status = "settled"
        reconciliation.match(session, "gateway", str(pid), actual_ref=str(s.id), actual_amount=net,
                             evidence={"settlement": external_id})
    return s


def cod_remittance(session: Session, principal: Principal, carrier_account_id, report_id: str,
                   rows: list[dict]) -> Settlement:
    """rows from the Biteship COD fund report: {shipment_external_id, amount_remitted}. CSV import (G-BS1)."""
    principal.require("finance.manage")
    total = sum((Decimal(str(r["amount_remitted"])) for r in rows), Decimal("0"))
    s, new = _settlement(session, principal.org_id, carrier_account_id, "cod_remittance", report_id, total,
                         Decimal("0"), total, source="csv")
    if not new:
        return s
    by_portfolio: dict = {}
    le = None
    for r in rows:
        sh = session.scalars(select(Shipment).where(Shipment.external_id == r["shipment_external_id"])).first()
        if sh is None:
            continue
        amt = Decimal(str(r["amount_remitted"]))
        by_portfolio[sh.portfolio_id] = by_portfolio.get(sh.portfolio_id, Decimal("0")) + amt
        le = le or posting.entity_for_order(session, session.get(Order, sh.order_id))
        session.add(SettlementLine(organization_id=principal.org_id, settlement_id=s.id,
                                   platform_account_id=carrier_account_id, order_id=sh.order_id, shipment_id=sh.id,
                                   line_type="cod_remittance", amount=amt))
        reconciliation.match(session, "cod", str(sh.id), actual_ref=str(s.id), actual_amount=amt,
                             evidence={"report": report_id})
    posting.cod_remitted(session, principal.org_id, le, str(s.id), by_portfolio, carrier_account_id)
    return s


def carrier_withdrawal(session: Session, principal: Principal, carrier_account_id, withdrawal_id: str,
                       gross: Decimal, fee: Decimal, legal_entity_id) -> None:
    principal.require("finance.manage")
    posting.carrier_withdrawal(session, principal.org_id, legal_entity_id, withdrawal_id, gross, fee,
                               carrier_account_id)


def marketplace_payout(session: Session, principal: Principal, account_id, payout_id: str,
                       lines: list[dict]) -> Settlement:
    """lines (normalized by the marketplace adapter or CSV import): {external_order_id, gross, fee, tax_withheld,
    net}. Fee true-up = actual fee - accrued expected fee."""
    principal.require("finance.manage")
    gross = sum((Decimal(str(ln["gross"])) for ln in lines), Decimal("0"))
    fees = sum((Decimal(str(ln["fee"])) for ln in lines), Decimal("0"))
    net = sum((Decimal(str(ln["net"])) for ln in lines), Decimal("0"))
    s, new = _settlement(session, principal.org_id, account_id, "marketplace_payout", payout_id, gross, fees, net)
    if not new:
        return s
    for ln in lines:
        order = session.scalars(select(Order).where(Order.platform_account_id == account_id,
                                                    Order.external_order_id == ln["external_order_id"])).first()
        if order is None:
            exceptions.open_case(session, principal.org_id, "marketplace_settlement_mismatch",
                                 subject_type="settlement", subject_id=f"{s.id}:{ln['external_order_id']}",
                                 context={"reason": "settlement line for unknown order"})
            continue
        item = orders.items_of(session, order.id)[0]
        accrued = Decimal(session.scalar(select(func.coalesce(func.sum(LedgerEntry.debit), 0)).where(
            LedgerEntry.order_id == order.id, LedgerEntry.source_type == "marketplace_fee_accrual")))
        receivable = Decimal(str(ln["gross"])) - accrued
        posting.marketplace_settled(
            session, principal.org_id, posting.entity_for_order(session, order), f"{s.id}:{order.id}",
            receivable_cleared=receivable, net_paid=Decimal(str(ln["net"])),
            fee_true_up=Decimal(str(ln["fee"])) - accrued, withheld_tax=Decimal(str(ln.get("tax_withheld", 0))),
            portfolio_id=item.portfolio_id, platform_account_id=account_id)
        session.add(SettlementLine(organization_id=principal.org_id, settlement_id=s.id, platform_account_id=account_id,
                                   order_id=order.id, external_order_id=ln["external_order_id"], line_type="sale",
                                   amount=Decimal(str(ln["net"]))))
        reconciliation.match(session, "marketplace", str(order.id), actual_ref=str(s.id),
                             actual_amount=Decimal(str(ln["net"])), evidence={"payout": payout_id})
        order.settlement_status = "settled"
        orders.derive_status(session, order)
    return s


@events.subscribe("shipment_delivered")
def _expect_settlement(session: Session, data: dict) -> None:
    order, sh = data["order"], data["shipment"]
    g = session.get(FulfillmentGroup, sh.group_id)
    if order.payment_method == "cod":
        # Only this shipment's own deductions (keyed by shipment id), never other shipments of the same order.
        deducted = Decimal(session.scalar(select(func.coalesce(func.sum(LedgerEntry.credit), 0)).where(
            LedgerEntry.order_id == order.id, LedgerEntry.account_code == A.COD_RECEIVABLE,
            LedgerEntry.source_type.in_(("cod_fee", "carrier_cost")), LedgerEntry.source_id == str(sh.id))))
        reconciliation.expect(session, order.organization_id, "cod", str(sh.id), sh.cod_amount - deducted,
                              portfolio_id=g.portfolio_id)
    elif order.payment_method == "marketplace":
        expected = Decimal(session.scalar(select(func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0))
                                          .where(LedgerEntry.order_id == order.id,
                                                 LedgerEntry.account_code == A.MARKETPLACE_RECEIVABLE)))
        reconciliation.expect(session, order.organization_id, "marketplace", str(order.id), expected,
                              portfolio_id=g.portfolio_id)


# --- refunds and returns ----------------------------------------------------------------------------------------


def request_refund(session: Session, principal: Principal, order_id: uuid.UUID, item_id: uuid.UUID,
                   amount: Decimal, reason: str) -> Refund:
    order = session.get(Order, order_id)
    item = session.get(OrderItem, item_id)
    if principal.is_service:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "the AI cannot request refunds")
    if not (principal.can("refund.request", item.portfolio_id) or principal.can("refund.approve", item.portfolio_id)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "refund authority required")
    refund = Refund(organization_id=order.organization_id, order_id=order.id, amount=amount, reason=reason,
                    requested_by=principal.user_id, method=str(item.id))
    session.add(refund)
    session.flush()
    refund.approval_id = approvals.request_approval(
        session, principal, kind="refund", subject_type="refund", subject_id=str(refund.id), amount=amount,
        portfolio_id=item.portfolio_id, idempotency_key=f"refund:{refund.id}",
        payload={"order": order.order_number, "item": str(item.id), "reason": reason}).id
    return refund


def approve_refund_obligation(session: Session, principal: Principal, refund_id: uuid.UUID) -> Refund:
    """After approval: post the obligation (revenue reduction if delivered, advance release otherwise)."""
    refund = session.get(Refund, refund_id, with_for_update=True)
    approvals.require_approved(session, refund.approval_id, "refund", str(refund.id))
    order = session.get(Order, refund.order_id)
    item = session.get(OrderItem, uuid.UUID(refund.method))
    if item.qty_delivered > 0:
        posting.refund_after_delivery(session, order, item, refund.amount, str(refund.id))
    refund.status = "approved"
    return refund


def mark_refund_paid(session: Session, principal: Principal, refund_id: uuid.UUID, bank_reference: str) -> Refund:
    """A human paid the refund from the bank (OMS never moves money); recorded with evidence."""
    principal.require("finance.manage")
    refund = session.get(Refund, refund_id, with_for_update=True)
    if refund.status != "approved":
        raise HTTPException(status.HTTP_409_CONFLICT, f"refund is {refund.status}")
    order = session.get(Order, refund.order_id)
    item = session.get(OrderItem, uuid.UUID(refund.method))
    posting.refund_paid(session, order, item, refund.amount, str(refund.id))
    refund.status, refund.paid_at = "paid", utcnow()
    item.qty_refunded = min(item.qty, item.qty_refunded + max(1, int(refund.amount // max(item.unit_price, 1))))
    audit(session, principal.org_id, "refund.paid", "refund", refund.id, actor_user_id=principal.user_id,
          portfolio_id=item.portfolio_id, after={"amount": refund.amount, "bank_reference": bank_reference})
    orders.derive_status(session, order)
    return refund


def receive_return(session: Session, principal: Principal, order_id: uuid.UUID, item_id: uuid.UUID, qty: int, *,
                   resellable: bool, warehouse_id, return_shipping_cost: Decimal) -> None:
    order = session.get(Order, order_id)
    item = session.get(OrderItem, item_id, with_for_update=True)
    principal.require("fulfillment.operate", item.portfolio_id)
    if qty <= 0 or item.qty_returned + qty > item.qty_delivered:
        raise HTTPException(status.HTTP_409_CONFLICT, "cannot return more than delivered")
    item.qty_returned += qty
    # The exact cost of what was delivered for this line (per-warehouse allocations), not the last pickup's cost.
    unit_cost = inventory.delivered_unit_cost(session, item.id) or item.unit_cost or Decimal("0")
    if unit_cost:
        inventory.restock_return(session, order.organization_id, portfolio_id=item.portfolio_id, sku_id=item.sku_id,
                                 warehouse_id=warehouse_id, qty=qty, unit_cost=unit_cost, order_id=order.id,
                                 resellable=resellable)
        posting.return_received(session, order, item, qty, unit_cost, resellable=resellable,
                                source_id=f"{item.id}:{item.qty_returned}")
    if return_shipping_cost:
        posting.return_shipping(session, order, item, return_shipping_cost, f"{item.id}:{item.qty_returned}")
    orders.derive_status(session, order)
    if order.status == "returned":
        events.emit(session, "order_returned", order=order)


def credit_loss(session: Session, principal: Principal, payment_id: uuid.UUID, kind: str,
                approval_id: uuid.UUID | None = None) -> None:
    """F13: allowance / release / write_off (approval) / recovery for release-on-attestation receivables."""
    principal.require("finance.manage")
    p = session.get(Payment, payment_id)
    order = session.get(Order, p.order_id)
    items = orders.items_of(session, order.id)
    if kind == "write_off":
        approvals.require_approved(session, approval_id, "ledger_adjustment", str(payment_id))
    shares = posting.split(p.amount, [i.line_total for i in items])
    for item, share in zip(items, shares, strict=True):
        posting.credit_loss(session, order, item, share, kind, f"{payment_id}:{item.id}")
