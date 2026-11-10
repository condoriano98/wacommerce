# 29 — Execution Playbook: V3.1 through M5

> Procedural document. It references `docs/00-plan.md` (canonical specification) and never redefines architecture.

## Context
The user has authorised implementation (not planning) of V3.1 from the current repo state through M5, with
gates M0–M4 passing first. External live actions remain forbidden; integrations default to mocked/disabled; no
production deploy. State inspected (read-only): branch `claude/intelligent-cori-6qujr0` @ `a0aa78e` (V3.0 code + V3.1
docs, 21 tests), draft PR #1 (`chore/m5-readiness-gates-20261110` @ `fe25b4a`, base = our branch) adding
`.github/workflows/oms-ci.yml` (Postgres 16 service, ruff + pytest), `docs/25-m5-readiness-and-release-gates.md`
(NO-GO, gate table, 10 hardening tests) and `docs/26-m5-controlled-deployment-runbook.md`. Environment: Python 3.13 +
uv, local PostgreSQL 16 (no Docker daemon → testcontainers only in CI), Node 22, outbound package registries reachable.

## Ground rules
- Work on `impl/v31` (branched from `claude/intelligent-cori-6qujr0`, PR #1 merged with SHAs preserved); one draft PR
  `impl/v31 → claude/intelligent-cori-6qujr0`; push after each milestone; CI must be green before a milestone closes.
- Commit per milestone with SHA + test counts recorded in a new `docs/27-implementation-status.md` (status labels).
- Never label mocked integrations or unexecuted drills as verified; gates stay open with evidence fields.

## Mandatory corrections (user, 2026-11-10) — override anything below that conflicts

1. **Git/CI**: dedicated branch `impl/v31` created from `claude/intelligent-cori-6qujr0`; PR #1 integrated by **merging**
   `origin/chore/m5-readiness-gates-20261110` (SHAs preserved → no conflicting PR; PR #1 shows as contained). One draft PR
   `impl/v31 → claude/intelligent-cori-6qujr0` whose body states it includes PR #1. `oms-ci.yml` triggers on
   `pull_request` **and** `push` to `impl/**` and `claude/**`; milestone done only when the GitHub check run for the
   milestone commit is green (read via GitHub MCP `get_check_runs`/actions) **and** local run is green.
2. **Migration safety**: inventory done read-only — revisions `0001_schema`, `0002_security`, `0003_reference_data`;
   databases only local `oms` (dev) and `oms_test` (recreated per test run); remote branches touching migrations: none
   (`chore/m5…` = CI/docs only); no deployment config in repo. Sumopod DB migration status is **unknown → not proven
   disposable**. Decision: **no history replacement** — V3.1 schema is delivered as **forward migrations 0004+**
   (expand → backfill → contract), each with a downgrade where safe. Re-verify the local inventory (row counts,
   `alembic_version`) when Postgres is restarted and record it in `docs/27`. ADR-013 rewritten: "forward-only".
   **Staged migration deployment** (`docs/18`, ADR-014): (1) pre-migration backup + restore check; (2) rehearse on a
   restored staging copy; (3) apply *expand* migration as `oms_owner` via a one-off job with `lock_timeout=5s`,
   `statement_timeout`, idempotent DDL — old code keeps working; (4) deploy new code (reads/writes both shapes where
   needed); (5) run *backfill* as a batched, resumable, idempotent job with invariant checks (journals balance,
   quantities, tenant FK validity); (6) only in a later release, after verification, apply *contract* migration
   (drop superseded columns/constraints). Each step has a written rollback (code revert before schema; contract steps
   never run in the same release as expand). Production execution requires explicit approval.
3. **Payment accounting (resolved)**: manual payments `reported → attested → bank_matched`. **Receipts and attestations
   create no ledger entry and no asset** (an unverified claim is not a resource). Postings: bank-statement match before
   delivery → Dr 1000 / Cr 2200 (contract liability); default release policy = fulfil only after `bank_matched`.
   Optional per-portfolio policy "release on attestation" (approval-backed exposure limit): if goods are delivered
   before the bank match, revenue is recognised against **1105 Customer receivable – transfer pending verification**
   (Dr 1105 / Cr 4000/4100); later bank match → Dr 1000 / Cr 1105; unmatched beyond SLA → `bank_unmatched` case,
   allowance Dr 5700 Expected credit losses / Cr 1109 Allowance, write-off only with approval. Exposure on 1105 is
   reported per portfolio. Fixture F3 split into F3a (bank match before delivery) and F3b (release on attestation).
   Accounts added to the CoA: **1105**, **1109 Allowance for expected credit losses** (contra-asset), **5700 Expected
   credit losses (net of recoveries)**. Credit-loss fixtures **F13**: (a) allowance Dr5700/Cr1109; (b) allowance release
   when the transfer is later matched: Dr1109/Cr5700 then Dr1000/Cr1105; (c) approved write-off Dr1109/Cr1105;
   (d) recovery after write-off: Dr1000/Cr5700 (recovery credited to the same expense line, shown separately in reports).

**Additional corrections (user, 2026-11-10, round 3)**
- **DB-role cutover is backward-compatible** (expand/contract, ADR-014): 0005 *creates* `oms_worker`, `oms_webhook`,
  `oms_reporting_owner`, reporting roles, resolvers and new policies **alongside** the existing `oms_app`/`oms_system`;
  code switches worker/webhook to the new roles behind a config flag (`OMS_DB_ROLE_MODE=legacy|v31`); after both CI
  modes and isolation tests pass, a later contract migration revokes BYPASSRLS from and drops `oms_system`. No step
  removes a role still used by deployed code; credentials for new login roles come from the secret store.
- **Acceptance-test ownership** (single owner per test; split where a test spans milestones): M1 → #1, #2, #3, #4, #7,
  #8, #9, #11a (DOKU/COD/bank settlement ageing), #13a (ledger close + adjustments), #16, #17, F1–F13 service tests,
  RLS/order-slice/reporting-role isolation; M2 → #5, #6, #14, #15, E2E vertical slice; M3 → #12, #13b (FP&A snapshot
  immutability), #18, S1–S9; M4 → #10, #11b (marketplace settlement ageing), adapter contract tests; M5 → PR #1
  hardening tests 1–10 + DR drill. §19/§20 updated to match.
- **Canonical spec vs execution playbook**: `docs/00-plan.md` = canonical V3.1 architecture specification only
  (§A–§26 with all corrections integrated); `docs/29-execution-playbook.md` = this execution section (branching, CI,
  milestone steps, reporting, migration/role cutover procedures). The playbook references the spec and never redefines
  architecture; contradictions are resolved in the spec only.
4. **Partial visibility**: `orders` header (totals, shipping address, payment data) visible only to org-scope roles or
   users who can see **every** line of the order; portfolio-limited users read `order_slices` (security-barrier view /
   definer function) exposing order number, derived status and **only their lines and line-level allocations**; customer
   PII visible only via `customer_portfolios`; addresses only through the shipment/fulfilment groups they own. Negative
   tests assert no header totals, other lines or other portfolios' allocations leak.
5. **Procurement concurrency**: `purchase_lines` insert/update/delete maintains `order_items.qty_procured` via trigger
   under `SELECT … FOR UPDATE` of the item row; `CHECK qty_procured <= qty_allocated_dropship`; tests use **separate DB
   connections in threads with a barrier** (two concurrent purchases exceeding qty → exactly one commits).
6. **Jobs**: Procrastinate worker with heartbeat; periodic `retry_stalled_jobs` task (stalled = heartbeat older than
   threshold); retries with exponential backoff; terminal failures → `dead_letters` table + review/requeue API + metric.
   Every external side effect is an `external_operations` row (provider, operation, idempotency key, status
   `prepared → sent → confirmed | uncertain | failed`) written and committed **before** the call. **Provider-specific
   idempotency policies** (ADR-015, `docs/04` table; provider facts marked unverified until gates close):
   | Operation | Key | Uncertain outcome (timeout/crash after send) | Retry rule |
   |---|---|---|---|
   | DOKU create payment link | `invoice_number` = payment id | check status by invoice number (G-DOKU) | reuse same invoice; never new number |
   | DOKU notification | `(provider, notification_id)` | n/a (inbound) | dedupe |
   | Biteship create order (paid booking) | `reference_id` = shipment id | lookup by reference **if supported (G-BS4)**, else mark `uncertain` → reconciliation task + human check in Biteship dashboard | **no automatic re-create** |
   | Meta WhatsApp send | OMS message id (no provider idempotency) | wait for status webhook (wamid) up to 15 min; else `uncertain` → human decides | **no automatic resend** of uncertain sends |
   | Meta CAPI event | `event_id` (Meta doesn't dedupe BM) | `uncertain`, no resend; logged | resend only on pre-send/connection failures |
   | Chatwoot mirror post | `echo_token` in message attributes | search conversation for token | re-post only if absent |
   | Marketplace writes (e.g. ship/arrange pickup) | platform order id + action | read order status first | retry only if status unchanged |
   | Marketplace/ads reads, imports | window + cursor / file fingerprint | safe | idempotent upserts |
   | Refund payout / supplier purchase / ad budget | n/a — **human-executed outside OMS** | n/a | OMS records evidence only |
   Test: kill the worker after the mock provider accepted but before commit → restart → stalled recovery → policy
   lookup finds the effect → exactly one external effect; uncertain-without-lookup cases end in `uncertain` + case.
7. **Metabase OSS**: no sandboxing/impersonation/auth-embedding assumed. Portfolio-level reporting is served by the OMS
   API under RLS (dashboard pages). Metabase only for org-level users. **Database-backed tenant identity** (resolved):
   GUC-based identity is spoofable from Metabase native SQL (`set_config`), so reporting identity comes from the
   **login role**. `current_user` is unusable for this: inside SECURITY DEFINER helpers it is the function owner, and
   inside definer views it is the view owner. Identity therefore uses **`session_user`**, which `SET ROLE` cannot change
   and `SET SESSION AUTHORIZATION` requires superuser. Design: table `reporting_principals(db_role UNIQUE,
   organization_id, principal_user_id)`; `app_org()`/`app_user()` first check `session_user` against
   `reporting_principals` — if it is a reporting login, return the mapped ids and **ignore GUCs**; otherwise return the
   transaction GUCs (honoured only for server-side roles `oms_app`/`oms_worker`/`oms_webhook`). Reporting views live in
   schema `reporting`, owned by `oms_reporting_owner` (NOLOGIN, not a table owner, no BYPASSRLS, SELECT on needed base
   tables) and created with `security_invoker = false`, so base-table RLS evaluates `app_org()` → `session_user` mapping.
   Per-org login roles `rpt_<org_short>` (NOINHERIT, member of nothing privileged, `CONNECTION LIMIT`,
   `statement_timeout`) get only `USAGE` on `reporting` + `SELECT` on PII-free aggregate views. Metabase native-query
   permission off for non-admin groups. Signed guest embeds with locked params = presentation only.
   **G-MB1 stays unverified** until PostgreSQL tests pass: reporting login cannot see another org's rows via views; `SELECT
   set_config('app.org_id', …)` / `SET app.user_id` changes nothing; `SET ROLE` to any other role fails; base tables
   and definer data functions are not accessible; views return only the mapped org.
8. **FP&A simulation**: deterministic, ledger-reconciled scenarios first. Nine stress scenarios explicit: S1 CPA +30/+60%,
   S2 CVR −25%, S3 returns ×2, S4 COD delay +7/+14d, S5 supplier cost +10/+20%, S6 sell-through −40%, S7 ROAS −35%,
   S8 one channel outage 7d, **S9 marketplace settlement delay +14d with platform fee +2pp** (combined downside kept as a
   preset combining S1+S2+S4+S5, not counted as S9). Monte Carlo = Planned, behind a flag, enabled only with documented
   and validated distributions.
9. **DR**: local PG16 restore drill = **preliminary evidence** only. M5 sign-off requires Sumopod backup/PITR
   confirmation and a measured restore against RPO 15 min / RTO 4 h; until then the gate stays open.
10. **Financial acceptance**: fixture tests execute the **real OMS services against PostgreSQL** (quotes→orders→
    payments→shipments→settlements→refunds) and compare resulting ledger lines (account, debit, credit) to
    `docs/fixtures/journals.yaml`; the YAML balance check is only a sanity test.
11. **Integration classification**: registry keeps two fields — `implementation_status` (`planned | mock_tested |
    sandbox_verified | production_verified`) and `access_state` (`available | unsupported | not_authorized |
    temporarily_unavailable | mocked | planned`). "Operationally ready" requires production_verified + available.
12. **Reporting**: `docs/27-implementation-status.md` per milestone: commit SHA, CI run link/result, exact
    pass/fail/skip counts + command, changed files, functional status vs external-verification status, open blockers,
    evidence links.

## M0 — prerequisites (code-impact + verification harness)
0. **Consolidate the single authoritative spec first** (before any M1 code): copy this file to `docs/00-plan.md`;
   regenerate topic docs from it; update `docs/fixtures/journals.yaml` (F3 → F3a/F3b, new accounts 1105/1109/5700);
   `docs/07-adr.md` (ADR-013 forward-only migrations, ADR-014 staged migration deployment, ADR-015 provider idempotency,
   ADR-016 DB-role reporting identity, ADR-017 deterministic-first simulation); `docs/19-milestones.md` (corrections 1,
   9, 12 gates); `docs/23-contradiction-register.md` (C15 rebaseline→forward-only, C16 no asset for unverified payments,
   C17 GUC identity for reporting, C18 Monte Carlo deferred, C19 S9 defined, C20 header visibility); `docs/25` merged
   from PR #1 and aligned. Grep check: no remaining "rebaseline", "1015", "sandboxing", unqualified "Monte Carlo".
1. `docs/27-implementation-status.md`: code-impact assessment of V3.0 vs V3.1 (BYPASSRLS `oms_system` in
   `core/db.py`/`config.py`/conftest; cash journals in `modules/org.execute_capital_reallocation`; single
   `orders.fulfillment_route` + router MANUAL_REVIEW on mixed; `UNIQUE(order_item_id)` / `UNIQUE(procurement_task_id)`;
   COD remittance wording; `payments.verify_manual_transfer`→paid; WIP Chatwoot proxy; custom dispatcher without
   Procrastinate) → each mapped to the M1/M2 change that replaces it.
2. `tests/test_journal_fixtures.py`: loads `docs/fixtures/journals.yaml`, asserts each journal balances and scenario
   nets (contract liabilities, GIT, receivables clear); later reused by M1 posting-rule tests.
3. Gate register in `docs/21-risks-blockers.md` stays open for G-DOKU, G-BS1–3, G-CW1 (attempt doc verification via
   web research where reachable; record source + date, else "unverified"), G-PROC (verified locally by test), G-ACC1/
   G-TAX1/G-GOV1 (business decisions), G-OPS1/2, G-MB1.

## M1 — financially correct OMS core (forward migrations — see correction 2)
- **Migrations**: keep 0001–0003; add `0004_v31_entities` (new tables/columns, expand), `0005_v31_security` (roles
  without BYPASSRLS, definer resolvers, rewritten policies incl. order slices, procurement trigger),
  `0006_v31_coa` (account renumber/backfill), `0007_v31_contract` (drop superseded columns/constraints after code
  switch). Composite tenant FKs via `UNIQUE(organization_id, id)` on parents.
- **Models** (`oms/app/models/*`): add `legal_entities`, `bank_accounts`, `identity_domains`, `capital_mandates`,
  `capital_allocation_events`, `capital_commitments`, `commitment_consumptions`, `integration_capabilities`,
  `fulfillment_groups`, `fulfillment_group_items`, `shipment_items`, `purchase_tasks`, `purchase_lines`,
  `order_item_allocations`, `order_item_events`, `payment_events`, `bank_statement_lines`, `reconciliation_runs`,
  `reconciliation_items`, `accounting_periods`, `stock_counts(_lines)`, `wa_events`, `message_mappings`,
  `message_templates`, `media_objects`; order_items quantity columns + CHECKs; inventory `in_transit`; CoA renumber
  (2200 contract liabilities, 1250 GIT, 1020 carrier balance, 1400 prepaid tax, 1900/2900 intercompany…).
- **Security**: roles `oms_owner` (DDL), `oms_app`, `oms_worker`, `oms_webhook`, `oms_reporting` — none BYPASSRLS;
  per-org system principals for worker jobs; definer resolvers (`resolve_wa_account`, `resolve_payment_ref`,
  `resolve_shipment_ref`, `resolve_marketplace_account`); `read_credential` for worker only; WITH CHECK on all policies.
  `core/db.py`: `tenant_session(org, user)` only; `system_session` removed; `worker_session(org)` runs as system principal.
- **Services**: rewrite `orders` (items qty, groups, derived status), `routing` (per group, mixed allowed),
  `fulfillment` (GIT, per-shipment-line recognition), new `procurement` (tasks/lines/qty locks), `payments`
  (captured/reported/attested/bank_matched), `settlements`, `reconciliation` (items, ageing, exposure, cases),
  `capital` (mandates/commitments; no GL), `finance/posting` v2 reproducing F1–F12, `finance/periods` (close,
  adjustments), `inventory` (GIT, counts, concurrency). Reuse: `post_journal`, `journal_exists`, approvals, audit,
  exceptions playbooks, quotes policy core.
- **Outbox + Procrastinate** (`core/outbox.py`, `app/worker.py`): outbox table unchanged semantics; Procrastinate
  relay task (`relay_outbox`, periodic) dispatching handlers, retries/backoff, dead-letter status; fallback dispatcher
  kept behind same interface for tests.
- **API**: `app/api/{foundation,commerce,finance,capital,reconciliation}.py` with consistent error model.
- **Tests (gate)**: acceptance #1,2,3,4,7,8,9,11,13,16,17 + F1–F12 fixture-driven posting tests + invariants checker +
  RLS negative suite (app/worker/webhook/reporting roles, raw SQL, cross-tenant FK attempts) + concurrency tests
  (threads: oversell, over-procurement, duplicate purchase).

## M2 — WhatsApp commerce vertical slice
- `modules/messaging.py` rework: receiver verifies signature → `wa_events` + outbox in one commit → 200; `wa.process`
  job; replay endpoint; message mappings, echo tokens; templates registry; media to local object store adapter
  (`integrations/storage.py`, S3-compatible interface); window enforcement; handoff.
- `/webhooks/chatwoot` (API-channel callbacks, signature, echo suppression) → send commands.
- `modules/agent.py` + `/agent/*` API: authority matrix, reply validator, acceptance evidence, limits, kill switch,
  execution logs (savepoint for denials), prompt-injection fixtures.
- Adapters (mock + sandbox-flagged live, contract tests with respx): `integrations/{doku,biteship}.py`;
  `CarrierAdapter`/`PaymentProvider` protocols; CAPI sender (`conversions` eligibility + mock Meta).
- n8n: `n8n/workflows/ai-sales-admin.json`, `error-workflow.json` (marked not externally verified).
- Procurement copilot API + dashboard pages.
- **Gate**: E2E test inquiry → quote → acceptance → DOKU mock capture → routing (mixed groups) → Biteship mock
  shipment events → delivery → disbursement/COD remittance → reconciliation → ledger, run twice with duplicated/
  out-of-order events: no duplicate sends/bookings/journals; acceptance #5,6,14,15.

## M3 — FP&A and capital intelligence
- `modules/fpa/`: SQL views (`v_order_line_economics`, `v_cm_waterfall`, L1/L2/L3), ledger tie-out function,
  budgets/forecasts, 13-week cash forecast, CCC, capital utilisation & recovery horizon, risk alerts, bulk-buy
  evaluator, deterministic ledger-reconciled simulator with stress scenarios S1–S9 (Monte Carlo Planned, flag-gated
  until distributions are documented and validated), Rp100M seed (A/B/C),
  snapshots + period close immutability.
- Dashboard `dashboard/` (Next.js + Refine + shadcn/ui): overview, portfolios, orders, procurement copilot,
  approvals, reconciliation exceptions, FP&A waterfall, capital, simulator comparison, integration health; Metabase
  `reporting` schema views + per-org reporting roles (correction 7); portfolio reporting via OMS API pages.
- **Gate**: waterfall totals tie to ledger per period; closed period immutable; stress alerts fire (#12,13,18);
  Playwright smoke against dashboard with API mocks.

## M4 — marketplace & advertising expansion
- `integrations/{shopee,tiktokshop,lazada,meta_ads,google_ads,tiktok_ads,marketplace_ads}.py`: protocols, token
  lifecycle (Shopee single-use refresh atomic rotation, Lazada non-refreshable re-auth, TikTok `shop_cipher`), rate
  limit/backoff, capability checks, order sync (windows), fee + settlement ingestion, ads insights (28-day re-pull,
  effective-dated allocations), dedupe/replay; recorded-fixture contract tests; `imports/` CSV framework (schema,
  provenance, fingerprints, dry-run, reconciliation). Capability registry states: implemented adapters `mocked`,
  live access `not_authorized`/`planned` unless credentials exist (none do).
- **Gate**: each adapter passes mock contract tests; registry never shows `available` without credentials (#10, #11).

## M5 — production hardening
- Security: Hypothesis-based RLS fuzz tests; authz matrix tests; secret rotation (key versions) + log/response
  scanning test; `pip-audit` + `npm audit` + `gitleaks` in CI; API auth hardening (token expiry, rate limit).
- Reliability: worker-kill test (subprocess killed mid-job, re-run, single effect), dead-letter review endpoint,
  circuit breaker + timeouts in `integrations/http.py`, webhook replay window, poison message handling.
- Observability: structured JSON logging with correlation IDs (middleware + job context), Prometheus metrics
  (`/metrics`: request latency, outbox lag, queue depth, integration errors, reconciliation exposure), alert rules
  file, Grafana/Metabase dashboard definitions.
- DR: `deploy/backup/` scripts (pg_basebackup + WAL archiving, pg_dump), **restore drill executed locally** on an
  isolated PG16 cluster with measured RPO/RTO recorded in `docs/28-dr-drill-evidence.md` (Sumopod PITR = G-OPS1 open).
- Deployment: `deploy/docker-compose.yml` (dev/CI), `deploy/sumopod/` container + env templates (live flags off),
  runbook updates (docs 25/26), release checklist. No production deploy.
- **Gate**: PR #1 hardening tests 1–9 automated where possible; items needing external access stay open.

## Verification (every milestone)
`cd oms && uv run ruff check . && uv run pytest -q` against local PG16 (CI: Postgres service); dashboard
`npm run build && npx playwright test`; record pass/fail/skip counts, commit SHA and changed files in
`docs/27-implementation-status.md`; push; update draft PR body.

---
