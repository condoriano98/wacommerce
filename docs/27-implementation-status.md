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
| Journal fixtures F1–F13 | **Implemented & tested** — YAML sanity (35) and real services against PostgreSQL (#19, 15 tests) | `oms/tests/test_journal_fixtures.py`, `oms/tests/test_accounting_services.py` |
| G-DOKU | **Documentation-verified 2026-11-10, not sandbox-verified**; `invoice_number` uniqueness unverified | `docs/04` DOKU row (sources linked) |
| G-BS1 / G-BS2 / G-BS3 | Open: COD remittance dashboard/CSV only; webhook auth unverified; billing per account (Topup/Invoice) | `docs/04` Biteship row |
| G-BS4 | **Closed (negative)**: no lookup by `reference_id` → uncertain bookings go to human reconciliation (ADR-015) | Biteship retrieve docs |
| G-CW1 | **Documentation/source-verified, not instance-verified**: API-created messages echo to `webhook_url`; suppress by `source_id`/sender | `docs/04` Chatwoot row |
| G-PROC | **Verified locally** with procrastinate 3.10.0: async worker, periodic tasks, `queueing_lock`/`lock`, heartbeat-based stalled recovery | `oms/tests/test_worker.py` (4 tests), local `python -m app.worker` smoke run |
| G-MB1 | **PostgreSQL isolation verified locally** (session_user identity; `set_config`/`SET ROLE` spoofing rejected; base tables inaccessible); Metabase instance configuration **unverified** | `test_m1_acceptance.py::test_20_reporting_role_identity_cannot_be_spoofed` |
| G-ACC1, G-TAX1 | Open: accountant decision | defaults configurable |
| G-GOV1 | Open: needs a second approver or a delegation policy | — |
| G-OPS1, G-OPS2 | Open: needs Sumopod confirmation | — |

## M1 — Financially correct OMS core

### Acceptance-test ownership → evidence (all against PostgreSQL 16 with RLS roles)
| # | Test |
|---|---|
| 1, 2 | `test_m1_acceptance.py::test_1_2_multi_portfolio_split_fulfilment_partial_return_and_refund` |
| 3 | `test_accounting_services.py::test_F2_cod_revenue_recognised_before_remittance` |
| 4 | `test_accounting_services.py::test_F1_prepaid_whatsapp_doku` |
| 7 | `test_m1_acceptance.py::test_7_isolation_across_app_worker_webhook_roles_and_sql`; `test_foundation.py` RLS tests |
| 8 | `test_m1_acceptance.py::test_8_concurrent_purchases_cannot_over_procure` (separate connections, thread barrier) |
| 9 | `test_m1_acceptance.py::test_9_concurrent_reservations_never_oversell` |
| 11a | `test_m1_acceptance.py::test_11a_unmatched_attested_payment_ages_into_exception_with_exposure` |
| 13a | `test_m1_acceptance.py::test_13a_closed_period_is_immutable_and_corrections_are_adjustments` |
| 16 | `test_foundation.py::test_approval_separation_of_duties_and_capital_reallocation_without_cash` |
| 17 | `test_m1_acceptance.py::test_17_consolidation_eliminates_intercompany_and_inter_portfolio` |
| 19 | `test_accounting_services.py::test_F1…test_F13_*` (15) |
| 20 | `test_m1_acceptance.py::test_20_partial_visibility_…`, `::test_20_reporting_role_identity_cannot_be_spoofed` |
| Jobs | `test_worker.py` (relay dedupe, stalled-worker recovery, failure recorded with backoff, API role has no queue access) |

### Migrations (forward-only; 0001–0003 history preserved)
`0004_v31_expand` (tables/columns/CHECKs) · `0005_v31_security` (oms_worker/oms_webhook/reporting roles alongside legacy
`oms_system`, definer resolvers, rewritten policies, composite tenant FKs, `order_slices`) · `0006_v31_data` (CoA incl.
1105/1109/5700, legal entities, identity domains, system principals) · `0007_v31_reporting` (reporting views,
`provision_reporting_principal`) · `0008_procrastinate` (queue in schema `procrastinate`, `oms_worker` only, pinned
function `search_path`). The contract migration (drop `oms_system`, superseded columns) is **not** written yet. It
belongs to a later release, after both role modes have run in staging (ADR-014).

### Known limitations carried into M2
- Provider adapters (DOKU, Biteship, Meta, Chatwoot) are not built; `external_operations` table exists but no adapter
  uses it yet. Test #6, the worker kill after an uncertain provider response, is owned by M2.
- No dead-letter review HTTP endpoint yet (`requeue_dead_letter` service exists). The endpoint and metrics are planned for M2/M5.
- `OMS_DB_ROLE_MODE=legacy` path kept for cutover. CI runs `v31` only.

## M2 — WhatsApp commerce vertical slice (all providers mocked)

### Acceptance-test ownership → evidence
| # | Test |
|---|---|
| 5 | `test_m2_whatsapp.py::test_5_*` (Meta duplicates, re-deliveries, out-of-order statuses, replay); `test_m2_e2e.py` (DOKU duplicate + stale FAILED, Biteship duplicated/out-of-order callbacks with provider re-fetch) |
| 6 | `test_m2_reliability.py` — real worker **process** SIGKILLed after the mock provider accepted: DOKU completes once via status lookup; Biteship ends `uncertain` + `external_uncertain` case, never re-booked; timeout-after-accept variants; human resolution via API (`test_m2_ops_api.py`) |
| 14 | `test_m2_agent.py` — compromised-LLM simulation: discount above cap, injected fields, human-only tools, unknown tools, unquoted amounts, refund/free-shipping/date promises, fake acceptance evidence, kill switch, handoff; every denial persisted |
| 15 | `test_m2_whatsapp.py::test_15_*` — Chatwoot echo (`source_id` `oms:*`) dropped, human reply sent exactly once, re-delivery deduplicated, private notes ignored |
| E2E | `test_m2_e2e.py` — WhatsApp → AI quote → acceptance → DOKU capture → mixed groups (owned + dropship) → Biteship booking → delivery → disbursement → ledger; full re-delivery of every webhook changes nothing (sends, payment pages, bookings, journals) |
| Other | `test_m2_capi.py` (eligibility, hashed identifiers, no resend), `test_m2_adapters.py` (offline contract), `test_m2_regressions.py`, `test_n8n_workflows.py` (static) |

### Built
Migrations `0009` (external operation response), `0010` (wa_events, message_mappings, message_templates, media_objects;
RLS + composite FKs), `0011` (`agent_usage` definer). `core/external_ops.py` (ADR-015 policies, human resolution +
job resume), adapters `integrations/{doku,biteship,meta_capi,mockstore}.py`, `modules/{agent,provider_webhooks}.py`,
messaging rework (persist-then-ack, replay, mappings, templates/window, echo suppression), APIs `api/{webhooks,agent,
operations}.py` (dead letters, uncertain operations, procurement copilot), n8n `n8n/workflows/*.json` (static-checked,
**not externally verified**).

### Defects found and fixed during M2
- SQLAlchemy 2.1 reports `rowcount = -1` for ORM `INSERT … ON CONFLICT DO NOTHING`: dedupe results were wrong since M1
  (state guards masked it for shipment events and gateway notifications; bank import counts were wrong; CAPI events
  were never queued). All four sites now use `RETURNING`; regression tests added.
- A requeued outbox message that died again violated `dead_letters` uniqueness; the existing dead letter is re-opened.

### Not done / open
- Media download/upload adapter and object storage (table `media_objects` exists; no adapter yet) — moved to M5 hardening.
- Procurement copilot dashboard pages — with the M3 dashboard. OMS n8n error-intake endpoint — M5 observability.
- Webhook secrets for DOKU/Chatwoot come from settings; per-account secrets need a webhook-readable definer (M5).

## M3 — FP&A and capital intelligence

### Acceptance-test ownership → evidence
| # | Test |
|---|---|
| 12 | `test_m3_fpa_core.py::test_12_allocation_change_is_effective_dated_and_13b_snapshots_survive_later_changes` — new allocation version from today; change inside a closed period refused; closed-day re-pull booked in the open period with `event_time` = original day; closed-period ledger line count unchanged |
| 13b | same test — close snapshot byte-identical after later activity/allocation change/re-pull; UPDATE/DELETE on `fpa_snapshots` rejected even for the schema owner; restatement = version 2; portfolio users see only their snapshot |
| 18 | `test_m3_simulator.py::test_18_stress_breaching_reserve_opens_case_and_notifies` — breach → `liquidity_breach` case with exposure + `notify.risk_alert` outbox; no breach → nothing |
| S1–S9 | `test_m3_simulator.py` — hand-computed base, S1 and S9; each scenario moves the expected metric; delays move cash not contribution; S9 only affects the marketplace portfolio; determinism; ledger-actuals override; Monte Carlo refused |
| Waterfall | `test_m3_fpa_core.py::test_waterfall_ties_to_ledger_*` — operating profit = ledger P&L; every P&L account mapped once; L2 + unallocated = L3; RLS portfolio view |
| API/UI | `test_m3_api.py` (FP&A API, read endpoints with RLS, reporting login on `reporting.monthly_waterfall`); `dashboard/tests/smoke.spec.ts` (5 Playwright tests, OMS API mocked) |

### Built
Migrations `0012` (cost_level_map, effective-dated allocations, append-only `fpa_snapshots`, `simulation_runs`) and `0013`
(`reporting.monthly_waterfall`). Modules `fpa/{waterfall,snapshots,simulator,risk,cash}.py`, `marketing.py`; APIs
`api/fpa.py`, `api/reads.py`; dashboard `dashboard/` (Next.js 16 + Refine 5 + Tailwind/shadcn-style components, 10 pages,
reads the OMS API only) with its own CI workflow `dashboard-ci.yml`.

### Process defects (own mistakes, fixed)
- `d0658a7` and `76ff6c5` were pushed with a failing test / lint error because command output was piped through `tail`,
  masking the exit status. Fixed in `d367e84` and `55a9a29`; commits now go through a gate script whose exit code is
  checked unpiped.

### Not done / open
- Metabase instance itself not deployed or verified (G-MB1: PostgreSQL side verified).
- Dashboard has no live-auth E2E against a running OMS (API mocked in Playwright); write actions in the UI are limited
  to sign-in and running simulations.
- Simulator inputs are illustrative; calibration from ledger actuals beyond elapsed-week override is not automated.

## Milestone log
**M2 closed 2026-11-10**: local and CI green on `65224c0`; all providers mocked, n8n not externally verified.
**M1 closed 2026-11-10**: local and CI green on `3057cd1`. External verification: none (all providers mocked or absent).

| Milestone | Commit | CI | Local tests (pass/fail/skip) | Functional | External verification |
|---|---|---|---|---|---|
| M0 | `103608a` | ✅ PR run 3 | 56/0/0 (`cd oms && uv run pytest -q`) | spec consolidated, fixtures, inventory, impact assessment | provider gates open |
| M1 core | `73947dd` | ✅ push run 6, PR run 7 (`c0f085a` WIP failed, superseded) | 80/0/0 | services, migrations 0004–0007, acceptance #1–4, 7–9, 11a, 13a, 16, 17, 19, 20 | none (all providers mocked/absent) |
| M1 worker | `3057cd1` | ✅ push run 8 (42749665055), PR run 9 (42749670705) | 84/0/0 (`cd oms && uv run pytest`, PostgreSQL 16.x local) | Procrastinate executor (0008, `app/worker.py`) | G-PROC verified locally |
| M2 | `9d2473e` (slices `09c9d33`, `f634227`, `9692cc6`) | ✅ push run 17 (42753626706), PR run 18 (42753632856); slices ✅ runs 11, 13, 15 | 117/0/0 (`cd oms && uv run pytest`) | WhatsApp vertical slice, #5, #6, #14, #15, E2E | none — all providers mocked; n8n not verified |
| M3 | `24a559f` (slices `d0658a7`→fix `d367e84`, `76ff6c5`→fix `55a9a29`, `ef4792e`) | _pending_ | 130/0/0 OMS (`cd oms && uv run pytest`); dashboard build + 5/0/0 Playwright | waterfall + tie-out, #12, #13b, S1–S9 + #18, cash forecast, bulk-buy, FP&A API, dashboard | none — no external systems involved; Metabase instance unverified |
