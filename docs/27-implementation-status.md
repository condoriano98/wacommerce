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

## M4 — Marketplace & advertising expansion (mock_tested only)

### Acceptance-test ownership → evidence
| # | Test |
|---|---|
| 10 | `test_m4_channels.py::test_10_missing_credentials_means_not_authorized_and_no_calls`, `test_m4_imports_ads.py::test_10_live_ads_account_without_access_makes_no_calls` — live-enabled account without credentials: registry `not_authorized`, respx sees zero HTTP calls, mock adapter not called either |
| 11b | `test_m4_channels.py::test_11b_*` — missing payout ages into `marketplace_settlement_mismatch` with full exposure, late exact payout matches and resolves the case, replay idempotent; short payout opens a case with the shortfall as exposure |
| Contracts | `test_m4_channels.py` (normalisers on documentation-derived fixtures for Shopee/TikTok Shop/Lazada, signing schemes, LiveShopee pagination + error mapping via respx), `test_m4_imports_ads.py` (Meta insights) |
| Sync | windowed idempotent order sync with per-order error isolation, lost-push recovery, concurrent single-use token rotation (two connections, barrier: each refresh token spent exactly once), scheduled runs isolating a failing account |
| Imports | dry-run changes nothing; commit applies through domain services; re-upload deduplicated; invalid file rejected as a whole with line numbers; provenance (sha256, uploader) |

### M4 review findings (2026-11-10) — fixed
| Sev | Finding | Fix | Evidence |
|---|---|---|---|
| P0 | Duplicate rows within one CSV could reach domain services twice | In-file dedupe by fingerprint; rows claimed with `ON CONFLICT DO NOTHING RETURNING`; only claimed rows applied; batch stats record `duplicates_in_file` / `claimed` | `test_m4_review_fixes.py::test_p0_duplicate_lines_within_one_file_are_applied_once`, `::test_p0_concurrent_uploads_of_the_same_file_apply_exactly_once` |
| P1 | Registry could show `available` while live access is refused | `available` only after a recorded live authorization + capability check (`verification_source = live_check`); credentials alone → `verification_pending` (migration 0015); reauth blocker → `not_authorized` | `::test_p1_credentials_alone_are_verification_pending_never_available` |
| P1 | Refresh-token crash after provider success | Rotation recorded as an external operation (`sent` committed before the call); new tokens + confirmation stored atomically; a later attempt finding `sent` never re-spends the token → `uncertain`, `reauthorization_required`, `external_uncertain` case; runbook in docs/26 | `::test_p1_db_failure_after_provider_rotation_requires_reauth_and_never_reuses_token` |
| P1 | Ad spend boundaries (currency, entity, close) | Rows must carry currency; non-IDR rejected until an FX policy exists; ad accounts need an explicit legal entity (no default); closed-day re-pulls booked in the open period (asserted for CSV). Live ads imports remain gated by **G-ADS1** | `::test_p1_ad_spend_requires_idr_and_explicit_legal_entity`, `::test_p1_closed_period_ad_repull_is_booked_in_the_open_period` |

### Registry as built
Adapters with contract tests are `implementation_status = mock_tested`; `access_state` is `mocked` (not live-enabled),
`not_authorized` (live-enabled without credentials) or `temporarily_unavailable` (sync failure). No account can show
`available` without stored credentials, and no capability is `operationally_ready` (requires production_verified).
**No adapter is sandbox- or production-verified.** Fixtures are documentation-derived, not recorded from real shops.

### Open blockers (unchanged, external)
Shopee shop eligibility (Seller In-house app), TikTok Shop ISV/partner access for multi-shop, Lazada app approval
(Sponsored Solutions category unverified), Google Ads/Data Manager access, TikTok Ads app review, Meta `ads_read` system
user. CSV imports are the supported path until access exists.

## M5 — Production hardening (no deployment)

