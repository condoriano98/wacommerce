# 12 — Payments, Settlement & Reconciliation

> Source of truth: `docs/00-plan.md` (V3.1, approved 2026-11-10). This file is the topic view of the listed sections.


## 16. Payments (revised)

DOKU payment links (adapter after G-DOKU verification); notification = trigger → status re-check via API → capture;
manual transfer reported → attested → bank_matched; COD per F2; partial refunds per item allocation; payment fees as
allocations; idempotent notifications; mismatches → `payment_mismatch` case.

## 11. Reconciliation and exceptions (revised — P0.6)

Reconciliation kinds: DOKU payments ↔ notifications ↔ disbursements ↔ bank lines · manual transfers ↔ bank lines ·
Biteship COD collected ↔ remittance ↔ deductions ↔ withdrawals · marketplace orders ↔ settlement lines (fees, tax,
refunds) ↔ payouts · dropship purchases ↔ supplier evidence ↔ payment out · stock movements ↔ physical counts ·
ad insights ↔ invoices/charges ↔ portfolio allocations.
Each `reconciliation_item`: expected ref/amount, actual ref/amount, difference, evidence (media/bank line/provider
payload), responsible operator, classification (timing, fee variance, missing, duplicate, unknown), ageing bucket
(0–2, 3–7, 8–14, >14 days), financial exposure, status, approval history. Rules: receipt screenshots never set
`bank_matched`; unmatched or uncertain items stay open and auto-open exception cases at thresholds; accepting a variance
needs an approval tier; every match is traceable operational event → journal → settlement evidence.
Exception playbooks (preserved from `docs/17`, extended): delivery_failed, returned_to_sender, cod_refused,
lost_or_damaged, refund_requested, cod_settlement_mismatch, cod_not_remitted, supplier_out_of_stock,
supplier_price_drift, supplier_canceled, dropship_late, wrong_item, payment_mismatch, marketplace_settlement_mismatch,
duplicate_order, oversold_marketplace, manual_review, **bank_unmatched**, **stock_count_variance**,
**ad_spend_unallocated**. Cases carry exposure + ageing; SLA breaches alert.
