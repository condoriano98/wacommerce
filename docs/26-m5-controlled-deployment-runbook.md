# 26 — M5 Controlled Deployment and Recovery Runbook

Status: **DRAFT — NOT EXECUTED**

## Preconditions

1. Complete M0–M4 gates in `docs/25-m5-readiness-and-release-gates.md`.
2. Obtain documented authorization for the target environment, deployment time, operator and rollback owner.
3. Identify release commit SHA and immutable image tags; capture migration plan and risk assessment.
4. Verify tenant-safe schema privileges and that database migration credentials are unavailable to API, workers and webhooks.
5. Verify all provider adapters default to mock / disabled; no paid shipment, supplier purchase, refund, ad-budget change or funds movement is enabled implicitly.
6. Confirm backup and restore proof, monitoring dashboards, on-call contacts and status communication channel.

## Preflight checks (record actual command output)

- Run lint, type/security checks, all tests and integration contract tests in an isolated environment.
- Test migrations against a fresh PostgreSQL instance and a sanitized, restored staging copy.
- Record the current Postgres schema version, RLS policies, privileges, active integration flags and queue depth.
- Verify operational secrets exist in the hosting secret manager but are absent from source and build logs.
- Verify bank, DOKU, Biteship and messaging production actions are disabled by default.

## Controlled rollout

1. Snapshot / back up the pre-release system and verify restoration destination.
2. Perform expand-compatible migrations first; prohibit destructive migration until compatibility is proven.
3. Deploy API and worker images to staging, then run smoke tests with fake credentials and mocked external effects.
4. Verify authorization, accounting postings, portfolio isolation, reconciliation, outbox and Chatwoot echo suppression.
5. Only after sign-off, deploy to production with outbound side effects disabled.
6. Confirm internal endpoints, metrics, queue processing, read-only dashboards and journal invariants.
7. Enable each live provider independently only after credential/sandbox proof, business authorization, idempotency checks and documented rollback path.
8. Capture release evidence and open issues; leave unverified integrations in a disabled state.

## Emergency stop

- Disable external side-effect feature flags and pause worker tasks that could call providers.
- Preserve webhook ingest when safe so events can be replayed after remediation.
- Do not delete failed events, mutate ledger entries or replay irreversible provider actions blindly.
- Escalate discrepancies, isolate affected tenants/accounts, and record the incident timeline.

## Rollback and recovery

- Prefer reverting application images before destructive database rollback.
- For incompatible schema, use a reviewed forward fix or isolated restore procedure; never overwrite active financial facts without reconciliation and explicit approval.
- Restore backups only in isolation first; measure recovered write timestamp vs failure timestamp for RPO and time to working service for RTO.
- Reconcile outbox, provider idempotency keys, webhooks, bank/provider settlement records and ledger balances before reopening side-effecting workers.

## Required evidence packet

Release SHA, reviewer names, test run links, contract test results, backup and restore timestamps, measured RPO/RTO, dashboard links, integration feature-flag export (redacted), secrets-scan status, critical vulnerability remediation, migration log, rollback drill log, and final GO/NO-GO approval.

**Never label this runbook executed or M5 complete without those artifacts.**


## Runbook: marketplace token rotation uncertain (reauthorization required)

Trigger: `external_uncertain` case for operation `token_refresh`, capability blocker `reauthorization_required`.
Meaning: the provider may have consumed the single-use refresh token, but the new tokens were not stored. The OMS
will **not** retry with the old token.
1. Do not edit credentials in the database. Keep marketplace sync for the account paused (it is `not_authorized`).
2. In the provider's seller/partner console, reauthorize the shop for the OMS app (new authorization code).
3. Store the new tokens through `PUT /platform-accounts/{id}/credentials` (owner role; audited).
4. Remove the `reauthorization_required` blocker by resolving the case (`/external-operations/{id}/resolve`,
   `effect_exists=true`), then run one manual order sync and confirm the registry state.
5. Record the incident (time window without sync) and run an order backfill for the gap.


## Release checklist (each item needs a named approver and a timestamp; none is signed yet)
1. All CI workflows green on the release commit (OMS, Dashboard, Security).
2. External gates closed or explicitly waived in writing by the owner (docs/25 table).
3. Staging rehearsal: restore a production-like backup, run `alembic upgrade head` as the migration job (ADR-014),
   `/readyz` green, `/ops/invariants/check` clean, smoke the dashboard.
4. Secrets loaded from the Sumopod secret store; the process refuses dev defaults (`production_problems`).
5. `/metrics` scraped and alert rules loaded; inject one failure per integration in staging and see the alert.
6. Backups/PITR confirmed with a measured restore (G-OPS1); evidence appended to docs/28.
7. **First live transaction / provider-side irreversible effect** (payment link, paid booking, purchase, refund, ad
   change): explicit human approval recorded before it happens; `live_enabled` switched per account by the owner only.
8. Rollback plan rehearsed: code revert before schema; contract migrations (e.g. `deploy/migrations/contract_drop_oms_system.sql`)
   never in the same release as their expand step.
