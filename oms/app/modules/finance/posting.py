"""Posting rules: business events -> balanced journals (docs/06-design-revisions.md R2).

Recognition policy
* Prepaid cash received before delivery is a customer advance (liability), not revenue.
* Revenue and COGS are recognised when the customer receives the goods (delivery).
* COD: on delivery the courier owes us the cash (COD receivable) until the remittance is matched.
* Marketplace: on delivery the platform owes us (marketplace receivable) until the payout is matched.
* Dropship purchases are supplier prepayments until the customer order is delivered.
Every rule is idempotent on (source_type, source_id).
"""

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orders import Order, OrderItem
from app.modules.finance import accounts as A
from app.modules.finance.ledger import ZERO, JournalLine, journal_exists, post_journal


def _items(session: Session, order_id: uuid.UUID) -> list[OrderItem]:
    return list(session.scalars(select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.created_at)))


def _split_by_items(items: list[OrderItem], amount: Decimal) -> list[tuple[OrderItem, Decimal]]:
    """Allocate an order-level amount (e.g. shipping income) to lines pro rata to line totals; rounding
    remainder goes to the last line so the split always sums exactly."""
    total = sum((i.line_total for i in items), ZERO)
    out, allocated = [], ZERO
    for idx, item in enumerate(items):
        share = amount - allocated if idx == len(items) - 1 else (
            (amount * item.line_total / total).quantize(Decimal("0.01")) if total else ZERO)
        allocated += share
        out.append((item, share))
    return out


def payment_received(session: Session, order: Order, amount: Decimal, *, via_gateway: bool, source_id: str) -> None:
    """Prepaid cash in before delivery: Dr gateway clearing / cash, Cr customer advances."""
    if journal_exists(session, "payment_received", source_id):
        return
    items = _items(session, order.id)
    debit_account = A.GATEWAY_CLEARING if via_gateway else A.CASH
    lines = []
    for item, share in _split_by_items(items, amount):
        lines += [JournalLine(debit_account, debit=share, portfolio_id=item.portfolio_id, order_id=order.id,
                              order_item_id=item.id),
                  JournalLine(A.CUSTOMER_ADVANCES, credit=share, portfolio_id=item.portfolio_id, order_id=order.id,
                              order_item_id=item.id)]
    post_journal(session, order.organization_id, source_type="payment_received", source_id=source_id, lines=lines,
                 memo=f"payment {order.order_number}")


def revenue_on_delivery(session: Session, order: Order, unit_costs: dict[uuid.UUID, tuple[Decimal, str]]) -> None:
    """Recognise revenue (+ shipping income) and COGS per order line.

    unit_costs: order_item_id -> (unit cost, credit account: INVENTORY for owned stock or SUPPLIER_PREPAYMENTS for
    dropship)."""
    source_id = str(order.id)
    if journal_exists(session, "order_delivered", source_id):
        return
    items = _items(session, order.id)
    if order.payment_method == "cod":
        receivable = A.COD_RECEIVABLE
    elif order.payment_method == "marketplace":
        receivable = A.MARKETPLACE_RECEIVABLE
    else:
        receivable = A.CUSTOMER_ADVANCES  # prepaid: release the advance
    lines: list[JournalLine] = []
    shipping = dict((i.id, s) for i, s in _split_by_items(items, order.shipping_fee))
    for item in items:
        net = item.line_total
        ship = shipping[item.id]
        kw = {"portfolio_id": item.portfolio_id, "order_id": order.id, "order_item_id": item.id,
              "platform_account_id": order.platform_account_id}
        lines += [JournalLine(receivable, debit=net + ship, **kw), JournalLine(A.REVENUE, credit=net, **kw)]
        if ship:
            lines.append(JournalLine(A.SHIPPING_INCOME, credit=ship, **kw))
        cost, credit_account = unit_costs.get(item.id, (ZERO, A.INVENTORY))
        cogs = (cost * item.qty).quantize(Decimal("0.01"))
        if cogs:
            lines += [JournalLine(A.COGS, debit=cogs, **kw), JournalLine(credit_account, credit=cogs, **kw)]
    post_journal(session, order.organization_id, source_type="order_delivered", source_id=source_id, lines=lines,
                 memo=f"delivered {order.order_number}")


