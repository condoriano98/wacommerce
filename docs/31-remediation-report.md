# 31 — V3.1 correctness remediation report (2026-11-10)

> Scope: the code-review findings raised against `434ab5d` (financial correctness, transaction integrity, settlement
> and liquidity, authorization, forecasting, M5 operations). Branch `impl/v31`, draft PR #2. Source of truth for the
> architecture stays `docs/00-plan.md`; evidence for earlier milestones stays in `docs/27`.
>
> Method for every finding: inspect the code → write a regression test → run it and record the failure → minimal fix
> → rerun → related suites → full gate (ruff + full pytest, exit code checked unpiped; dashboard build + Playwright for
> UI) → commit → CI. Concurrency tests use separate PostgreSQL connections in threads released by a barrier.
> Nothing live was called; no deployment; no destructive production change.

## 1. Defect register

All findings were **still present** at `3787adf` (none had been fixed earlier). "Red" is the first failing result of the
regression test against the unfixed code; every test is green at `53c9c46`.

| ID | Finding (root cause) | Fix (files) | Commit | Regression test(s) | Red before the fix |
|---|---|---|---|---|---|
| A1 | `execute()` read `prepared` without a lock and every reader called the provider; transitions were unconditional (a stale worker could overwrite a confirmed outcome) | Liveness advisory lock per operation; atomic `prepared→sent` claim with a claim token; every later transition fenced on the token; takeover of a dead claim → lookup or `uncertain`; `external_operation_attempts` (`core/external_ops.py`, `channel_sync.py`, migration 0018) | `846cab4` | `test_remediation_concurrency.py::test_A1_*` (4) | 4 concurrent workers → **4 provider calls** |
| A2 | `journal_exists()` check-then-insert; no database identity for a posting | `journals` header, unique `(organization, source_type, source_id)` claimed by `claim_journal()`; existing duplicates backfilled as `legacy_duplicate` (flagged, never deleted); reversal once; inventory transfer / ad-spend deltas protected; credit-loss release key collision fixed (`finance/ledger.py`, `marketing.py`, `inventory.py`, `payments.py`, migration 0019) | `846cab4` | `test_A2_*` (3), `test_remediation_migrations.py` | **4** journals for one event; **3** for one ad-spend delta; a journal reversed **twice** |
| B1 | `payment_bank_matched` credited 1105 with the whole share whenever any 1105 balance existed | Payment applied in order: open recognised receivable (capped) → recovery of written-off amounts → customer advance for the undelivered part (`finance/posting.py`) | `f101a0c` | `test_B1_bank_match_after_partial_delivery_*` | 1105 driven to **−107,500** |
| B2 | Pickup took reservations of any warehouse and booked the last balance's cost for the whole line; delivery/RTS used an arbitrary in-transit balance | `shipment_cost_allocations` (qty + cost per balance consumed); pickup limited to the shipment's warehouse; GIT, COGS, RTS restoration and after-delivery returns use the recorded costs (`inventory.py`, `fulfillment.py`, `posting.py`, `settlements.py`, migration 0020) | `f101a0c` | `test_B2_*` (2) | The Surabaya shipment consumed a **Jakarta** reservation |
| B3 | Each group's COD added the line's full shipping allocation; expected remittance deducted the whole order's fees from every shipment; failed COD left 1100 negative | Per-line split of goods + shipping across groups (Σ = collectible); per-shipment fee deduction; re-split on cancellation with a case; COD carrier-cost reclass on RTS (`fulfillment.py`, `settlements.py`, `orders.py`, `posting.py`) | `f101a0c` | `test_B3_*` (2) | COD **330,000** collected for a **315,000** order; 1100 at **−12,000** |
| B4 | `marketplace_payout` booked the whole line to the first item's portfolio; two lines of one order collapsed into one journal | Lines aggregated per order; fee, tax and net split by item gross; per-item fee true-up; per-item 1110 clearing; reconciliation on value received; org-level item for multi-portfolio orders (`settlements.py`, `posting.py`, `reconciliation.py`) | `1600adf` | `test_B4_*` (2) | 1110 portfolio C **−73,600** / B **+73,600**; second line **dropped** |
| B5 | Provider-reported payouts debited 1000 (verified cash) on report | Account **1050 Funds in transit to bank**; payouts booked to 1050 with a `bank_payout` expectation; only a bank line of the same legal entity with the payout token and exact amount moves it to 1000 in the original dimensions; unmatched payouts age into `bank_unmatched`; cash forecast opening cash = 1000 only (`posting.py`, `settlements.py`, `procurement.py`, `payments.py`, `fpa/cash.py`, migration 0021; fixtures F1/F2/F4/F9/F11) | `1600adf` | `test_B5_*` (2) | Verified cash **+115,000** on a mere provider report; no ageing |
| C1 | FP&A permission check skipped without `portfolio_id`; no legal-entity scope; `/capital/summary` unchecked | `legal_entity` role scope + `app_legal_entity_visible()` in ledger/snapshot RLS; `require_financial_scope` (portfolio / entity / consolidated = org) on waterfall, snapshots, cash forecast; `fpa.read` on capital summary (`core/authz.py`, `api/fpa.py`, `api/reads.py`, migration 0022) | `52d0c54` | `test_remediation_security.py::test_C1_*` (5) | cs/packer read the waterfall (**200**); pm_a read L1/L2/L3 consolidated (**200**) |
| C2 | `order_slices.order_status` = header status (all portfolios' lines and cases) | Slice status derived from the visible rows only (window over the RLS-filtered rows) (migration 0022) | `52d0c54` | `test_C2_*` | pm_a's slice showed **`exception`** caused by portfolio B |
| C3 | `app_org()` trusted the GUC without membership; definer functions PUBLIC-executable without `pg_temp`; `resolve_shipment` picked an arbitrary tenant on duplicate references | `app_org()` returns the org only for an active member user; principal loading checks org membership; all definers `search_path = public, pg_temp`, no PUBLIC execute; ambiguous shipment references resolve to nothing; mock Biteship ids unique (migration 0023, `api/deps.py`) | `52ec827` | `test_C3_*` (6) + strengthened RLS fuzz (`test_m5_security.py`) | A real user claiming another org saw **10 users** of that tenant; 15 definers PUBLIC-executable; ambiguous reference resolved to one tenant (this also explained an order-dependent e2e failure). Already correct before the fix: forged token → 401; GUCs transaction-local on pooled connections |
| D1 | Cash forecast filtered ledger by entity but summed every open commitment of the org into week 1 | `capital_commitments.legal_entity_id` (backfilled) + `expected_payment_date`; forecast per entity and per payment week (`models/capital.py`, `capital.py`, `fpa/cash.py`, migration 0024) | `4077fc6` | `test_D1_*` | Entity 1 forecast carried entity 2's **Rp7,000,000** PO |
| D2 | Simulator had no stock: owned sales never ran out and were never replenished | Inventory at cost per week: opening stock, demand capped by stock (stockouts = lost sales), reorder on expected demand with lead time / safety cover / payment lag, carrying cost, weekly stock identity (`fpa/simulator.py`, engine `sim-1.1`) | `4077fc6` | `test_D2_*` (3) | No inventory model (structural); hand-computed scenario impossible |
| E1 | Browser held the bearer token in `localStorage` and called the OMS origin directly (no CORS → cross-origin calls failed); tests only mocked the API | Same-origin BFF: `/api/session` (httpOnly, SameSite=Strict, Secure cookie), `/api/oms/*` server-side proxy (CSRF header, auth/webhooks blocked, 401 clears session, 502 on outage), `proxy.ts` route guard, runtime `OMS_API_URL`; live Playwright suite on a real OMS + PostgreSQL; CI job `live-e2e` (`dashboard/*`, `oms/tests/e2e_seed.py`, `deploy/docker-compose.yml`) | `53c9c46` | `dashboard/tests/smoke.spec.ts` (7), `dashboard/tests-live/live.spec.ts` (8) | Token readable by page scripts; no session expiry handling; no live test |
| E2 | `notify.risk_alert` handler did nothing | Channel adapter (mock only), `notification_deliveries` (status, attempts, error, correlation id), persisted failures with delayed retries, dead letter + escalation event + case flag after `notify_max_attempts`, audit; case never modified (`fpa/risk.py`, `integrations/notify.py`, migration 0024) | `a5137c3` | `test_E2_*` (3) | Nothing delivered, nothing recorded |
| E3 | Live adapters bypassed the circuit breaker; Biteship token in URL | Every live call through `integrations/http.request`; AST test forbids direct `httpx` calls; uvicorn access log disabled in the image, our access log records route templates only; Biteship token documented as provisional (G-BS2) | `a5137c3` | `test_E3_*` (3) | **11** direct `httpx` calls (status lookups, Chatwoot, Shopee, CAPI, WhatsApp) outside the breaker |
| E4 | DR evidence was a 1.7 MB local drill only | Production DR checklist (docs/18, docs/28); `post_restore_report()` + DR drill post-restore counts; local drill re-run at head 0024 — **still preliminary** | `f1f08ff` | `test_m5_deploy.py`, drill JSON below | — (gate G-OPS1 stays open) |
| F | No subledger-vs-GL and attribution invariants | 9 new invariants (below); every remediation scenario ends with an invariant run; detection test with manufactured drift (`modules/invariants.py`) | `f1f08ff` | autouse fixture in `test_v31_correctness_remediation.py`, `test_F_*` | — |
| G | Migration safety unproven for the new tables | Forward-only expand migrations 0018–0024; round trip 0018 → head → 0017 → head on a scratch database with pre-existing duplicate postings | `f1f08ff` | `test_remediation_migrations.py` | — |

## 2. Financial integrity verification

Every scenario below runs the real services against PostgreSQL and asserts subledger amounts, not only balance. After
each one, the invariant run (Phase F) must be empty: journals balanced, stock subledger = GL 1200 + 1250 per portfolio,
1105 ≥ 0 and 2200 ≤ 0 per line, COD collectible over shipments, ledger entity = portfolio entity.

| Scenario | Evidence (amounts in IDR) |
|---|---|
| Partial delivery under release-on-attestation, then full bank match (B1) | 2 × Rp100,000 + shipping; 1 unit delivered → 1105 = 100,000 + ½ shipping; bank line → 1105 = 0, 2200 = the undelivered half, 1000 = order total; second delivery → 1105 = 2200 = 0, revenue = order total |
| Multi-warehouse valuation (B2) | Jakarta 2 @ 60,000 + Surabaya 1 @ 80,000 → GIT 200,000; RTS of the Surabaya shipment restores exactly 80,000 to Surabaya; Jakarta delivery COGS 120,000; subledger = GL |
| Split-shipment COD (B3) | Σ shipment COD = order total (goods + shipping once); shipping income = shipping fee; each expected remittance = its COD − its own carrier cost − its own COD fee; failed COD → 1100 = 0, 2010 = outbound + return shipping |
| Multi-portfolio marketplace settlement (B4) | Items C (120,000) and B (80,000); payout gross 200,000, fee 17,000, tax 1,000, net 182,000 → 1110 C = B = 0; 5300 C 10,200 / B 6,800; 1400 600 / 400; payout 109,200 / 72,800; fee above accrual → variance case (−1,000); two lines per order both settled |
| Verified bank cash (B5) | Gateway payout reported → 1000 unchanged, 1050 = payout, forecast opening cash unchanged; other entity's bank line → no match; own bank line → 1050 → 1000; re-import idempotent; unmatched → `bank_unmatched` case with exposure |
| Journal fixtures F1–F13 | Updated for B5 (bank receipt step); all fixture tests green; fixture balance/clearing test includes 1050 |
| Inventory forecast (D2) | 1-week stock cover: stockout in week 1 only, lost sales 1,000,000, POs in weeks 0 and 3, closing stock path 0/0/1.5M/1.0M/0.5M/2.0M, cash path includes PO payments; S6 over 26 weeks keeps stock ≥ 0 with less purchasing and lower CM2 |

## 3. Concurrency evidence (separate connections, barrier-released threads)

| Test | Result |
|---|---|
| `test_A1_concurrent_workers_same_operation_key_cause_exactly_one_provider_effect` | 4 workers, slow provider: **1** provider call, 1 attempt row; the others get `OperationInFlight`; a later retry returns the stored response |
| `test_A1_stale_claim_*`, `test_A1_superseded_worker_is_fenced_*` | Dead claimant + lookup policy → confirmed by lookup, 0 calls; without lookup → `uncertain` + case, 0 calls; superseded worker's finish is `fenced`, row untouched |
| `test_A2_concurrent_posting_of_the_same_event_produces_exactly_one_journal` | 4 concurrent `_post` → **1** journal, no errors |
| `test_A2_concurrent_direct_post_journal_same_key_*` | 3 concurrent ad-spend deltas with one key → **1** journal |
| Existing: `test_m4_review_fixes.py` concurrent CSV uploads, `test_m1_acceptance.py` oversell / over-procurement, `test_m4_channels.py` token rotation, `test_m2_reliability.py` worker SIGKILL (#6) | still green |

## 4. Security evidence

| Area | Tests |
|---|---|
| RLS under spoofed context | `test_m5_security.py::test_1_rls_fuzz_*` now requires **zero** rows when a real user claims another org (previously tolerated org-level rows); `test_C3_own_user_with_another_orgs_id_sees_and_writes_nothing` (ORG-kind tables, inserts into orders/outbox/products rejected); `test_C3_org_context_without_a_user_has_no_tenant` |
| Token / membership | `test_C3_token_for_a_user_of_another_org_is_rejected` (401) |
| Pooled connections | `test_C3_identity_is_transaction_local_on_a_pooled_connection` |
| Definer functions | `test_C3_security_definer_functions_pin_search_path_and_are_not_public` |
| Financial authorization | `test_C1_*` matrix (cs, packer, pm_a, finance, legal-entity-scoped finance) |
| Portfolio isolation | `test_C2_*`, live `portfolio managers see only their own lines…` |
| Webhook tenant resolution | `test_C3_ambiguous_shipment_reference_resolves_to_no_tenant` |
| Dashboard session | live: httpOnly/SameSite cookie, no token in page scripts, expired and forged tokens, CSRF header, blocked routes |
| Secrets in logs | `test_E3_biteship_shared_token_never_appears_in_logs`, existing `test_7_*` |

**Residual (not closed by this work): G-SEC1.** A holder of the `oms_app` database credentials can still assert a
matching (user, organization) pair through the GUCs. The database now refuses mismatched pairs, but full protection
needs per-tenant database roles or a separate identity service. Multi-tenant production security is **not** claimed.

## 5. Test summary

| Suite | Command | Result |
|---|---|---|
| OMS (lint + full suite, local PostgreSQL 16) | gate script: `uv sync --frozen && ruff check . && pytest` | **211 passed, 0 failed, 0 skipped** (171 before the remediation; +40 tests) |
| Dashboard build | `npm run build` | passes (route guard + API routes) |
| Dashboard mocked smoke | `npx playwright test --trace off` | **7 passed** |
| Dashboard live E2E (real OMS + PostgreSQL) | `npx playwright test -c playwright.live.config.ts --trace off` | **8 passed** |
| Migration round trip | `tests/test_remediation_migrations.py` | passes (0018 → head → 0017 → head) |
| Local DR drill at head 0024 | `deploy/backup/dr_drill.sh` | RTO 3.255 s on 2.1 MB, row counts match, 0 unbalanced journals, 100 tables with FORCE RLS; post-restore: 1 pending outbox, 1 sent-unconfirmed and 5 uncertain external operations, 9 payouts awaiting bank proof (test data) — **preliminary only** |

CI on `impl/v31` (OMS core / Security / Dashboard), push and PR runs:

| Commit | OMS | Security | Dashboard |
|---|---|---|---|
| `846cab4` A1–A2 | ✅ 48 / 49 | ✅ | — |
| `f101a0c` B1–B3 | ✅ PR 51 | ✅ 11 / 12 | ✅ 15 |
| `1600adf` B4–B5 | ✅ 52 / 53 | ✅ 13 / 14 | ✅ 16 |
| `52d0c54` C1–C2 | ✅ 54 / 55 | ✅ 15 / 16 | ✅ 17 |
| `52ec827` C3, `4077fc6` D, `a5137c3` E2–E3 | ✅ (through 61) | ✅ (through 22) | ✅ (through 20) |
| `f1f08ff` F, G, E4 | ✅ 62 / 63 | ✅ 23 / 24 | ✅ 21 |
| `53c9c46` E1 (adds `live-e2e`) | ✅ 64 / 65 | started 25 / 26 (result in the PR checks) | ✅ PR 23 — mocked smoke **and** `live-e2e` (browser → dashboard → OMS → PostgreSQL) green |

## 6. Remaining external blockers (unchanged by code)

- **Providers:** G-DOKU (sandbox signature/status), G-BS1–BS4 (COD remittance reporting, **webhook authentication —
  the shared token is provisional**, billing model, lookup by reference), G-CW1 (Chatwoot echo semantics), Meta WABA /
  CAPI (dataset, hashed fallback, `test_event_code`).
- **Marketplace / ads access:** Shopee eligibility, TikTok Shop ISV, Lazada approval, Google / TikTok Ads review;
  G-ADS1 (billing currency, FX policy, entity mapping).
- **Accounting:** G-ACC1 / G-TAX1 — accountant sign-off, including the new 1050 funds-in-transit policy and the
  payment application order (receivable → recovery → advance).
- **Governance:** G-GOV1 second approver.
- **Infrastructure:** G-OPS1 Sumopod PITR with a production-sized restore against RPO 15 min / RTO 4 h; G-OPS2 role
  privileges (note: definer functions owned by a non-superuser owner would be subject to FORCE RLS); **G-SEC1**
  database identity (new); G-MB1 Metabase instance.
- **Known engineering limitations (documented, not hidden):** outflows (refunds, supplier payments, ad charges,
  packing) still credit 1000 when recorded (conservative for liquidity, not bank-matched); marketplace allocation uses
  item gross, not provider item-level fees; COD per-shipment rounding vs per-shipment revenue rounding may differ by
  cents; returned goods are not restocked in the simulator; notification channels are mock-only; circuit-breaker state
  is per process.

## 7. Release decisions (two separate decisions)

1. **PR #2 — merge into the development branch:** ready **for human review**. CI for `53c9c46` is green for OMS
   (64/65) and Dashboard (PR 23, including the new `live-e2e` job); the docs-only commit that follows re-runs the
   Security workflow. All findings in this report have red→green regression tests, the
   full suite passes and no test was skipped or weakened (two earlier assertions that encoded the insecure behaviour
   were inverted: a portfolio manager reading the consolidated waterfall now expects 403; the RLS fuzz now expects zero
   rows under a spoofed org). Merging requires a reviewer's approval; I did not merge.
2. **Production:** **NO-GO.** Open: G-SEC1, G-OPS1/2, G-DOKU, G-BS1–4, G-CW1, G-ACC1/G-TAX1, G-GOV1, G-ADS1,
   marketplace/ads access, G-MB1. A green CI run is necessary but not sufficient; no live transaction, purchase,
   booking, refund, ad change or capital movement is authorised.
