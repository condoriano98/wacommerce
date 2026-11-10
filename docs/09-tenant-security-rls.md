# 09 — Tenant Authorization & Row-Level Security

> Source of truth: `docs/00-plan.md` (canonical V3.1 specification incl. §27 normative corrections). This file is a topic view.


## 7. Tenancy, authorization and RLS (revised)

- **Transaction-local context only**: `set_config('app.org_id', …, true)` and `app.user_id` per transaction (already the
  V3.0 implementation); never session-level `SET`. Pooled connections can't leak identity.
- **Roles**: `oms_owner` (DDL/migrations only), `oms_app` (API), `oms_worker` (jobs), `oms_webhook` (receivers),
  `oms_reporting` (Metabase, read-only views). **None has BYPASSRLS.** Workers run each job as the job's org plus a
  per-org **system principal** user with explicit, minimal role assignments (e.g. `system_router` org-scoped read of
  cash/policies). Webhook tenant resolution via `SECURITY DEFINER` resolvers (`resolve_wa_account(phone_number_id)`,
  `resolve_doku_payment(ref)`, …) that return only (org_id, account_id).
- **Policies**: SELECT/UPDATE/DELETE `USING`, INSERT and UPDATE `WITH CHECK`; visibility from `role_assignments` via
  definer helpers (org, legal entity, portfolio, platform account scopes); orders visible if any item visible; customers
  via `customer_portfolios`; FORCE RLS on all tenant tables; composite tenant FKs.
- **Credentials**: no grants for `oms_app`; written via `store_credential()` (owner role check); read only by
  `oms_worker` through `read_credential()` definer that audits each read; key versioning for rotation; revocation flag.
- **Permissions** (actions) in app layer (`authz.PERMISSIONS`); **visibility** in Postgres. Every surface enforced: API,
  workers, imports, webhooks, reports. Reports: portfolio-level through OMS API; Metabase via per-org reporting login
  roles whose identity is resolved from `current_user` in `reporting_principals` (GUCs ignored for reporting roles),
  SELECT only on PII-free `reporting` views (correction 7).
- **Partial order visibility** (correction 4): order header visible only with org scope or full-line visibility;
  portfolio-limited users use `order_slices` exposing only their lines/allocations.
- Sensitive actions audited (credentials, approvals, capital, refunds, price overrides, policy overrides, ledger
  adjustments, exports of personal data).

## 27. Normative corrections (2026-11-10, rounds 2–3) — integrated into this specification

These items are part of the specification. Where an earlier section is less specific, this section governs. Execution procedure (branching, CI cadence, reporting) lives in `docs/29-execution-playbook.md`.

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
