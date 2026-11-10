"""Contribution waterfall from ledger truth (spec §15).

Gross sales − returns/allowances = net sales; − CM1 costs (COGS, shipping, payment & marketplace fees, packing) = CM1;
− attributable marketing = CM2; − variable return/AI-CS/credit-loss cost = CM3; − allocated opex = operating profit.
Every P&L account maps to exactly one level (``cost_level_map``). Lines without a portfolio (unallocated marketing,
shared opex) are never spread: they appear as separate ``unallocated`` lines and only in legal-entity (L3) totals.
Amounts are signed contributions (revenue positive, costs negative); all arithmetic is Decimal.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.finance import LedgerAccount, LedgerEntry
from app.models.fpa import CostLevel
from app.models.orders import Order, OrderItem

LEVELS = ("gross_sales", "returns_allowances", "cm1", "cm2", "cm3", "opex")
Z = Decimal("0")


@dataclass
class Waterfall:
    scope: str
    by_level: dict[str, Decimal] = field(default_factory=lambda: dict.fromkeys(LEVELS, Z))
    by_account: dict[str, Decimal] = field(default_factory=dict)
    unallocated: dict[str, Decimal] = field(default_factory=dict)

    @property
    def net_sales(self) -> Decimal:
        return self.by_level["gross_sales"] + self.by_level["returns_allowances"]

    @property
    def cm1(self) -> Decimal:
        return self.net_sales + self.by_level["cm1"]

    @property
    def cm2(self) -> Decimal:
        return self.cm1 + self.by_level["cm2"]

    @property
    def cm3(self) -> Decimal:
        return self.cm2 + self.by_level["cm3"]

    @property
    def operating_profit(self) -> Decimal:
        return self.cm3 + self.by_level["opex"]

    def as_dict(self) -> dict:
        return {"scope": self.scope, "gross_sales": str(self.by_level["gross_sales"]),
                "returns_allowances": str(self.by_level["returns_allowances"]), "net_sales": str(self.net_sales),
                "cm1_costs": str(self.by_level["cm1"]), "cm1": str(self.cm1),
                "marketing": str(self.by_level["cm2"]), "cm2": str(self.cm2),
                "cm3_costs": str(self.by_level["cm3"]), "cm3": str(self.cm3),
                "opex": str(self.by_level["opex"]), "operating_profit": str(self.operating_profit),
                "unallocated": {k: str(v) for k, v in sorted(self.unallocated.items())},
                "by_account": {k: str(v) for k, v in sorted(self.by_account.items())}}


def _rows(session: Session, start: date, end: date, legal_entity_id: uuid.UUID | None):
    q = (select(CostLevel.level, LedgerEntry.account_code, LedgerEntry.portfolio_id,
                func.sum(LedgerEntry.credit - LedgerEntry.debit))
         .join(CostLevel, CostLevel.account_code == LedgerEntry.account_code)
         .where(LedgerEntry.accounting_date >= start, LedgerEntry.accounting_date <= end)
         .group_by(CostLevel.level, LedgerEntry.account_code, LedgerEntry.portfolio_id))
    if legal_entity_id is not None:
        q = q.where(LedgerEntry.legal_entity_id == legal_entity_id)
    return session.execute(q).all()


def compute(session: Session, start: date, end: date, *, legal_entity_id: uuid.UUID | None = None,
            portfolio_id: uuid.UUID | None = None) -> Waterfall:
    """L2 when ``portfolio_id`` is given (only that portfolio's lines); otherwise L3 for the legal entity (or every
    entity visible to the caller), including unallocated lines shown separately. RLS limits what the caller sees."""
    wf = Waterfall(scope=f"portfolio:{portfolio_id}" if portfolio_id else f"legal_entity:{legal_entity_id or 'all'}")
    for level, account, pid, amount in _rows(session, start, end, legal_entity_id):
        if portfolio_id is not None and pid != portfolio_id:
            continue
        amount = Decimal(amount)
        wf.by_level[level] += amount
        wf.by_account[account] = wf.by_account.get(account, Z) + amount
        if pid is None:
            wf.unallocated[level] = wf.unallocated.get(level, Z) + amount
    return wf


def by_portfolio(session: Session, start: date, end: date, legal_entity_id: uuid.UUID | None = None) -> dict:
    pids = {pid for _, _, pid, _ in _rows(session, start, end, legal_entity_id) if pid is not None}
    return {pid: compute(session, start, end, legal_entity_id=legal_entity_id, portfolio_id=pid) for pid in pids}


def l1(session: Session, start: date, end: date, legal_entity_id: uuid.UUID | None = None) -> list[dict]:
    """SKU × platform account × channel for lines carrying an order item; order-level/settlement-level costs stay at L2."""
    q = (select(OrderItem.sku_id, Order.platform_account_id, Order.sales_channel, CostLevel.level,
                func.sum(LedgerEntry.credit - LedgerEntry.debit))
         .join(CostLevel, CostLevel.account_code == LedgerEntry.account_code)
         .join(OrderItem, OrderItem.id == LedgerEntry.order_item_id)
         .join(Order, Order.id == OrderItem.order_id)
         .where(LedgerEntry.accounting_date >= start, LedgerEntry.accounting_date <= end)
         .group_by(OrderItem.sku_id, Order.platform_account_id, Order.sales_channel, CostLevel.level))
    if legal_entity_id is not None:
        q = q.where(LedgerEntry.legal_entity_id == legal_entity_id)
    out: dict[tuple, dict] = {}
    for sku, acct, channel, level, amount in session.execute(q).all():
        row = out.setdefault((sku, acct, channel), {"sku_id": str(sku), "platform_account_id": str(acct),
                                                    "channel": channel, **{lv: Z for lv in LEVELS}})
        row[level] += Decimal(amount)
    for row in out.values():
        row["cm1"] = row["gross_sales"] + row["returns_allowances"] + row["cm1"]
        for lv in LEVELS:
            row[lv] = str(row[lv])
        row["cm1"] = str(row["cm1"])
    return list(out.values())


def ledger_pnl(session: Session, start: date, end: date, legal_entity_id: uuid.UUID | None = None) -> Decimal:
    q = (select(func.coalesce(func.sum(LedgerEntry.credit - LedgerEntry.debit), 0))
         .join(LedgerAccount, LedgerAccount.code == LedgerEntry.account_code)
         .where(LedgerAccount.type.in_(("revenue", "expense")),
                LedgerEntry.accounting_date >= start, LedgerEntry.accounting_date <= end))
    if legal_entity_id is not None:
        q = q.where(LedgerEntry.legal_entity_id == legal_entity_id)
    return Decimal(session.scalar(q))


def unmapped_pnl_accounts(session: Session) -> list[str]:
    return list(session.scalars(select(LedgerAccount.code).where(
        LedgerAccount.type.in_(("revenue", "expense")), LedgerAccount.active.is_(True),
        LedgerAccount.code.not_in(select(CostLevel.account_code)))))


def tie_out(session: Session, start: date, end: date, legal_entity_id: uuid.UUID | None = None) -> dict:
    """Waterfall operating profit must equal the ledger P&L for the same period and entity."""
    wf = compute(session, start, end, legal_entity_id=legal_entity_id)
    pnl = ledger_pnl(session, start, end, legal_entity_id)
    unmapped = unmapped_pnl_accounts(session)
    return {"ok": wf.operating_profit == pnl and not unmapped, "waterfall_operating_profit": str(wf.operating_profit),
            "ledger_pnl": str(pnl), "unmapped_accounts": unmapped}
