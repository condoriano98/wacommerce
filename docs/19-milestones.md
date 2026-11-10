# 19 — Implementation Milestones

> Source of truth: `docs/00-plan.md` (V3.1, approved 2026-11-10). This file is the topic view of the listed sections.


## 19. Implementation milestones (gated)

**M0 — Architecture & provider verification** (≈5 engineer-days)
Deliverables: `docs/` set (§23), ERD, ADRs (incl. ADR-003 revised: outbox + Procrastinate), accounting policy + F1–F12
fixtures as YAML, tenant security spec, provider contracts with verification dates; resolve G-DOKU, G-BS1–3, G-CW1,
G-PROC, G-MB1; accountant input request for G-ACC1/G-TAX1. Dependencies: none. Migrations: none. Tests: fixture
balance checker. Mock/live: n/a. DoD: docs reviewed, gates resolved or explicitly deferred with owner. Blockers: DOKU
docs access, accountant availability.

**M1 — Financially correct OMS core** (≈22 days)
Modules: org/legal entities/identity domains, capital (mandates/commitments), platforms + capability registry, catalog,
customers, quotes, orders + items + allocations, fulfillment groups, procurement (purchase lines), inventory (goods in
transit, counts), payments, settlements, reconciliation, ledger (periods, rules v2), approvals, audit, outbox +
Procrastinate. Migrations: **rebaseline** 0001–0003 (no deployed data exists) into a V3.1 baseline with composite FKs,
roles without BYPASSRLS. Tests: F1–F12, invariants, RLS negative suite, concurrency (oversell, over-procurement).
Mock/live: all providers mocked. Controls: approvals, audit. DoD: acceptance tests 1–9, 11, 13, 16–18 green.
Deployment readiness: staging DB migration dry-run. Blockers: G-ACC1/G-TAX1 defaults configurable.

**M2 — WhatsApp commerce vertical slice** (≈20 days)
Modules: Meta webhook bridge (`wa_events`, process job, replay), Chatwoot API inbox adapter + mapping + echo
suppression, agent API + authority matrix + reply validator, n8n AI Sales Admin workflow, DOKU sandbox adapter (after
G-DOKU), Biteship mock/sandbox, human dropship copilot UI/API, CAPI outbox sender (mock), dashboard slices (orders,
approvals, procurement, exceptions). Tests: 5, 6, 14, 15 + E2E WhatsApp → order → payment → fulfil → ledger. Mock vs
live: Meta/Chatwoot/DOKU/Biteship mocked; sandbox only with explicit flag. DoD: E2E green, no duplicate sends/loops.
Blockers: WABA + Chatwoot instance on Sumopod; DOKU sandbox keys.

**M3 — FP&A & capital intelligence** (≈15 days)
Modules: waterfall views, L1–L3, budgets/forecasts, 13-week cash forecast, capital utilisation, simulator + stress,
risk alerts, bulk-buy evaluator, close/snapshots, Metabase. Tests: 12, 13, 18 + snapshot immutability. DoD: Rp100M
scenario reproducible; alerts fire on breach.

**M4 — Marketplace & advertising expansion** (≈25 days)
Modules: Shopee, TikTok Shop (+Tokopedia & Shop), Lazada adapters (orders, fees, settlements), Meta/Google/TikTok/
marketplace ads insights, CSV import framework. Tests: 10, 11 + contract tests against recorded sandbox payloads.
Mock vs live: mocked until registry states `available`. Blockers: Shopee eligibility, TikTok ISV, Lazada app approval,
Google/TikTok app reviews.

**M5 — Production hardening** (≈15 days)
Security tests (RLS fuzzing, authz), integration contract tests, reconciliation verification on sandbox data, failure
recovery drills, observability, backup restore drill, controlled deployment runbook. DoD: RPO/RTO drill passed,
runbooks signed off. No mocked integration is labelled production-ready.
