"""Posting rules v2: business events -> balanced journals (spec §9, §27.3; fixtures F1–F13).

Policies
* Order confirmation, receipts and attestations create **no** journal.
* Prepaid cash captured (gateway) or bank-matched (manual) before delivery is a contract liability (2200).
* Owned stock moves Inventory → Goods in transit at carrier pickup; COGS is recognised from 1250 at delivery.
* Revenue + shipping income are recognised per **delivered shipment line** (control transfer), never waiting on cash.
* COD: delivery debits the COD receivable; COD fee and carrier-deducted shipping are accrued against it.
* Marketplace: delivery debits the marketplace receivable; expected fees accrued, trued up at settlement.
* Dropship: supplier payment is a prepayment (1210) until the customer delivery.
* Release-on-attestation (approved policy): delivery before bank match debits 1105; allowances via 1109/5700.
Every rule is idempotent on (source_type, source_id).
"""

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.finance import LedgerEntry
from app.models.orders import Order, OrderItem, OrderItemAllocation, Payment
from app.models.org import Brand
from app.models.platforms import PlatformAccount
from app.modules.finance import accounts as A
from app.modules.finance.ledger import ZERO, JournalLine, journal_exists, post_journal

CENT = Decimal("0.01")


def q(v: Decimal) -> Decimal:
    return v.quantize(CENT, ROUND_HALF_UP)


def entity_for_order(session: Session, order: Order) -> uuid.UUID | None:
    acc = session.get(PlatformAccount, order.platform_account_id)
    if acc is not None and acc.legal_entity_id:
        return acc.legal_entity_id
    brand = session.get(Brand, order.brand_id)
    return brand.legal_entity_id if brand else None


def _post(session: Session, org_id, legal_entity_id, source_type: str, source_id: str, lines: list[JournalLine],
          memo: str | None = None) -> uuid.UUID | None:
    if journal_exists(session, source_type, source_id):
        return None
    lines = [ln for ln in lines if ln.debit or ln.credit]
    if not lines:
        return None
    return post_journal(session, org_id, source_type=source_type, source_id=source_id, lines=lines, memo=memo,
                        legal_entity_id=legal_entity_id)


