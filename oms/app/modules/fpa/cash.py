"""13-week cash forecast from ledger balances (spec §15) and the risk-adjusted bulk-buy evaluator (docs/15).

Forecast: opening verified bank cash (1000) + provider payouts awaiting bank proof (1050, week 1) + expected
settlement of open receivables by channel delay − payables − open capital
commitments. Transfers pending bank verification (1105) are excluded until matched (no asset for unverified money);
the allowance (1109) is shown, never netted silently.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.capital import CapitalCommitment
from app.models.finance import LedgerEntry
from app.modules.finance import accounts as A

Z = Decimal("0")
# 1050 (provider-reported payouts not yet proven by a bank line) is an expected inflow, never opening cash (B5).
RECEIVABLE_DELAY_WEEKS = {A.FUNDS_IN_TRANSIT: 1, A.GATEWAY_CLEARING: 1, A.COD_RECEIVABLE: 2, A.MARKETPLACE_RECEIVABLE: 2,
                          A.SUPPLIER_REFUND_RECEIVABLE: 2, A.CARRIER_BALANCE: 1}
PAYABLE_DUE_WEEKS = {A.SUPPLIER_PAYABLES: 1, A.CARRIER_PAYABLES: 2, A.ADS_PAYABLES: 1, A.REFUNDS_PAYABLE: 1}


def _balances(session: Session, legal_entity_id=None) -> dict[str, Decimal]:
    q = select(LedgerEntry.account_code, func.sum(LedgerEntry.debit - LedgerEntry.credit)).group_by(
        LedgerEntry.account_code)
    if legal_entity_id is not None:
        q = q.where(LedgerEntry.legal_entity_id == legal_entity_id)
    return {a: Decimal(v) for a, v in session.execute(q).all()}


def forecast_13w(session: Session, legal_entity_id=None) -> dict:
    b = _balances(session, legal_entity_id)
    weeks = [{"week": w, "inflow": Z, "outflow": Z} for w in range(13)]
    for acct, wk in RECEIVABLE_DELAY_WEEKS.items():
        if b.get(acct, Z) > 0:
            weeks[wk]["inflow"] += b[acct]
    for acct, wk in PAYABLE_DUE_WEEKS.items():
        if b.get(acct, Z) < 0:
            weeks[wk]["outflow"] += -b[acct]
    # Open commitments of THIS entity only, in the week their payment is expected (default: next week). Consumed
    # amounts are excluded: they are already payables or cash out in the ledger (no double count).
    cq = select(CapitalCommitment.amount - CapitalCommitment.consumed_amount,
                CapitalCommitment.expected_payment_date).where(
        CapitalCommitment.status.in_(("approved", "partially_consumed")))
    if legal_entity_id is not None:
        cq = cq.where(CapitalCommitment.legal_entity_id == legal_entity_id)
    today = date.today()
    for open_amount, pay_date in session.execute(cq).all():
        week = 1 if pay_date is None else min(max((pay_date - today).days // 7, 0), 12)
        weeks[week]["outflow"] += Decimal(open_amount)
    cash = b.get(A.CASH, Z)
    for w in weeks:
        cash += w["inflow"] - w["outflow"]
        w["closing_cash"] = cash
    return {"opening_cash": str(b.get(A.CASH, Z)),  # verified bank cash only
            "funds_in_transit_unverified": str(b.get(A.FUNDS_IN_TRANSIT, Z)),
            "excluded_unverified_transfers": str(b.get(A.TRANSFER_RECEIVABLE, Z)),
            "credit_loss_allowance": str(b.get(A.CREDIT_LOSS_ALLOWANCE, Z)),
            "weeks": [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in w.items()} for w in weeks],
            "minimum_closing_cash": str(min(w["closing_cash"] for w in weeks))}


@dataclass(frozen=True)
class BulkBuyInputs:
    weekly_demand: list[int]  # delivered units per week (≥ 4 weeks)
    qty: int
    dropship_unit_cost: Decimal
    bulk_unit_cost: Decimal
    carrying_cost_per_unit_week: Decimal
    max_holding_weeks: int
    supplier_reliability: Decimal  # 0..1 (on-time, as-described)
    return_rate: Decimal
    salvage_rate: Decimal  # recovery on unsold stock
    post_purchase_min_cash: Decimal  # from the combined-downside simulation including this purchase
    reserve: Decimal
    risk_limit: Decimal  # maximum acceptable downside loss for the portfolio
    max_cv: Decimal = Decimal("0.35")
    min_reliability: Decimal = Decimal("0.95")
    max_return_rate: Decimal = Decimal("0.08")


def evaluate_bulk_buy(i: BulkBuyInputs) -> dict:
    """All rules must hold; a low supplier price alone is never enough (docs/15)."""
    reasons, d = [], [Decimal(x) for x in i.weekly_demand]
    if len(d) < 4:
        reasons.append("fewer than 4 weeks of delivered demand")
        mean = cv = Z
    else:
        mean = sum(d, Z) / len(d)
        var = sum(((x - mean) ** 2 for x in d), Z) / len(d)
        cv = (var.sqrt() / mean) if mean else Decimal("99")
        if cv > i.max_cv:
            reasons.append(f"demand unstable (CV {cv:.2f} > {i.max_cv})")
    weeks_to_sell = (Decimal(i.qty) / mean) if mean else Decimal("999")
    saving = (i.dropship_unit_cost - i.bulk_unit_cost) * i.qty
    carrying = i.carrying_cost_per_unit_week * i.qty * weeks_to_sell / 2
    if saving - carrying <= 0:
        reasons.append("saving after carrying cost is not positive")
    if weeks_to_sell > i.max_holding_weeks:
        reasons.append(f"expected sell-through {weeks_to_sell:.1f} weeks exceeds {i.max_holding_weeks}")
    if i.supplier_reliability < i.min_reliability:
        reasons.append("supplier reliability below threshold")
    if i.return_rate > i.max_return_rate:
        reasons.append("return rate above threshold")
    if i.post_purchase_min_cash < i.reserve:
        reasons.append("liquidity would fall below the reserve under the combined downside")
    unsold = max(Decimal(i.qty) - mean * i.max_holding_weeks, Z)
    downside = unsold * i.bulk_unit_cost * (1 - i.salvage_rate)
    if downside > i.risk_limit:
        reasons.append("downside loss on unsold stock exceeds the risk limit")
    return {"recommend": not reasons, "reasons": reasons, "net_saving": str(saving - carrying),
            "weeks_to_sell": str(weeks_to_sell.quantize(Decimal("0.1"))), "downside_loss": str(downside)}