### Evidence (PR #1 hardening tests)
| # | Test / artefact |
|---|---|
| 1 | `test_m5_security.py::test_1_rls_fuzz_*` (Hypothesis: 8 roles × all portfolio-scoped tables × org-GUC spoof, reads + writes) |
| 2 | `test_m5_security.py::test_2_*` (FK catalogue — found and fixed `import_batches.platform_account_id`, migration 0017; DB rejects cross-org references) |
| 3 | `test_m5_ops.py::test_invariants_*`, `modules/invariants.py` (hourly worker task, API) |
| 4 | `test_m2_reliability.py` (worker SIGKILL), `test_m4_review_fixes.py` (token rotation crash) |
| 5 | `test_m2_whatsapp.py`, `test_m2_e2e.py`, `test_m5_ops.py::test_webhook_replay_window_*` |
| 6 | `test_m1_acceptance.py::test_11a_*`, `test_m4_channels.py::test_11b_*` |
| 7 | `test_m5_security.py::test_7_*` (responses, audit logs, logs, ciphertext; key rotation) + gitleaks in CI |
| 8 | `docs/28-dr-drill-evidence.md` — **preliminary** local drill (RTO 3.3 s on 1.7 MB; RPO target not met by logical backups) |
| 9 | `test_m5_ops.py::test_metrics_endpoint_protection_and_injected_failures` (dead letter + provider errors visible), `deploy/observability/alerts.yml` |
| 10 | docs/26 release checklist item 7 (process control; not testable in code) |

### Fixed during M5 (found by the new tests)
- `/webhooks/biteship` accepted unauthenticated callbacks → shared URL token (provider signature still unverified, G-BS2).
- `/agent/tools`, `/fpa/simulations/scenarios` were readable without sign-in → authenticated.
- `import_batches.platform_account_id` lacked a tenant-scoped FK → migration 0017.

### Not done / open
- Docker images not built here (no Docker daemon); compose/Dockerfiles checked statically only.
- gitleaks not run locally (downloading the binary was not permitted in this session); first result comes from CI.
- No Prometheus/Grafana/Sumopod deployment; alert rules not exercised end to end.
- Residual risks (docs/30): trusted identity GUCs for server roles; ORG-scoped tables check org only.

## Remediation after the M5 code review (2026-11-10)

The review of `434ab5d` found P0/P1 defects in transaction integrity, accounting, settlement, authorization, forecasting
and M5 operations. All were reproduced with failing regression tests and fixed; the register, red→green evidence,
financial integrity, concurrency and security evidence and the release decisions are in
**`docs/31-remediation-report.md`**.

| Phase | Commit | Findings | Migration |
|---|---|---|---|
| A — transaction integrity | `846cab4` | A1 atomic fenced external-operation claims; A2 database-enforced journal identity | 0018, 0019 |
| B — accounting / settlement / liquidity | `f101a0c`, `1600adf` | B1 partial bank matching; B2 per-warehouse cost; B3 split-shipment COD; B4 multi-portfolio payouts; B5 funds in transit (1050) | 0020, 0021 |
| C — authorization / tenant identity | `52d0c54`, `52ec827` | C1 financial scopes (+ legal-entity scope); C2 slice status; C3 membership-checked `app_org()`, definer hardening, ambiguous webhook refs | 0022, 0023 |
| D — forecasting | `4077fc6` | D1 entity-scoped commitments; D2 inventory-aware simulator | 0024 |
| E — operations | `a5137c3`, `f1f08ff`, `53c9c46` | E2 notifications; E3 breaker coverage; E4 post-restore report; E1 dashboard BFF + live E2E | 0024 |
| F / G — invariants, migrations | `f1f08ff` | 9 new invariants; migration round-trip test | — |

Local: **211 passed, 0 failed, 0 skipped** (OMS gate); dashboard 7/7 mocked + 8/8 live (real OMS + PostgreSQL).
CI: green on every remediation commit — OMS 48/49 (`846cab4`) through 64/65 (`53c9c46`), Security 11–24, Dashboard
15–23; Dashboard PR run 23 includes the new `live-e2e` job (real browser → dashboard → OMS → PostgreSQL), green. Production stays **NO-GO**; new gate **G-SEC1** (database identity).

