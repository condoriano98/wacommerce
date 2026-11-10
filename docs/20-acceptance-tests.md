# 20 — Acceptance-Test Matrix & Invariants

> Source of truth: `docs/00-plan.md` (canonical V3.1 specification incl. §27 normative corrections). This file is a topic view.


## 20. Acceptance-test matrix and invariants

| # | Test | Milestone |
|---|---|---|
| 1 | Multi-portfolio order with split fulfilment (owned + 2 suppliers) | M1 |
| 2 | Partial refund and partial return reconcile (stock, revenue, refunds) | M1 |
| 3 | COD revenue recognised at delivery before remittance; remittance clears receivable | M1 |
| 4 | DOKU prepayment recorded as contract liability until delivery | M1 |
| 5 | Duplicate and out-of-order webhooks (Meta, DOKU, Biteship, marketplaces) | M2 |
| 6 | Failed and retried jobs apply effects exactly once (worker killed mid-job) | M2 |
| 7 | Cross-organization and cross-portfolio access attempts (app, worker, webhook, reporting roles) | M1 |
| 8 | Multiple supplier purchases within one order; duplicate purchase attempts rejected | M1 |
| 9 | Overselling prevented under concurrent reservations across channels | M1 |
| 10 | Missing marketplace credentials → capability `not_authorized`, no calls | M4 |
| 11a | DOKU/COD/bank settlement failure or delay ages into exception with exposure | M1 |
| 11b | Marketplace settlement failure or delay ages into exception with exposure | M4 |
| 12 | Ad cost allocation change effective-dated; closed periods unchanged | M3 |
| 13a | Ledger period close and adjusting entries | M1 |
| 13b | FP&A snapshot immutability after close | M3 |
| 19 | F1–F13 executed through OMS services against PostgreSQL match fixture lines | M1 |
| 20 | Order-slice and reporting-role isolation (session_user identity, set_config/SET ROLE spoof attempts) | M1 |
| 14 | AI tool authorization failures logged; prompt-injection fixtures | M2 |
| 15 | WhatsApp outbound loop prevention (Chatwoot echo) | M2 |
| 16 | Capital reallocation creates no cash journal | M1 |
| 17 | Consolidated reconciliation (eliminations net to zero) | M1 |
| 18 | Stress test breaches liquidity reserve → alert | M3 |
| + | 3 demo portfolios (A FMCG FB→WA, Shopee dropship, Biteship option; B IG→WA, Tokopedia supplier, own budget; C Shopee + TikTok Shop native, shared SKUs) and the 10-step mocked scenario | M2–M3 |
Invariants checked after every test: journals balance; stock subledger = GL inventory + GIT; Σ order-item quantity
stages consistent; procured ≤ allocated; payment allocations = payment amount; external events deduplicated; contract
liabilities = undelivered prepaid value.
