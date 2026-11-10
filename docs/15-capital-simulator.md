# 15 — Rp100M Capital Simulator & Stress Scenarios

> Source: `docs/00-plan.md` §10 and §15. Advisory only: no automatic ad spend, fund movement or bulk purchase.

## Baseline scenario (editable; illustrative, not benchmarks)

| Category | Amount (Rp) |
|---|---:|
| Advertising | 27,000,000 |
| Inventory and supplier float | 45,000,000 |
| Fulfillment working capital | 10,000,000 |
| Software, salaries, opex | 6,000,000 |
| Liquidity reserve | 12,000,000 |
| **Total** | **100,000,000** |

Default risk thresholds (editable, every override audited): minimum cash reserve Rp12,000,000; platform revenue
concentration review 35%; operating margin review 5%; pause recommendation below SKU break-even ROAS.

## Model structure
Weekly time steps over 26 weeks, per portfolio (A, B, C) and consolidated.
- **Inputs per portfolio**: mandate by category; channel mix; CPA/CAC; conversion rate; AOV and price; COGS or supplier
  price; marketplace and payment fees; fulfilment cost; return and failed-delivery rates; settlement delay distribution
  (DOKU, COD remittance, marketplace payout); inventory MOQ, lead time and sell-through; holding period; fixed opex
  allocation; test-and-learn budget; reinvestment rule.
- **Engine**: order volume from ad spend ÷ CPA (or a conversion funnel); CM1–CM3 per order (`14-fpa-methodology.md`);
  cash timing from settlement delays; inventory cash out at purchase and cash in at settlement; dropship supplier cash
  out at purchase; commitments consume mandates.
- **Outputs**: P&L by week; cash balance path; minimum cash and the week it occurs; probability of breaching the
  reserve (Monte Carlo only after distributions are documented and validated — ADR-017; until then deterministic minimum-cash and shortfall date); capital recovery horizon; contribution per Rp committed;
  working capital tied up; recommendation list (scale / hold / cut / switch fulfilment / bulk-buy) with rationale.

## Stress scenarios

| Scenario | Shock |
|---|---|
| CPA/CAC up | +30% and +60% |
| Conversion down | −25% |
| Returns up | return and failed-delivery rates ×2 |
| COD settlement delay | +7 and +14 days |
| Supplier cost up | +10% and +20% |
| Sell-through slows | −40% |
| Ad underperformance | ROAS −35% |
| Integration outage | one channel offline 7 days (orders lost, costs continue) |
| S9 Marketplace settlement delay + fee increase | payout delay +14 days and platform fee +2 percentage points |
| Preset: combined downside (not a scenario number) | S1 +30%, S2 −15%, S4 +7d, S5 +10% together |

## Bulk-buy evaluator (risk-adjusted)
Recommend switching a dropship SKU to owned stock only if all hold: delivered demand is stable (coefficient of variation
below threshold over ≥4 weeks); unit saving after carrying cost is positive; expected sell-through within the maximum
holding period; supplier reliability ≥ threshold; return rate ≤ threshold; post-purchase liquidity stays above the
reserve under the combined downside; downside loss (unsold stock at salvage) is within the portfolio risk limit.
A low supplier price alone is never enough.

## Status
**Implemented & tested (M3, 2026-11-10)** — `oms/app/modules/fpa/simulator.py` (engine `sim-1.0`): deterministic weekly
Decimal engine; elapsed weeks can be replaced by ledger actuals; S1–S9 and the combined-downside preset as pure input
transforms; Rp100M A/B/C seed (illustrative planning inputs, not actuals); runs persisted in `simulation_runs`; reserve
and margin breaches open `liquidity_breach` / `margin_breach` cases with exposure (`fpa/risk.py`). Bulk-buy evaluator in
`fpa/cash.py`. Monte Carlo remains **Planned** and raises while `MONTE_CARLO_ENABLED` is false (ADR-017).
