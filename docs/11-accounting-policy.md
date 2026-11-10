# 11 — Accounting Policy & Journal Mappings

> Source: `docs/00-plan.md` §9 (ADR-008). Gates: **G-ACC1** (framework: SAK EP vs SAK EMKM, revenue trigger sign-off by
> the company accountant) and **G-TAX1** (PKP/PPN, PPh 22). Defaults below are configurable per legal entity and
> versioned per posting rule.


Framework: **G-ACC1** confirm with the company's accountant — SAK EP (private entities, effective 2025) or SAK EMKM;
principles below follow IFRS 15/PSAK 72-consistent control-transfer logic and are configurable per legal entity.
Tax: **G-TAX1** PKP status / PPN rate and PPh 22 withheld by marketplaces → `tax_profiles`; until confirmed, tax lines are
recorded as separate allocations (not netted into revenue silently).

Policies:
1. Order confirmation creates no journal.
2. Prepaid collection before control transfer → **contract liability** (customer advances), never revenue.
3. Revenue + COGS recognised **per shipment line at control transfer** (default trigger: carrier `delivered`;
   configurable per channel/payment term, e.g. platform "completed"). Partial deliveries recognise only delivered qty.
4. Owned stock leaving the warehouse moves Inventory → **Goods in transit** (keeps stock subledger = GL); COGS at delivery.
5. COD: at delivery Dr COD receivable (carrier) / Cr revenue; COD fee and carrier-deducted shipping accrued at delivery
   against the receivable; remittance clears the receivable to carrier balance or bank. **No revenue waiting on cash.**
6. Marketplace: at delivery Dr marketplace receivable / Cr revenue; expected fees accrued from listing fee profile;
   settlement trues up fees/tax/adjustments; payout clears receivable to bank.
7. Expenses are accrued once (source-keyed) and their payment only clears the payable (never re-expensed).
8. Advertising: accrued daily from insights (Dr Advertising / Cr ads payable or prepaid ad balance); invoice/charge
   clears; 28-day re-pulls post **delta adjustments** in the open period referencing the original date.
9. Refunds: before revenue → reduce contract liability; after revenue → sales returns & allowances; cash on payout.
10. Returns: resellable → Dr Inventory / Cr COGS at original cost; damaged → reclassify to return losses.
11. Dropship: supplier payment = **supplier prepayment** asset until customer delivery → COGS; supplier cancellation →
    supplier refund receivable.
12. Every posting rule is versioned (`posting_rule_version`), idempotent on (source_type, source_id), and posts into an
    open period of the legal entity; closed periods accept only adjustment journals in the current period.

Chart of accounts (renumbered; contract liability moved to the liability range):
1000 Bank · 1010 Gateway receivable (DOKU) · 1020 Carrier balance (Biteship) · 1030 Prepaid ad balances · 1100 COD
receivable · 1110 Marketplace receivable · 1120 Supplier refund receivable · 1200 Inventory · 1250 Goods in transit ·
1210 Supplier prepayments · 1400 Prepaid taxes (PPh 22 creditable) · 1900 Intercompany receivable · 2000 Supplier
payables · 2010 Carrier payables · 2020 Ads payables · 2100 Refunds payable · 2200 Contract liabilities (customer
advances) · 2300 Taxes payable · 2900 Intercompany payable · 3000 Owner capital · 3900 Retained earnings · 4000 Sales
revenue · 4100 Shipping income · 4900 Sales returns & allowances · 5000 COGS · 5100 Shipping expense · 5200 Payment &
COD fees · 5300 Marketplace fees · 5400 Packing · 5500 Return & failed-delivery losses · 5600 Variable AI/CS cost ·
6000 Advertising · 6900 Operating expenses.

