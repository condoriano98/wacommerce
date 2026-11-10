# 25 — M5 Production Hardening: Readiness Audit and Release Gates

Status: **BLOCKED — NOT PRODUCTION READY**  
Audit date: 2026-11-10  
Baseline: `claude/intelligent-cori-6qujr0` (V3.1 planning revision).

## Scope and truthfulness

M5 is the **target state**, not permission to deploy. M0–M4 acceptance conditions remain prerequisites. This checklist is read-only regarding external providers, bank accounts, ad spend, marketplace purchases and shipments. No live actions may occur without explicit authorization.

## Observed from repository

- `docs/00-plan.md` identifies V3.1 as a planning-only revision; the latest documentation commit explicitly says no application code changed.
- `oms/app/core/db.py` still defines a `system_engine` and `system_session` using a BYPASSRLS role. `oms/app/core/config.py` still configures `oms_system`, conflicting with V3.1 D4.
- `oms/app/api/foundation.py` still returns `alloc.journal_id` after executing a portfolio reallocation; V3.1 D8 requires a management event without a cash journal.
- `oms/pyproject.toml` does not yet list Procrastinate as a dependency; V3.1 D11 requires its worker/outbox design to be implemented.
- `oms/tests/conftest.py` currently provisions `oms_system`, and tests are built around that older model. V3.1 baseline and regression changes are outstanding.
- `docs/18-deployment-recovery.md` defines recovery targets and controls but does not supply drill evidence.
- The CI workflow added on this readiness branch executes lint and existing Postgres-backed tests; **workflow results have not been verified**. Passing legacy tests does **not** certify V3.1.

## Sequential gates

| Gate | Exit evidence | State |
|---|---|---|
| M0 architecture/provider verification | DOKU/Biteship/Chatwoot/procrastinate contracts or explicit owned waivers; accountant/tax decisions documented; journal fixtures automated | Open |
| M1 financially correct core | V3.1 migrations and code, no runtime BYPASSRLS, quantity-aware split fulfillment, accurate COD/prepay journals, reconciliation and capital invariants, negative tests | Blocked |
| M2 commerce vertical slice | Mocked, replay-safe Meta–OMS–Chatwoot and n8n flow; authorized OMS commands; payment/shipment simulation; loop/duplicate tests | Blocked |
| M3 FP&A | CM1–3 tie to ledger, closed-period immutability, cash forecasts, Rp100M scenario, stress and liquidity alerts | Blocked |
| M4 platform expansion | Capability-state contracts, CSV fallback, integration tests against sandbox/recorded responses, no false live-ready labels | Blocked |
| M5 security | RLS fuzzing, role and object-level auth, secret/log scans, dependency scan, threat model and remediation | Not started |
| M5 reliability | Worker-kill/restart, transactional outbox retries, duplicate and out-of-order webhooks, poison messages, circuit breakers, reconciliation ageouts | Not started |
| M5 observability | Correlation IDs and dashboards for API, workers, outbox lag, backlog, provider errors, reconcile exceptions; alerts exercised | Not started |
| M5 recovery | Backup inventory, verified restore to isolated environment, measured RPO <=15 min and RTO <=4 h or documented unmet target | Not started |
| M5 release control | Signed-off runbook, staged deployment, manual irreversible-action approvals, rollback and incident contacts | Not started |

## External and governance gates

| ID | Dependency | Required evidence |
|---|---|---|
| G-DOKU | Signature and check-status API | Official product-specific contract, sandbox webhook tests |
| G-BS1–3 | COD remittance, webhook verification and billing | Provider docs / sandbox evidence; no unverified financial automation |
| G-CW1 | Chatwoot API-channel echo semantics | Integration test for origin mappings and suppression |
| G-PROC | Procrastinate compatibility | Locked version + worker/lease/retry test |
| G-ACC1 / G-TAX1 | Accounting standard, VAT / PKP, marketplace withholding | Accountant sign-off |
| G-OPS1 / G-OPS2 | Sumopod PITR and role privileges | Backup restore proof and DB permission test |
| G-GOV1 | Founder-only approval conflict | Separation-of-duties solution approved |
| G-MB1 | Metabase tenant-safe reporting | Test of permitted reporting isolation |

## Production hardening acceptance tests

1. Verify every transaction respects tenant and portfolio RLS under all runtime roles.
2. Prove no cross-tenant foreign key can reference other organizations.
3. Confirm money/stock/quantity invariants and immutable financial history on current migrations.
4. Kill workers during side effects; retry without duplicate ledger, message, purchase or shipment events.
5. Simulate webhooks twice, out of order, and with invalid signatures; verify safe handling.
6. Demonstrate reconciliation mismatches age into assigned exceptions without being silently netted away.
7. Test secrets rotation and confirm no secrets in API responses, logs, tests or artifacts.
8. Prove a restored environment reaches target RPO/RTO using measured, timestamped evidence.
9. Verify observability detects at least one injected failure per critical integration and queue.
10. Obtain human approval before any first live transaction or provider-side irreversible effect.

## Release decision record

- Decision: **NO-GO for production**.
- Justification: V3.1 documentation has not yet been implemented or validated; provider and recovery gates remain open.
- Next engineering work: M0 gate closure followed by M1 schema/application corrections, while building CI and safety checks in parallel.
- Do not sign off M5 until every required evidence link is attached and independently reviewed.
