# 22 — Implementation Backlog (P0/P1/P2)

> Source of truth: `docs/00-plan.md` (V3.1, approved 2026-11-10). This file is the topic view of the listed sections.


## 22. Implementation backlog

| Pri | Item | Module | Depends | Effort (d) | Test gate | External blocker |
|---|---|---|---|---|---|---|
| P0 | V3.1 baseline migration (legal entities, identity domains, composite FKs, roles w/o BYPASSRLS, resolvers) | migrations, core/db | M0 | 4 | RLS negative suite (#7) | Sumopod role privileges (G-OPS2) |
| P0 | Accounting rules v2 + periods + CoA renumber + F1–F12 | finance | baseline | 4 | #3 #4 #13 + invariants | G-ACC1/G-TAX1 defaults |
| P0 | Order items qty model, allocations, fulfillment groups, derived status | orders, fulfillment | baseline | 5 | #1 #2 | — |
| P0 | Procurement purchase lines + qty invariant + copilot API | procurement | orders | 3 | #8 | — |
| P0 | Capital mandates/commitments; remove cash journals from reallocation | capital | baseline | 2 | #16 | — |
| P0 | Payments state machine (attested/bank_matched) + bank statement import | payments, reconciliation | finance | 3 | #11 | bank CSV formats (BCA/Mandiri) G-BANK1 |
| P0 | Reconciliation module + exception linkage (exposure, ageing) | reconciliation, exceptions | payments | 4 | #11 #17 | — |
| P0 | WhatsApp bridge: wa_events persist-then-ack, process job, replay, mappings, echo suppression | messaging | outbox | 4 | #5 #15 | G-CW1 |
| P0 | Inventory GIT + counts + concurrency tests | inventory | orders | 2 | #9 | — |
| P1 | Procrastinate integration behind outbox interface | core/outbox, worker | baseline | 2 | #6 | G-PROC |
| P1 | Agent API + authority matrix + reply validator + injection fixtures | agent | quotes, messaging | 4 | #14 | — |
| P1 | n8n AI Sales Admin workflow + error workflow | n8n | agent | 3 | E2E | n8n MCP optional |
| P1 | DOKU adapter (sandbox) | integrations/doku | G-DOKU | 2 | contract tests | DOKU sandbox keys |
| P1 | Biteship adapter (sandbox) + COD remittance reconciliation | integrations/biteship | G-BS1–3 | 3 | F2 fixture | Biteship sandbox key |
| P1 | CAPI sender + insights sync (mock/CSV) + effective-dated allocations | conversions, marketing | finance | 4 | #12 | Meta app/system user |
| P1 | Capability registry + health UI | platforms, dashboard | baseline | 2 | #10 | — |
| P1 | FP&A waterfall, L1–L3, close/snapshots, cash forecast | fpa | finance | 6 | #13 | — |
| P1 | Simulator + stress tests + alerts + bulk-buy evaluator | fpa | waterfall | 5 | #18 | — |
| P1 | Dashboard (Next.js + Refine) 10 sections | dashboard | APIs | 8 | Playwright | — |
| P1 | Ops: logging/correlation, health, DLQ review, backups doc | deploy | — | 4 | drills | G-OPS1 |
| P2 | Shopee adapter | integrations/shopee | M1 | 5 | contract | shop eligibility |
| P2 | TikTok Shop / Tokopedia & Shop adapter | integrations/tiktokshop | M1 | 6 | contract | ISV |
| P2 | Lazada adapter | integrations/lazada | M1 | 4 | contract | app approval |
| P2 | Google Ads + Data Manager conversions | integrations/google_ads | M1 | 4 | contract | access level |
| P2 | TikTok Ads + marketplace ads | integrations/* | M1 | 4 | contract | app review |
| P2 | Landing page + Pixel/website CAPI | landing | conversions | 3 | dedupe test | domain |
| P2 | Metabase per-portfolio access | reporting | G-MB1 | 2 | #7 (reporting) | — |