Journal fixtures (executable test data at Milestone 1; amounts IDR):
- **F1 Prepaid WhatsApp (DOKU)**, goods 190,000 + shipping 15,000, COGS 120,000, gateway fee 1,435, Biteship 12,000:
  capture Dr1010 205,000/Cr2200 205,000 · fee (known at capture) Dr5200 1,435/Cr1010 1,435 · pickup Dr1250 120,000/
  Cr1200 120,000; Dr5100 12,000/Cr2010 12,000 · delivery Dr2200 205,000/Cr4000 190,000/Cr4100 15,000; Dr5000 120,000/
  Cr1250 120,000 · disbursement Dr1000 203,565/Cr1010 203,565.
- **F2 COD**, goods 150,000 + shipping 15,000, COD fee 4,950, carrier-deducted shipping 12,000: pickup Dr1250/Cr1200 ·
  delivery Dr1100 165,000/Cr4000 150,000/Cr4100 15,000; Dr5000/Cr1250; Dr5200 4,950/Cr1100 4,950; Dr5100 12,000/
  Cr1100 12,000 · remittance Dr1020 148,050/Cr1100 148,050 · withdrawal Dr1000/Cr1020 (less withdrawal fee → 5200).
- **F3 Manual transfer**: reported (no journal) → attested with bank ref Dr1000/Cr2200 (bank_matched later confirms) →
  delivery as F1.
- **F4 Marketplace**, 240,000, expected fee 19,200, actual 20,000, PPh 22 1,200: delivery Dr1110 240,000/Cr4000;
  Dr5300 19,200/Cr1110 · settlement Dr1000 218,800/Dr5300 800/Dr1400 1,200/Cr1110 220,800.
- **F5 Dropship**, prepaid 165,000, supplier 100,000: capture Dr1010/Cr2200 · purchase Dr1210 100,000/Cr1000 ·
  delivery Dr2200 165,000/Cr4000/Cr4100; Dr5000 100,000/Cr1210.
- **F6 Partial fulfilment**: 2-line prepaid order; line 1 delivered (F1 for line 1 only); line 2 canceled before
  shipment Dr2200/Cr2100; refund paid Dr2100/Cr1000.
- **F7 Refund (allowance, no return)** after delivery: Dr4900/Cr2100 → paid Dr2100/Cr1000.
- **F8 Return resellable**: Dr4900/Cr2100; Dr1200/Cr5000 at original cost; return shipping Dr5500/Cr2010.
- **F9 Settlement fee deduction** (DOKU fee unknown until disbursement): disbursement Dr1000/Dr5200/Cr1010.
- **F10 Failed delivery (RTS)**: no revenue; goods back Dr1200/Cr1250; outbound + return shipping Dr5100/Dr5500/
  Cr2010; prepaid → refund or reship per case decision.
- **F11 Supplier cancellation**: Dr1120/Cr1210 → refund received Dr1000/Cr1120.
- **F12 Ad spend & restatement**: daily accrual Dr6000/Cr2020 per allocation (portfolio or unallocated); re-pull delta
  ±Dr6000/Cr2020 in open period; card charge Dr2020/Cr1000.
Invariants: every journal balances (deferred trigger); stock subledger value = 1200 + 1250 per (legal entity, portfolio);
contract liabilities = Σ prepaid undelivered order value; COD/marketplace receivables = Σ open expected settlements.


## Executable fixtures
`docs/fixtures/journals.yaml` encodes F1–F12 line by line. Verified on 2026-11-10: every journal balances; after each
scenario completes, contract liabilities (2200), goods in transit (1250) and settlement receivables (1010/1100/1110/1020)
net to zero, so revenue never waits on cash and cash never inflates revenue. F8 intentionally leaves the refund payable
(2100) open until the refund is paid. In M1 the posting rules must reproduce these lines exactly (fixture-driven tests).

## Separation of measures
| Measure | Built from | Never includes |
|---|---|---|
| Accounting profit | P&L accounts 4xxx–6xxx in the period | settlement timing, mandates |
| Cash position | 1000 (+ 1010/1020 clearing shown separately) | receivables, mandates |
| Working capital | 1100/1110/1120/1200/1210/1250 − 2000/2010/2020/2200 | cash, equity |
| Capital mandates & commitments | management ledger (`10-…`, §10) | GL balances |