def expense(session: Session, org_id: uuid.UUID, *, account: str, amount: Decimal, source_type: str, source_id: str,
            portfolio_id: uuid.UUID | None, order_id: uuid.UUID | None = None,
            credit_account: str = A.SUPPLIER_PAYABLES,
            memo: str | None = None, platform_account_id: uuid.UUID | None = None) -> None:
    if amount <= 0 or journal_exists(session, source_type, source_id):
        return
    kw = {"portfolio_id": portfolio_id, "order_id": order_id, "platform_account_id": platform_account_id}
    post_journal(session, org_id, source_type=source_type, source_id=source_id, memo=memo,
                 lines=[JournalLine(account, debit=amount, **kw), JournalLine(credit_account, credit=amount, **kw)])


def supplier_prepayment(session: Session, org_id: uuid.UUID, *, amount: Decimal, portfolio_id: uuid.UUID,
                        order_id: uuid.UUID, source_id: str) -> None:
    """Dropship purchase paid to a marketplace seller: Dr supplier prepayments, Cr cash."""
    expense(session, org_id, account=A.SUPPLIER_PREPAYMENTS, amount=amount, source_type="dropship_purchase",
            source_id=source_id, portfolio_id=portfolio_id, order_id=order_id, credit_account=A.CASH,
            memo="dropship purchase")


def inventory_purchase(session: Session, org_id: uuid.UUID, *, amount: Decimal, portfolio_id: uuid.UUID,
                       source_id: str, paid: bool) -> None:
    expense(session, org_id, account=A.INVENTORY, amount=amount, source_type="inventory_receipt",
            source_id=source_id, portfolio_id=portfolio_id,
            credit_account=A.CASH if paid else A.SUPPLIER_PAYABLES, memo="inventory receipt")


def settlement_received(session: Session, org_id: uuid.UUID, *, source_id: str, receivable_account: str,
                        portfolio_lines: list[tuple[uuid.UUID | None, Decimal, Decimal]], fee_account: str,
                        platform_account_id: uuid.UUID | None) -> None:
    """Cash arrives for previously recognised receivables.
    portfolio_lines: (portfolio_id, gross receivable cleared, fees deducted)."""
    if journal_exists(session, "settlement", source_id):
        return
    lines = []
    for portfolio_id, gross, fees in portfolio_lines:
        kw = {"portfolio_id": portfolio_id, "platform_account_id": platform_account_id}
        lines += [JournalLine(A.CASH, debit=gross - fees, **kw), JournalLine(receivable_account, credit=gross, **kw)]
        if fees:
            lines.append(JournalLine(fee_account, debit=fees, **kw))
    post_journal(session, org_id, source_type="settlement", source_id=source_id, lines=lines, memo="settlement")


def refund_approved(session: Session, order: Order, amount: Decimal, *, source_id: str,
                    revenue_recognised: bool) -> None:
    """Refund obligation: after delivery it reduces revenue (sales returns); before delivery it releases the
    customer advance. Cash leaves only when the refund is paid."""
    if journal_exists(session, "refund_approved", source_id):
        return
    items = _items(session, order.id)
    debit_account = A.SALES_RETURNS if revenue_recognised else A.CUSTOMER_ADVANCES
    lines = []
    for item, share in _split_by_items(items, amount):
        kw = {"portfolio_id": item.portfolio_id, "order_id": order.id, "order_item_id": item.id}
        lines += [JournalLine(debit_account, debit=share, **kw), JournalLine(A.REFUNDS_PAYABLE, credit=share, **kw)]
    post_journal(session, order.organization_id, source_type="refund_approved", source_id=source_id, lines=lines,
                 memo=f"refund {order.order_number}")


def refund_paid(session: Session, order: Order, amount: Decimal, *, source_id: str) -> None:
    if journal_exists(session, "refund_paid", source_id):
        return
    items = _items(session, order.id)
    lines = []
    for item, share in _split_by_items(items, amount):
        kw = {"portfolio_id": item.portfolio_id, "order_id": order.id, "order_item_id": item.id}
        lines += [JournalLine(A.REFUNDS_PAYABLE, debit=share, **kw), JournalLine(A.CASH, credit=share, **kw)]
    post_journal(session, order.organization_id, source_type="refund_paid", source_id=source_id, lines=lines)
