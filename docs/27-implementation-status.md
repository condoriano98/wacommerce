# 27 — Implementation Status (V3.1 execution)

Labels: **Implemented & tested** · **Implemented, not externally verified** · **Mocked** · **Planned** · **Blocked**.
Functional status and external-verification status are reported separately (spec §27.12).

## Branch and CI
- Development branch: `impl/v31` (from `claude/intelligent-cori-6qujr0` @ `a0aa78e`; PR #1 merged with SHAs preserved,
  merge commit `d6fb08a`).
- CI: `.github/workflows/oms-ci.yml` (PostgreSQL 16 service; ruff + pytest) on `pull_request` and on `push` to `impl/**`,
  `claude/**`. A milestone closes only when the GitHub check for its commit is green and the local run is green.

## M0 — Architecture verification and prerequisites

### Migration and database inventory (2026-11-10, read-only)
| Item | Finding |
|---|---|
| Alembic revisions | `0001_schema`, `0002_security`, `0003_reference_data` (linear) |
| Local `oms` (dev) | at `0003`, 0 organizations, 0 ledger entries — disposable |
| Local `oms_test` | at `0003`, test data only (22 orgs, 107 ledger lines); dropped/recreated by every test run — disposable |
| Remote branches with migrations | none (`chore/m5-readiness-gates-20261110` = CI/docs only) |
| Deployment configs | none in repository |
| Sumopod databases | **unknown** — not proven disposable → history preserved; forward migrations 0004+ (ADR-013/014) |

### Code-impact assessment (V3.0 code vs V3.1 spec)
| V3.0 code | Conflict with V3.1 | Replacement (milestone) |
|---|---|---|
| `core/config.py` `system_database_url`; `core/db.py` `system_engine/system_session`; migration `0002` creates `oms_system` with BYPASSRLS; tests use it | D4 / §7: no runtime BYPASSRLS | `oms_worker`, `oms_webhook` roles + definer resolvers + per-org system principals behind `OMS_DB_ROLE_MODE` (M1, expand); drop `oms_system` in contract migration |
| `modules/org.execute_capital_reallocation` posts Cash/3100 journals; `api/foundation.py` returns `journal_id` | D8 / §10: mandates are not cash | `capital` module: mandates, allocation events, commitments; no GL (M1) |
| `orders.fulfillment_route` single column; `routing.route_order` → MANUAL_REVIEW on mixed sources | D2 / ADR-010 | fulfillment groups per order, per-group routing (M1) |
| `ProcurementTask UNIQUE(order_item_id)`; `ExternalPurchase UNIQUE(procurement_task_id)` | D3: blocks split purchasing | `purchase_tasks` per group, `purchase_lines` with qty, trigger-maintained `qty_procured` + CHECK (M1) |
| `finance/posting.revenue_on_delivery` credits 1200 at delivery while stock leaves at shipment | C13: subledger ≠ GL in transit | goods in transit 1250 at pickup; COGS from 1250 at delivery (M1) |
| `finance/accounts.py` 1300 customer advances (liability in asset range) | C11 | 2200 contract liabilities; new 1020/1105/1109/1250/1400/5700… (M1, `0006`) |
| `payments.verify_manual_transfer` → `paid` + Dr cash | D9, §27.3 | `reported → attested → bank_matched`; no entry until bank match (M1) |
| `orders` RLS: visible if any line visible (header totals leak) | §27.4 | header policy + `order_slices` (M1) |
| `core/outbox.dispatch_due` custom dispatcher | ADR-003 rev., §27.6 | Procrastinate executor + stalled recovery + `dead_letters` + `external_operations` (M1 core / M2 providers) |
| WIP `modules/messaging.py` (system session, inline processing) | D7, §12 | persist-then-ack `wa_events`, mappings, echo suppression (M2) |
| COD remittance wording in V3.0 plan | C2 | already revenue-at-delivery in code; F2 fixture governs |

### Verification gates
| Gate | State | Evidence |
|---|---|---|
| Journal fixtures F1–F13 | **Implemented & tested** (sanity) | `oms/tests/test_journal_fixtures.py` (35 tests); service-level proof = acceptance #19 (M1) |
| G-DOKU, G-BS1–4, G-CW1 | Open — research in progress | to be recorded in `docs/04` with source + date |
| G-PROC | Open — verified by M1 worker tests | — |
| G-MB1 | **Unverified** until reporting-role isolation tests (#20) pass | — |
| G-ACC1, G-TAX1 | Open — accountant decision | defaults configurable |
| G-GOV1 | Open — needs second approver or delegation policy | — |
| G-OPS1, G-OPS2 | Open — Sumopod confirmation | — |

## Milestone log
| Milestone | Commit | CI | Local tests (pass/fail/skip) | Functional | External verification |
|---|---|---|---|---|---|
| M0 | _pending commit_ | _pending_ | 56/0/0 (`cd oms && uv run pytest -q`) | spec consolidated, fixtures, inventory, impact assessment | provider gates open |
