"""Deterministic capital simulator (spec §15, docs/15, ADR-017).

Weekly steps per portfolio; consolidated = sum. Everything is Decimal and reproducible: the same inputs always give the
same outputs (no randomness). Elapsed weeks can be replaced by ledger actuals (``actual_weeks``) so the base case
reconciles to the books. Stress scenarios S1–S9 are pure transforms of the inputs. Monte Carlo is not implemented: it
stays behind ``MONTE_CARLO_ENABLED`` until distributions are documented and validated (ADR-017).

Cash timing: ad spend, dropship supplier cost, fulfilment and opex are paid in the week incurred; owned stock is bought
up front (``inventory_buy``) and consumed as COGS; customer cash arrives after each channel's settlement delay, net of
channel fees; returns refund the order value at settlement time.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal

ENGINE_VERSION = "sim-1.0"
MONTE_CARLO_ENABLED = False
Z = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class Channel:
    share: Decimal  # of orders
    settlement_delay_days: int
    fee_rate: Decimal  # payment / COD / marketplace fees, share of order value


@dataclass(frozen=True)
class PortfolioInputs:
    code: str
    weekly_ad_spend: Decimal
    cpa: Decimal  # ad cost per order
    aov: Decimal  # average order value (goods + shipping income)
    cogs_rate: Decimal  # supplier / product cost as share of order value
    fulfilment_cost: Decimal  # per order (shipping, packing)
    return_rate: Decimal
    return_cost: Decimal  # per returned order (return shipping, damage)
    fixed_opex_weekly: Decimal
    channels: dict[str, Channel]
    owned_share: Decimal = Z  # share of orders served from owned stock (bought up front)
    inventory_buy: Decimal = Z
    conversion_factor: Decimal = ONE  # S2
    sell_through_factor: Decimal = ONE  # S6 (owned-stock demand)
    outage: tuple[str, int, int] | None = None  # S8: (channel, first week, weeks)


@dataclass(frozen=True)
class SimInputs:
    opening_cash: Decimal
    reserve: Decimal
    portfolios: tuple[PortfolioInputs, ...]
    weeks: int = 26


@dataclass
class PortfolioResult:
    code: str
    weekly_operating_profit: list[Decimal] = field(default_factory=list)
    weekly_cm2: list[Decimal] = field(default_factory=list)
    weekly_cash_flow: list[Decimal] = field(default_factory=list)
    revenue: Decimal = Z
    cm2: Decimal = Z
    operating_profit: Decimal = Z
    committed: Decimal = Z  # ad spend + inventory bought

    @property
    def contribution_per_rp_committed(self) -> Decimal:
        return (self.cm2 / self.committed).quantize(Decimal("0.0001"), ROUND_HALF_UP) if self.committed else Z

    @property
    def recovery_week(self) -> int | None:
        """First week from which cumulative cash flow stays ≥ 0 (capital recovered)."""
        cum, first = Z, None
        for i, cf in enumerate(self.weekly_cash_flow):
            cum += cf
            if cum >= 0 and first is None:
                first = i
            elif cum < 0:
                first = None
        return first


def _week_of(days: int) -> int:
    return -(-days // 7)  # ceil


def _portfolio(p: PortfolioInputs, weeks: int, actual: dict[int, dict] | None) -> PortfolioResult:
    r = PortfolioResult(code=p.code)
    inflows = [Z] * (weeks + 60)
    outflows = [Z] * (weeks + 60)
    outflows[0] += p.inventory_buy
    r.committed = p.inventory_buy
    for w in range(weeks):
        if actual and w in actual:  # ledger actuals for elapsed weeks (base case reconciliation)
            a = actual[w]
            cm2, op = Decimal(a["cm2"]), Decimal(a["operating_profit"])
            r.weekly_cm2.append(cm2)
            r.weekly_operating_profit.append(op)
            r.revenue += Decimal(a["net_sales"])
            r.cm2 += cm2
            r.operating_profit += op
            inflows[w] += Decimal(a.get("cash_in", "0"))
            outflows[w] += Decimal(a.get("cash_out", "0"))
            continue
        base_orders = p.weekly_ad_spend / p.cpa * p.conversion_factor
        owned = base_orders * p.owned_share * p.sell_through_factor
        orders_total = base_orders * (ONE - p.owned_share) + owned
        revenue = cm2 = Z
        for name, ch in sorted(p.channels.items()):
            if p.outage and p.outage[0] == name and p.outage[1] <= w < p.outage[1] + p.outage[2]:
                continue  # channel offline: orders lost, costs continue
            orders = orders_total * ch.share
            value = orders * p.aov
            returned = orders * p.return_rate
            refunds = returned * p.aov
            fees = value * ch.fee_rate
            cogs = value * p.cogs_rate
            dropship_cogs = cogs * (ONE - p.owned_share)
            fulfil = orders * p.fulfilment_cost + returned * p.return_cost
            revenue += value - refunds
            cm2 += value - refunds - fees - cogs - fulfil
            inflows[w + _week_of(ch.settlement_delay_days)] += value - refunds - fees
            outflows[w] += dropship_cogs + fulfil
        cm2 -= p.weekly_ad_spend
        outflows[w] += p.weekly_ad_spend + p.fixed_opex_weekly
        r.committed += p.weekly_ad_spend
        op = cm2 - p.fixed_opex_weekly
        r.weekly_cm2.append(cm2)
        r.weekly_operating_profit.append(op)
        r.revenue += revenue
        r.cm2 += cm2
        r.operating_profit += op
    r.weekly_cash_flow = [inflows[w] - outflows[w] for w in range(weeks)]
    return r


def _q(v: Decimal) -> str:
    return str(v.quantize(Decimal("1"), ROUND_HALF_UP))


def run(inputs: SimInputs, actual_weeks: dict[str, dict[int, dict]] | None = None) -> dict:
    results = [_portfolio(p, inputs.weeks, (actual_weeks or {}).get(p.code)) for p in inputs.portfolios]
    cash, path = inputs.opening_cash, []
    for w in range(inputs.weeks):
        cash += sum((r.weekly_cash_flow[w] for r in results), Z)
        path.append(cash)
    min_cash = min(path) if path else inputs.opening_cash
    min_week = path.index(min_cash) if path else 0
    shortfall = next((i for i, c in enumerate(path) if c < inputs.reserve), None)
    return {
        "engine_version": ENGINE_VERSION,
        "inputs_hash": inputs_hash(inputs),
        "consolidated": {
            "revenue": _q(sum((r.revenue for r in results), Z)),
            "cm2": _q(sum((r.cm2 for r in results), Z)),
            "operating_profit": _q(sum((r.operating_profit for r in results), Z)),
            "cash_path": [_q(c) for c in path],
            "min_cash": _q(min_cash), "min_cash_week": min_week,
            "reserve": _q(inputs.reserve), "reserve_breach_week": shortfall,
        },
        "portfolios": {r.code: {
            "revenue": _q(r.revenue), "cm2": _q(r.cm2), "operating_profit": _q(r.operating_profit),
            "contribution_per_rp_committed": str(r.contribution_per_rp_committed),
            "recovery_week": r.recovery_week,
            "weekly_operating_profit": [_q(v) for v in r.weekly_operating_profit],
        } for r in results},
    }


def _jsonable(o):
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(type(o))


def inputs_hash(inputs: SimInputs) -> str:
    return hashlib.sha256(json.dumps(asdict(inputs), default=_jsonable, sort_keys=True).encode()).hexdigest()


# ---- stress scenarios (spec §15, docs/15): pure input transforms ----
def _each(inputs: SimInputs, fn) -> SimInputs:
    return replace(inputs, portfolios=tuple(fn(p) for p in inputs.portfolios))


def _channels(p: PortfolioInputs, name: str, **changes) -> dict[str, Channel]:
    return {k: (replace(c, **{f: v(c) for f, v in changes.items()}) if k == name else c) for k, c in p.channels.items()}


SCENARIOS = {
    "S1_cpa_+30": lambda i: _each(i, lambda p: replace(p, cpa=p.cpa * Decimal("1.30"))),
    "S1_cpa_+60": lambda i: _each(i, lambda p: replace(p, cpa=p.cpa * Decimal("1.60"))),
    "S2_cvr_-25": lambda i: _each(i, lambda p: replace(p, conversion_factor=p.conversion_factor * Decimal("0.75"))),
    "S3_returns_x2": lambda i: _each(i, lambda p: replace(p, return_rate=p.return_rate * 2)),
    "S4_cod_delay_+7d": lambda i: _each(i, lambda p: replace(p, channels=_channels(
        p, "cod", settlement_delay_days=lambda c: c.settlement_delay_days + 7))),
    "S4_cod_delay_+14d": lambda i: _each(i, lambda p: replace(p, channels=_channels(
        p, "cod", settlement_delay_days=lambda c: c.settlement_delay_days + 14))),
    "S5_supplier_+10": lambda i: _each(i, lambda p: replace(p, cogs_rate=p.cogs_rate * Decimal("1.10"))),
    "S5_supplier_+20": lambda i: _each(i, lambda p: replace(p, cogs_rate=p.cogs_rate * Decimal("1.20"))),
    "S6_sell_through_-40": lambda i: _each(i, lambda p: replace(
        p, sell_through_factor=p.sell_through_factor * Decimal("0.60"))),
    "S7_roas_-35": lambda i: _each(i, lambda p: replace(p, aov=p.aov * Decimal("0.65"))),
    "S8_whatsapp_outage_7d": lambda i: _each(i, lambda p: replace(p, outage=("prepaid", 1, 1))),
    "S9_marketplace_delay_+14d_fee_+2pp": lambda i: _each(i, lambda p: replace(p, channels=_channels(
        p, "marketplace", settlement_delay_days=lambda c: c.settlement_delay_days + 14,
        fee_rate=lambda c: c.fee_rate + Decimal("0.02")))),
}
# Preset (not a scenario number): docs/15 combined downside.
PRESETS = {
    "combined_downside": lambda i: _each(i, lambda p: replace(
        p, cpa=p.cpa * Decimal("1.30"), conversion_factor=p.conversion_factor * Decimal("0.85"),
        cogs_rate=p.cogs_rate * Decimal("1.10"),
        channels=_channels(p, "cod", settlement_delay_days=lambda c: c.settlement_delay_days + 7))),
}


def stress(inputs: SimInputs, names: list[str] | None = None) -> dict[str, dict]:
    table = {**SCENARIOS, **PRESETS}
    return {n: run(table[n](inputs)) for n in (names or list(table))}


def monte_carlo(*_a, **_k):
    if not MONTE_CARLO_ENABLED:
        raise RuntimeError("Monte Carlo is disabled until distributions are documented and validated (ADR-017)")
    raise NotImplementedError


def rp100m_seed() -> SimInputs:
    """The Rp100M three-portfolio scenario (docs/15): A FMCG via WhatsApp (prepaid + COD, owned stock option),
    B Instagram → WhatsApp dropship, C Shopee/TikTok Shop native. Illustrative planning inputs, not actuals."""
    wa = {"prepaid": Channel(Decimal("0.6"), 2, Decimal("0.0070")),
          "cod": Channel(Decimal("0.4"), 10, Decimal("0.0300"))}
    mp = {"marketplace": Channel(ONE, 14, Decimal("0.0800"))}
    return SimInputs(opening_cash=Decimal("100000000"), reserve=Decimal("15000000"), portfolios=(
        PortfolioInputs("A", Decimal("2500000"), Decimal("35000"), Decimal("165000"), Decimal("0.55"),
                        Decimal("14000"), Decimal("0.05"), Decimal("15000"), Decimal("750000"), wa,
                        owned_share=Decimal("0.5"), inventory_buy=Decimal("15000000")),
        PortfolioInputs("B", Decimal("2000000"), Decimal("38000"), Decimal("175000"), Decimal("0.60"),
                        Decimal("12000"), Decimal("0.06"), Decimal("15000"), Decimal("500000"), wa),
        PortfolioInputs("C", Decimal("1500000"), Decimal("22000"), Decimal("120000"), Decimal("0.58"),
                        Decimal("6000"), Decimal("0.04"), Decimal("12000"), Decimal("500000"), mp),
    ))
