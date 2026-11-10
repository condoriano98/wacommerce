# 14 — FP&A Methodology & Contribution Waterfall

> Source of truth: `docs/00-plan.md` (canonical V3.1 specification incl. §27 normative corrections). This file is a topic view.


## 15. FP&A methodology (revised — P1)

Levels: L1 SKU × platform × channel; L2 portfolio; L3 consolidated (eliminations: intercompany, inter-portfolio stock
transfers at cost).
Waterfall (per order line, aggregated):
Gross sales − discounts − refunds/returns/allowances − revenue adjustments = **Net sales**
Net sales − COGS − marketplace & payment fees − variable shipping & fulfilment = **CM1**
CM1 − attributable marketing (direct attribution first; effective-dated allocation policy) = **CM2**
CM2 − variable AI/customer-service cost − variable return/exception cost = **CM3**
CM3 − allocated fixed opex (documented driver) = **Operating profit**.
Rules: each cost appears at exactly one level (cost-to-level map); taxes per `tax_profiles` (output VAT excluded from net
sales; withholding as prepaid tax, not cost); ad prepayments expensed when consumed; unallocated marketing and shared
opex reported as separate lines at L2/L3 (never silently spread); accounting adjustments shown separately.
Metrics: actual vs budget vs forecast; rolling 13-week cash-flow forecast; working-capital forecast; portfolio liquidity
projection; cash conversion cycle; capital utilisation; contribution per Rp committed; break-even CPA/ROAS per SKU;
inventory turnover, ageing, sell-through; supplier concentration; settlement delay distribution; return and
failed-delivery rates; risk-adjusted bulk-buy evaluator (demand stability, savings, carrying cost, sell-through,
downside, post-buy liquidity); stress tests. Decimal arithmetic, explicit rounding (HALF_UP to 1 rupiah for allocations,
remainder to last line), IDR reporting currency, FX table if foreign currency appears.
**Rp100M simulator**: Portfolios A/B/C, reserve, ad budgets, inventory buys, dropship working capital, fulfilment cost,
settlement delays, fixed opex, test-and-learn acquisition cost, reinvestment/recovery. Stress (correction 8): S1 CPA
+30/+60%, S2 CVR −25%, S3 returns ×2, S4 COD delay +7/+14d, S5 supplier cost +10/+20%, S6 sell-through −40%, S7 ROAS −35%,
S8 channel outage 7d, S9 marketplace settlement delay +14d with platform fee +2pp; "combined downside" preset
(S1+S2+S4+S5). Outputs per portfolio and consolidated (deterministic): profit, liquidity path, minimum cash and shortfall
date, capital recovery horizon, capital productivity; base case reconciles to ledger actuals for elapsed periods.
Probabilities (Monte Carlo) only after distributions are documented and validated. Advisory only.
**Close & audit**: `accounting_periods` (open → soft close → closed), `fpa_snapshots` versioned, adjustment journals,
restatement only via adjustments with approval, effective-dated allocations, separate operational vs accounting
timestamps, immutable journals/audit.

## 10. Capital allocation, commitments, cash and liquidity (revised — P0.5)

Five distinct concepts:
1. **Capital mandate** (management budget per portfolio/category/period; effective-dated; approval-backed) — management
   ledger, **no GL entry**.
2. **Capital commitment** (reserved against a mandate for a specific purpose: inventory PO, supplier purchase, ad
   budget, opex) — proposed/approved/consumed/released; consumption linked to the GL journal that spent the money.
3. **Actual cash transaction** — GL journals on bank/gateway/carrier accounts of a **legal entity**.
4. **Working capital** — portfolio-dimensioned balance-sheet lines: inventory + goods in transit + receivables (COD,
   marketplace, gateway) + supplier prepayments − payables − contract liabilities.
5. **Unrestricted liquidity** — bank + clearing balances − committed outflows due − liquidity reserve − refund payables
   (shown both with and without customer-advance cash).
Reallocation between portfolios = mandate events only (D8). Real money moves only between **legal-entity bank
accounts** (intercompany journals 1900/2900, eliminated in consolidation). Capital productivity = CM3 ÷ average
committed + deployed capital. Commitment checks gate procurement and ad-budget recommendations.
