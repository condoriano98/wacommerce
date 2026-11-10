# 07 — Architecture Decision Records

| ADR | Decision | Status | Rationale | Consequences |
|---|---|---|---|---|
| ADR-001 | Modular monolith (FastAPI) | Accepted | One team, transactional consistency across orders/ledger | Module boundaries enforced by package layout and tests |
| ADR-002 | Sync SQLAlchemy 2 + psycopg 3; client-side UUIDs; explicit flush order (no ORM relationships) | Accepted | Simple transactions with RLS; inserts never need RETURNING visibility | Services flush parents before children |
| ADR-003 (rev. V3.1) | **Transactional outbox + Procrastinate executor** | Accepted (supersedes V3.0 "drop Procrastinate") | Outbox gives atomicity with business writes; Procrastinate gives leases, retries, periodic jobs, locks, dead letters on Postgres | Gate G-PROC; fallback keeps SKIP LOCKED dispatcher behind same interface |
| ADR-004 | Mock-first adapters; live requires credentials + capability `available` + `live_enabled` + approval for irreversible calls | Accepted | No accidental production effects | Capability registry is the switchboard |
| ADR-005 | Human-operated marketplace procurement (copilot) | Accepted | Shopee/Tokopedia ToS forbid bots | OMS enforces ceilings, qty, approvals, evidence |
| ADR-006 | OMS is sole Meta WhatsApp owner; Chatwoot API-channel inbox | Accepted (supersedes proxy design) | No callback contention; raw referral/BSUID capture; policy enforcement | OMS implements sending, templates, window, media |
| ADR-007 | Postgres RLS without BYPASSRLS runtime roles; definer resolvers; composite tenant FKs | Accepted | Isolation enforced by the database for every surface | Worker uses per-org system principals |
| ADR-008 | Accrual accounting with control-transfer revenue per shipment line; goods-in-transit; contract liabilities | Accepted (G-ACC1 accountant confirmation) | Profit not distorted by cash timing | Posting rules versioned; periods close |
| ADR-009 | Capital mandates/commitments as a management ledger separate from GL | Accepted | Budget moves are not bank moves | Intercompany journals only for real transfers |
| ADR-010 | Fulfillment groups and item-level quantity state machines; derived order status | Accepted | Real orders mix sources, suppliers, portfolios | Router decides per group |
| ADR-011 | Integration Capability Registry | Accepted | Separate "adapter exists" from "access approved" | UI shows state per capability |
| ADR-012 | Reconciliation as a first-class module with bank evidence | Accepted | Receipts ≠ settlement | Payment states reported → attested → bank_matched |
| ADR-013 (rev.) | **Forward-only migrations 0004+**; history 0001–0003 preserved | Accepted (supersedes "rebaseline") | Sumopod databases not proven disposable | Expand/backfill/contract per ADR-014 |
| ADR-014 | Staged migration & DB-role cutover: backup → staging rehearsal → expand (lock/statement timeouts) → code deploy behind `OMS_DB_ROLE_MODE` → batched idempotent backfill with invariant checks → contract in a later release | Accepted | Zero-downtime, reversible releases | Contract steps never in the same release as expand; production needs approval |
| ADR-015 | Provider-specific idempotency via `external_operations` (prepared→sent→confirmed/uncertain/failed) and per-provider lookup/retry policy | Accepted | Uncertain provider responses must not duplicate money/shipping effects | Uncertain outcomes become cases, never blind retries |
| ADR-016 | Reporting identity from `session_user` via `reporting_principals`; GUCs ignored for reporting roles; definer views owned by non-bypass `oms_reporting_owner` | Accepted; **G-MB1 unverified** until isolation tests pass | Metabase OSS has no row-level security and allows native SQL | Portfolio-level reporting served by the OMS API |
| ADR-017 | Deterministic, ledger-reconciled simulation first; Monte Carlo flag-gated until distributions validated | Accepted | Explainable, reproducible decisions | S1–S9 defined in spec §27.8 |