def split(amount: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Allocate an amount pro rata; rounding remainder goes to the last share so the split sums exactly."""
    total = sum(weights, ZERO)
    out, used = [], ZERO
    for i, w in enumerate(weights):
        share = amount - used if i == len(weights) - 1 else (q(amount * w / total) if total else ZERO)
        used += share
        out.append(share)
    return out


def _items(session: Session, order_id) -> list[OrderItem]:
    return list(session.scalars(select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.created_at)))


def _kw(item: OrderItem, order: Order) -> dict:
    return {"portfolio_id": item.portfolio_id, "order_id": order.id, "order_item_id": item.id,
            "platform_account_id": order.platform_account_id}


def allocate_order_amounts(session: Session, order: Order, policy_version: int = 1) -> None:
    """Persist line allocations of order-level amounts (shipping income) at confirmation."""
    items = [i for i in _items(session, order.id) if i.qty_canceled < i.qty]
    weights = [i.line_total * (i.qty - i.qty_canceled) / i.qty for i in items]
    for item, share in zip(items, split(order.shipping_fee, weights), strict=True):
        session.add(OrderItemAllocation(organization_id=order.organization_id, portfolio_id=item.portfolio_id,
                                        order_id=order.id, order_item_id=item.id, kind="shipping_income",
                                        amount=share, policy_version=policy_version))


def shipping_allocation(session: Session, item: OrderItem) -> Decimal:
    row = session.scalars(select(OrderItemAllocation).where(
        OrderItemAllocation.order_item_id == item.id, OrderItemAllocation.kind == "shipping_income")
        .order_by(OrderItemAllocation.policy_version.desc())).first()
    return row.amount if row else ZERO


def reallocate_shipping_after_cancel(session: Session, order: Order) -> None:
    """Shipping is charged for what ships: on a line cancellation the shipping income moves to remaining lines."""
    current = session.scalars(select(OrderItemAllocation.policy_version).where(
        OrderItemAllocation.order_id == order.id, OrderItemAllocation.kind == "shipping_income")
        .order_by(OrderItemAllocation.policy_version.desc())).first() or 0
    allocate_order_amounts(session, order, policy_version=current + 1)


def item_value(item: OrderItem, qty: int) -> Decimal:
    return q(item.line_total * qty / item.qty)


# --- payments -------------------------------------------------------------------------------------------------


def payment_captured(session: Session, order: Order, payment: Payment, amount: Decimal) -> None:
    """Gateway capture (re-checked) before delivery: Dr 1010 / Cr 2200."""
    le = entity_for_order(session, order)
    items = _items(session, order.id)
    lines = []
    for item, share in zip(items, split(amount, [i.line_total for i in items]), strict=True):
        lines += [JournalLine(A.GATEWAY_CLEARING, debit=share, **_kw(item, order)),
                  JournalLine(A.CUSTOMER_ADVANCES, credit=share, **_kw(item, order))]
    _post(session, order.organization_id, le, "payment_captured", str(payment.id), lines)


def payment_bank_matched(session: Session, order: Order, payment: Payment, amount: Decimal) -> None:
    """Manual transfer proven by a bank statement line. Clears 1105 if revenue was already recognised under an
    approved release-on-attestation policy, otherwise creates the contract liability."""
    le = entity_for_order(session, order)
    items = _items(session, order.id)
    open_1105 = {i.id: _balance(session, A.TRANSFER_RECEIVABLE, order_item_id=i.id) for i in items}
    lines = []
    for item, share in zip(items, split(amount, [i.line_total for i in items]), strict=True):
        credit = A.TRANSFER_RECEIVABLE if open_1105[item.id] > 0 else A.CUSTOMER_ADVANCES
        lines += [JournalLine(A.CASH, debit=share, **_kw(item, order)), JournalLine(credit, credit=share, **_kw(item, order))]
    _post(session, order.organization_id, le, "payment_bank_matched", str(payment.id), lines)


def gateway_fee(session: Session, order: Order, payment: Payment, fee: Decimal) -> None:
    le = entity_for_order(session, order)
    item = _items(session, order.id)[0]
    _post(session, order.organization_id, le, "gateway_fee", str(payment.id), [
        JournalLine(A.PAYMENT_FEES, debit=fee, **_kw(item, order)),
        JournalLine(A.GATEWAY_CLEARING, credit=fee, **_kw(item, order))])


def gateway_disbursement(session: Session, org_id, legal_entity_id, settlement_id: str, gross: Decimal,
                         unbooked_fee: Decimal, platform_account_id=None) -> None:
    """Gateway pays out: Dr bank net (+ fee not yet booked) / Cr 1010 gross."""
    kw = {"platform_account_id": platform_account_id}
    _post(session, org_id, legal_entity_id, "gateway_disbursement", settlement_id, [
        JournalLine(A.CASH, debit=gross - unbooked_fee, **kw),
        JournalLine(A.PAYMENT_FEES, debit=unbooked_fee, **kw),
        JournalLine(A.GATEWAY_CLEARING, credit=gross, **kw)])


# --- fulfilment -----------------------------------------------------------------------------------------------


@dataclass
class ShippedLine:
    item: OrderItem
    qty: int
    unit_cost: Decimal
    owned: bool


def picked_up(session: Session, order: Order, shipment_id: str, lines_: list[ShippedLine]) -> None:
    """Owned stock leaves the warehouse: Dr 1250 / Cr 1200 at cost."""
    lines = []
    for ln in lines_:
        if ln.owned and ln.unit_cost:
            amt = q(ln.unit_cost * ln.qty)
            lines += [JournalLine(A.GOODS_IN_TRANSIT, debit=amt, **_kw(ln.item, order)),
                      JournalLine(A.INVENTORY, credit=amt, **_kw(ln.item, order))]
    _post(session, order.organization_id, entity_for_order(session, order), "picked_up", shipment_id, lines)


def carrier_cost(session: Session, order: Order, shipment_id: str, amount: Decimal, *, cod_deducted: bool,
                 portfolio_id) -> None:
    credit = A.COD_RECEIVABLE if cod_deducted else A.CARRIER_PAYABLES
    kw = {"portfolio_id": portfolio_id, "order_id": order.id}
    _post(session, order.organization_id, entity_for_order(session, order), "carrier_cost", shipment_id, [
        JournalLine(A.SHIPPING_EXPENSE, debit=amount, **kw), JournalLine(credit, credit=amount, **kw)])


def delivered(session: Session, order: Order, shipment_id: str, lines_: list[ShippedLine], *,
              revenue_debit_account: str) -> None:
    """Control transfer for the delivered shipment lines: revenue + shipping income, then COGS (two journals)."""
    le = entity_for_order(session, order)
    rev, cogs = [], []
    for ln in lines_:
        item, kw = ln.item, _kw(ln.item, order)
        value = item_value(item, ln.qty)
        ship = q(shipping_allocation(session, item) * ln.qty / max(item.qty - item.qty_canceled, 1))
        rev += [JournalLine(revenue_debit_account, debit=value + ship, **kw), JournalLine(A.REVENUE, credit=value, **kw)]
        if ship:
            rev.append(JournalLine(A.SHIPPING_INCOME, credit=ship, **kw))
        cost = q(ln.unit_cost * ln.qty)
        if cost:
            source = A.GOODS_IN_TRANSIT if ln.owned else A.SUPPLIER_PREPAYMENTS
            cogs += [JournalLine(A.COGS, debit=cost, **kw), JournalLine(source, credit=cost, **kw)]
    _post(session, order.organization_id, le, "delivered_revenue", shipment_id, rev)
    _post(session, order.organization_id, le, "delivered_cogs", shipment_id, cogs)


def cod_fee(session: Session, order: Order, shipment_id: str, fee: Decimal, portfolio_id) -> None:
    kw = {"portfolio_id": portfolio_id, "order_id": order.id}
    _post(session, order.organization_id, entity_for_order(session, order), "cod_fee", shipment_id, [
        JournalLine(A.PAYMENT_FEES, debit=fee, **kw), JournalLine(A.COD_RECEIVABLE, credit=fee, **kw)])


def marketplace_expected_fee(session: Session, order: Order, item: OrderItem, fee: Decimal, source_id: str) -> None:
    _post(session, order.organization_id, entity_for_order(session, order), "marketplace_fee_accrual", source_id, [
        JournalLine(A.MARKETPLACE_FEES, debit=fee, **_kw(item, order)),
        JournalLine(A.MARKETPLACE_RECEIVABLE, credit=fee, **_kw(item, order))])


def returned_to_sender(session: Session, order: Order, shipment_id: str, lines_: list[ShippedLine],
                       outbound_cost: Decimal, return_cost: Decimal, portfolio_id) -> None:
    """Failed delivery: goods come back (1250 → 1200), no revenue; outbound + return shipping expensed."""
    le = entity_for_order(session, order)
    back = []
    for ln in lines_:
        if ln.owned and ln.unit_cost:
            amt = q(ln.unit_cost * ln.qty)
            back += [JournalLine(A.INVENTORY, debit=amt, **_kw(ln.item, order)),
                     JournalLine(A.GOODS_IN_TRANSIT, credit=amt, **_kw(ln.item, order))]
    _post(session, order.organization_id, le, "returned_to_warehouse", shipment_id, back)
    kw = {"portfolio_id": portfolio_id, "order_id": order.id}
    _post(session, order.organization_id, le, "failed_delivery_costs", shipment_id, [
        JournalLine(A.SHIPPING_EXPENSE, debit=outbound_cost, **kw),
        JournalLine(A.RETURN_LOSSES, debit=return_cost, **kw),
        JournalLine(A.CARRIER_PAYABLES, credit=outbound_cost + return_cost, **kw)])


# --- settlements ----------------------------------------------------------------------------------------------


def cod_remitted(session: Session, org_id, legal_entity_id, source_id: str, by_portfolio: dict, carrier_account_id,
                 to_bank: bool = False) -> None:
    """Carrier remits collected COD: Dr carrier balance (or bank) / Cr COD receivable."""
    debit = A.CASH if to_bank else A.CARRIER_BALANCE
    lines = []
    for pid, amount in by_portfolio.items():
        kw = {"portfolio_id": pid, "platform_account_id": carrier_account_id}
        lines += [JournalLine(debit, debit=amount, **kw), JournalLine(A.COD_RECEIVABLE, credit=amount, **kw)]
    _post(session, org_id, legal_entity_id, "cod_remitted", source_id, lines)


def carrier_withdrawal(session: Session, org_id, legal_entity_id, source_id: str, gross: Decimal, fee: Decimal,
                       carrier_account_id) -> None:
    kw = {"platform_account_id": carrier_account_id}
    _post(session, org_id, legal_entity_id, "carrier_withdrawal", source_id, [
        JournalLine(A.CASH, debit=gross - fee, **kw), JournalLine(A.PAYMENT_FEES, debit=fee, **kw),
        JournalLine(A.CARRIER_BALANCE, credit=gross, **kw)])


def marketplace_settled(session: Session, org_id, legal_entity_id, source_id: str, *, receivable_cleared: Decimal,
                        net_paid: Decimal, fee_true_up: Decimal, withheld_tax: Decimal, portfolio_id,
                        platform_account_id) -> None:
    kw = {"portfolio_id": portfolio_id, "platform_account_id": platform_account_id}
    lines = [JournalLine(A.CASH, debit=net_paid, **kw), JournalLine(A.PREPAID_TAX, debit=withheld_tax, **kw),
             JournalLine(A.MARKETPLACE_RECEIVABLE, credit=receivable_cleared, **kw)]
    if fee_true_up >= 0:
        lines.append(JournalLine(A.MARKETPLACE_FEES, debit=fee_true_up, **kw))
    else:
        lines.append(JournalLine(A.MARKETPLACE_FEES, credit=-fee_true_up, **kw))
    _post(session, org_id, legal_entity_id, "marketplace_settled", source_id, lines)


# --- procurement ----------------------------------------------------------------------------------------------


def supplier_paid(session: Session, order: Order, source_id: str, amount: Decimal, portfolio_id) -> None:
    kw = {"portfolio_id": portfolio_id, "order_id": order.id}
    _post(session, order.organization_id, entity_for_order(session, order), "supplier_paid", source_id, [
        JournalLine(A.SUPPLIER_PREPAYMENTS, debit=amount, **kw), JournalLine(A.CASH, credit=amount, **kw)])


def supplier_canceled(session: Session, order: Order, source_id: str, amount: Decimal, portfolio_id) -> None:
    kw = {"portfolio_id": portfolio_id, "order_id": order.id}
    _post(session, order.organization_id, entity_for_order(session, order), "supplier_canceled", source_id, [
        JournalLine(A.SUPPLIER_REFUND_RECEIVABLE, debit=amount, **kw),
        JournalLine(A.SUPPLIER_PREPAYMENTS, credit=amount, **kw)])


def supplier_refunded(session: Session, order: Order, source_id: str, amount: Decimal, portfolio_id) -> None:
    kw = {"portfolio_id": portfolio_id, "order_id": order.id}
    _post(session, order.organization_id, entity_for_order(session, order), "supplier_refunded", source_id, [
        JournalLine(A.CASH, debit=amount, **kw), JournalLine(A.SUPPLIER_REFUND_RECEIVABLE, credit=amount, **kw)])


# --- cancellations, refunds, returns --------------------------------------------------------------------------


def canceled_before_shipment(session: Session, order: Order, item: OrderItem, amount: Decimal, source_id: str) -> None:
    """Prepaid line canceled before shipment: the advance becomes a refund obligation."""
    _post(session, order.organization_id, entity_for_order(session, order), "canceled_prepaid", source_id, [
        JournalLine(A.CUSTOMER_ADVANCES, debit=amount, **_kw(item, order)),
        JournalLine(A.REFUNDS_PAYABLE, credit=amount, **_kw(item, order))])


def refund_after_delivery(session: Session, order: Order, item: OrderItem, amount: Decimal, source_id: str) -> None:
    _post(session, order.organization_id, entity_for_order(session, order), "refund_approved", source_id, [
        JournalLine(A.SALES_RETURNS, debit=amount, **_kw(item, order)),
        JournalLine(A.REFUNDS_PAYABLE, credit=amount, **_kw(item, order))])


def refund_paid(session: Session, order: Order, item: OrderItem, amount: Decimal, source_id: str) -> None:
    _post(session, order.organization_id, entity_for_order(session, order), "refund_paid", source_id, [
        JournalLine(A.REFUNDS_PAYABLE, debit=amount, **_kw(item, order)),
        JournalLine(A.CASH, credit=amount, **_kw(item, order))])


def return_received(session: Session, order: Order, item: OrderItem, qty: int, unit_cost: Decimal, *,
                    resellable: bool, source_id: str) -> None:
    amount = q(unit_cost * qty)
    debit = A.INVENTORY if resellable else A.RETURN_LOSSES
    _post(session, order.organization_id, entity_for_order(session, order), "return_received", source_id, [
        JournalLine(debit, debit=amount, **_kw(item, order)), JournalLine(A.COGS, credit=amount, **_kw(item, order))])


def return_shipping(session: Session, order: Order, item: OrderItem, amount: Decimal, source_id: str) -> None:
    _post(session, order.organization_id, entity_for_order(session, order), "return_shipping", source_id, [
        JournalLine(A.RETURN_LOSSES, debit=amount, **_kw(item, order)),
        JournalLine(A.CARRIER_PAYABLES, credit=amount, **_kw(item, order))])


# --- credit losses (F13) --------------------------------------------------------------------------------------


def credit_loss(session: Session, order: Order, item: OrderItem, amount: Decimal, kind: str, source_id: str) -> None:
    """kind: allowance (Dr5700/Cr1109) | release (Dr1109/Cr5700) | write_off (Dr1109/Cr1105) | recovery (Dr1000/Cr5700)."""
    debit, credit = {
        "allowance": (A.CREDIT_LOSSES, A.CREDIT_LOSS_ALLOWANCE),
        "release": (A.CREDIT_LOSS_ALLOWANCE, A.CREDIT_LOSSES),
        "write_off": (A.CREDIT_LOSS_ALLOWANCE, A.TRANSFER_RECEIVABLE),
        "recovery": (A.CASH, A.CREDIT_LOSSES),
    }[kind]
    _post(session, order.organization_id, entity_for_order(session, order), f"credit_loss_{kind}", source_id, [
        JournalLine(debit, debit=amount, **_kw(item, order)), JournalLine(credit, credit=amount, **_kw(item, order))])


# --- marketing, inventory -------------------------------------------------------------------------------------


def ad_spend_accrued(session: Session, org_id, legal_entity_id, source_id: str, amount: Decimal, portfolio_id,
                     ad_account_id) -> None:
    kw = {"portfolio_id": portfolio_id, "platform_account_id": ad_account_id}
    if amount >= 0:
        lines = [JournalLine(A.ADVERTISING, debit=amount, **kw), JournalLine(A.ADS_PAYABLES, credit=amount, **kw)]
    else:  # downward restatement in an open period
        lines = [JournalLine(A.ADS_PAYABLES, debit=-amount, **kw), JournalLine(A.ADVERTISING, credit=-amount, **kw)]
    _post(session, org_id, legal_entity_id, "ad_spend_accrued", source_id, lines)


def ad_charge_paid(session: Session, org_id, legal_entity_id, source_id: str, amount: Decimal, ad_account_id) -> None:
    kw = {"platform_account_id": ad_account_id}
    _post(session, org_id, legal_entity_id, "ad_charge_paid", source_id, [
        JournalLine(A.ADS_PAYABLES, debit=amount, **kw), JournalLine(A.CASH, credit=amount, **kw)])


def inventory_purchase(session: Session, org_id: uuid.UUID, *, amount: Decimal, portfolio_id: uuid.UUID,
                       source_id: str, paid: bool, legal_entity_id=None) -> None:
    kw = {"portfolio_id": portfolio_id}
    _post(session, org_id, legal_entity_id, "inventory_receipt", f"receipt:{source_id}", [
        JournalLine(A.INVENTORY, debit=amount, **kw),
        JournalLine(A.CASH if paid else A.SUPPLIER_PAYABLES, credit=amount, **kw)])


def expense(session: Session, org_id: uuid.UUID, *, account: str, amount: Decimal, source_type: str, source_id: str,
            portfolio_id, order_id=None, credit_account: str = A.SUPPLIER_PAYABLES, legal_entity_id=None,
            memo: str | None = None) -> None:
    if amount <= 0:
        return
    kw = {"portfolio_id": portfolio_id, "order_id": order_id}
    _post(session, org_id, legal_entity_id, source_type, source_id, [
        JournalLine(account, debit=amount, **kw), JournalLine(credit_account, credit=amount, **kw)], memo=memo)


def _balance(session: Session, account: str, **filters) -> Decimal:
    from sqlalchemy import func

    stmt = select(func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0)).where(
        LedgerEntry.account_code == account)
    for k, v in filters.items():
        stmt = stmt.where(getattr(LedgerEntry, k) == v)
    return Decimal(session.scalar(stmt))


def intercompany_transfer(session: Session, org_id, *, from_entity, to_entity, amount: Decimal, source_id: str) -> None:
    """Real cash moving between two legal entities' bank accounts. Each entity's journal balances on its own; the
    1900/2900 pair is eliminated on consolidation (spec §10)."""
    _post(session, org_id, from_entity, "intercompany_out", source_id, [
        JournalLine(A.INTERCOMPANY_RECEIVABLE, debit=amount), JournalLine(A.CASH, credit=amount)])
    _post(session, org_id, to_entity, "intercompany_in", source_id, [
        JournalLine(A.CASH, debit=amount), JournalLine(A.INTERCOMPANY_PAYABLE, credit=amount)])


def consolidated_balances(session: Session) -> dict[str, Decimal]:
    """Org-wide balances with intercompany eliminated (1900 vs 2900 must net to zero)."""
    from sqlalchemy import func

    rows = session.execute(select(LedgerEntry.account_code, func.sum(LedgerEntry.debit - LedgerEntry.credit))
                           .group_by(LedgerEntry.account_code)).all()
    out = {a: Decimal(v) for a, v in rows}
    ic = out.pop(A.INTERCOMPANY_RECEIVABLE, Decimal(0)) + out.pop(A.INTERCOMPANY_PAYABLE, Decimal(0))
    if ic != 0:
        raise ValueError(f"intercompany does not eliminate: {ic}")
    return out
