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

## Sequential gates (updated 2026-11-10 with evidence; see docs/27 for SHAs and CI runs)

| Gate | Exit evidence | State |
|---|---|---|
| M0 architecture/provider verification | Fixtures automated; provider facts documentation-verified (not sandbox) | **Met (docs)** — G-DOKU/G-BS/G-CW1 sandbox verification open |
| M1 financially correct core | Migrations 0004–0008, no runtime BYPASSRLS, F1–F13 via services, acceptance #1–4, 7–9, 11a, 13a, 16, 17, 19, 20 | **Met** (CI green) |
| M2 commerce vertical slice | Mocked Meta–OMS–Chatwoot, agent API, #5, #6, #14, #15, E2E | **Met** (mocked) |
| M3 FP&A | Waterfall tie-out, #12, #13b, S1–S9, #18, dashboard | **Met** |
| M4 platform expansion | Capability states, CSV fallback, #10, #11b, review P0/P1 fixed | **Met (mock_tested)** — no sandbox/recorded responses (access blocked) |
| M5 security | RLS fuzzing, FK catalogue, route matrix, secret hygiene, key rotation, threat model, CI scans | **Met in code** — gitleaks result pending first CI run; residual risks in docs/30 |
| M5 reliability | Worker kill (#6), outbox retries/dead letters, duplicate/out-of-order webhooks, replay window, circuit breaker, invariant checks | **Met** (tests) |
| M5 observability | Correlation IDs, JSON logs, /metrics, alert rules, injected failures visible | **Met in code** — no Prometheus/Grafana deployed; alerts not exercised end-to-end |
| M5 recovery | Local isolated restore drill with timestamps (docs/28) | **Preliminary only** — Sumopod PITR + production-sized RTO open (G-OPS1); RPO 15 min not met by logical backups |
| M5 release control | Runbooks (docs/26), release checklist, manual approval before first live transaction | **Documented** — not signed off; no deployment performed |

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

- Decision: **NO-GO for production** (re-confirmed 2026-11-10 after M0–M5 implementation).
- Justification: implementation and tests are complete for M0–M5 with all providers mocked, but external gates remain open: provider sandbox verification (G-DOKU, G-BS1–3, G-CW1, marketplace/ads access), accountant/tax sign-off (G-ACC1/G-TAX1), second approver (G-GOV1), Sumopod PITR and role privileges (G-OPS1/G-OPS2), ad-spend FX/entity policy (G-ADS1), Metabase instance (G-MB1 instance part). Residual security risks in docs/30.
- Next engineering work: M0 gate closure followed by M1 schema/application corrections, while building CI and safety checks in parallel.
- Do not sign off M5 until every required evidence link is attached and independently reviewed.