## Milestone log
**M3 closed 2026-11-10**: local and CI green (both workflows) on `24a559f`/`26e3d68`; no external systems involved.
**M5 implementation closed 2026-11-10**: local and CI green on `434ab5d` (OMS, Security, Dashboard). This closes the implementation scope only — the release decision stays **NO-GO** (`docs/25`) until the external and governance gates close (G-DOKU, G-BS1–3, G-CW1, marketplace/ads access, G-ACC1/G-TAX1, G-GOV1, G-OPS1/2, G-ADS1, G-MB1 instance) and a human signs off.
**M4 closed 2026-11-10**: review findings (P0 in-file CSV duplicates, P1 access state, P1 token-rotation crash, P1 ad-spend boundaries) fixed in `454cf38`, CI green (push 37 / PR 38). All marketplace/ads providers mocked.
**M2 closed 2026-11-10**: local and CI green on `65224c0`; all providers mocked, n8n not externally verified.
**M1 closed 2026-11-10**: local and CI green on `3057cd1`. External verification: none (all providers mocked or absent).

| Milestone | Commit | CI | Local tests (pass/fail/skip) | Functional | External verification |
|---|---|---|---|---|---|
| M0 | `103608a` | ✅ PR run 3 | 56/0/0 (`cd oms && uv run pytest -q`) | spec consolidated, fixtures, inventory, impact assessment | provider gates open |
| M1 core | `73947dd` | ✅ push run 6, PR run 7 (`c0f085a` WIP failed, superseded) | 80/0/0 | services, migrations 0004–0007, acceptance #1–4, 7–9, 11a, 13a, 16, 17, 19, 20 | none (all providers mocked/absent) |
| M1 worker | `3057cd1` | ✅ push run 8 (42749665055), PR run 9 (42749670705) | 84/0/0 (`cd oms && uv run pytest`, PostgreSQL 16.x local) | Procrastinate executor (0008, `app/worker.py`) | G-PROC verified locally |
| M2 | `9d2473e` (slices `09c9d33`, `f634227`, `9692cc6`) | ✅ push run 17 (42753626706), PR run 18 (42753632856); slices ✅ runs 11, 13, 15 | 117/0/0 (`cd oms && uv run pytest`) | WhatsApp vertical slice, #5, #6, #14, #15, E2E | none — all providers mocked; n8n not verified |
| M3 | `24a559f` (slices `d0658a7`→fix `d367e84`, `76ff6c5`→fix `55a9a29`, `ef4792e`) | ✅ OMS push 30 / PR 31, Dashboard push 1 / PR 2 (`24a559f`); PR runs OMS 32 + Dashboard 3 (`26e3d68`) | 130/0/0 OMS (`cd oms && uv run pytest`); dashboard build + 5/0/0 Playwright | waterfall + tie-out, #12, #13b, S1–S9 + #18, cash forecast, bulk-buy, FP&A API, dashboard | none — no external systems involved; Metabase instance unverified |
| M4 | `9d45743` + review fixes `454cf38` | ✅ `9d45743` push 34 / PR 35, `b1a2735` PR 36; review fixes `454cf38` push 37 / PR 38 | 151/0/0 | adapters (mock), channel sync, CSV imports, #10, #11b, review P0/P1 fixed | none — all marketplace/ads access blocked; G-ADS1 open |
| Remediation | `846cab4` … `53c9c46` (9 commits) | ✅ OMS 48–65, Security 11–24, Dashboard 15–23 (PR 23 incl. `live-e2e`) | 211/0/0 OMS; dashboard 7/7 mocked + 8/8 live | review findings A1–G fixed with red→green regression tests (docs/31) | none — providers mocked; G-SEC1 added; release **NO-GO** |
| M5 | `1fd96d6`, `a10cb8e`, `ae17bd1`, CI fix `434ab5d` | ✅ `1fd96d6` push 39 / PR 40; `a10cb8e`/`ae17bd1` ❌ OMS runs 41–44 (CI installed a hand-written tool list without `hypothesis`; code unaffected) → fixed by `434ab5d`: OMS push 45 / PR 46, Security push 5 / PR 6 (gitleaks full history clean, pip-audit and npm audit clean), Dashboard PR 12 | 171/0/0 (`cd oms && uv run pytest`) | hardening tests 1–9 automated or evidenced; deploy configs; release checklist | DR drill preliminary (local only; G-OPS1 open); no deployment; release **NO-GO** |
