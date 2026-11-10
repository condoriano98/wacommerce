# 12 — Payments, Settlement & Reconciliation

> Source of truth: `docs/00-plan.md` (canonical V3.1 specification incl. §27 normative corrections). This file is a topic view.


## 16. Payments (revised)

DOKU payment links (adapter after G-DOKU verification); notification = trigger → status re-check via API → capture;
manual transfer reported → attested → bank_matched (receipts never debit bank; F3a/F3b); COD per F2; partial refunds per item allocation; payment fees as
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

## Remediation amendments (2026-11-10)

- **Bank proof for payouts.** Every provider-reported payout registers a `bank_payout` reconciliation item (token,
  legal entity, source journals). `match_bank_lines` matches manual transfers first, then payouts: same legal entity,
  token in the bank reference, exact amount. Unmatched payouts age into `bank_unmatched` cases (SLA 3 days).
- **Marketplace reconciliation** compares the value received (payout + creditable withholding) with the receivable
  expected at delivery; a fee above the accrual is booked as a true-up **and** opens a variance case for review.
  Multi-portfolio orders get an org-level item; later partial deliveries raise an open expectation.
- **COD expectations** are per shipment and deduct only that shipment's carrier cost and COD fee.

## Round 2 amendments (2026-11-10)

- **Identity.** Reconciliation items are unique per (organization, kind, provider account, reference). A provider
  reference is never matched across organizations or accounts; ambiguity opens a case; unprovable legacy items are
  quarantined.
- **Manual transfers** match only bank lines of the order's legal entity, exact amount, whole-token reference, exactly
  one candidate (otherwise `payment_mismatch` case).
- **Provider payouts** match only with organization, legal entity, currency, amount and whole-token reference; several
  candidates → `bank_unmatched` case → `resolve_bank_match` with approval.
- **Marketplace payout semantics.** Lines may carry `final` (default true — Shopee escrow pays an order once). The
  receivable is cleared by at most what is outstanding; the accrued fee is released pro rata to the gross share; excess
  and duplicate payouts go to **2400 suspense** with a case. Reconciliation accumulates receipts; a final payout
  decides matched vs variance. Settlement status: none → expected → partially_settled → matched / variance →
  under_review → adjusted → closed (closed = matched or adjusted, cases resolved, payout proven by the bank).
- **Variance acceptance** requires an approved `variance_acceptance`; posts Dr 5300 / Cr 1110 for the unpaid
  receivable; audited.
- **COD remittance** is capped per shipment at the COD still expected; excess → 2400 + case; lines carry the order.
