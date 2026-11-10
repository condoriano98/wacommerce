# Wacommerce V3.1 — Financially Controlled Commerce & Capital Allocation OS (Canonical Specification)

Status: **V3.1 architecture revision (planning only)** · supersedes V3.0 plan of 2026-11-09 and design revisions R1–R5
(2026-11-10) by integrating them · repo `condoriano98/wacommerce`, branch `claude/intelligent-cori-6qujr0`.

Reading guide: §A is the V3.0→V3.1 delta. §1–§18 are the consolidated architecture (newer decisions already applied).
§19 milestones, §20 tests, §21 contradiction register, §22 backlog, §23 documentation set, §24 current-code impact,
§25 risks and verification gates, §26 executive summary.

Status labels used everywhere: **Implemented & tested** · **Implemented, not externally verified** · **Mocked** ·
**Planned** · **Blocked (credentials/provider access)**. Capability states (provider registry, §6):
`available | unsupported | not_authorized | temporarily_unavailable | mocked | planned`.

---

## Context

The founder runs several e-commerce portfolios funded from one constrained capital pool (simulated at Rp100M). Sales
start on WhatsApp (Meta Click-to-WhatsApp ads) and expand to Shopee, Tokopedia & Shop / TikTok Shop and Lazada. Stock is
either owned (Engine A, shipped via Biteship incl. COD) or dropshipped from Shopee/Tokopedia sellers by a human operator
(Engine B). The objective is **risk-adjusted contribution, capital productivity, cash conversion, operational reliability
and portfolio liquidity** — never GMV or ROAS in isolation.

V3.0 produced a working foundation (69-table schema, Postgres RLS, approvals, ledger, outbox, quotes, order state
machine, routing, posting rules; 21 green tests) but an architecture review found P0 defects: COD/cash recognition
contradictions, single-path orders, procurement uniqueness that blocks multi-supplier orders, a BYPASSRLS system role,
capital reallocations posted as fake cash, receipt-based payment "verification", and two systems competing for the Meta
webhook. V3.1 fixes these at the design level before more code is written. **No live external effects, production
migrations, purchases, bookings, refunds, ad changes or capital movements are authorised by this plan.**

---

## A. V3.0 → V3.1 Architecture Delta

| # | Existing V3.0 design | Issue | V3.1 design | Affected modules | Migration implications | Priority | Acceptance criteria |
|---|---|---|---|---|---|---|---|
| D1 | Plan §5: "COD → revenue only after delivered + remittance matched"; R2 said revenue at delivery | Contradiction; revenue delayed by cash timing | Revenue recognised at **control transfer per delivered shipment line** (configurable trigger per channel/payment term); COD remittance only clears COD receivable | `finance/posting`, `fulfillment` | New accounts (goods in transit, carrier balances, contract liabilities); posting rules rewritten | P0 | Journal fixtures F1–F12 balance; COD revenue booked in delivery period while receivable open |
| D2 | One `orders.fulfillment_route`; mixed owned+dropship → MANUAL_REVIEW | Real orders mix sources/portfolios/suppliers | **Fulfillment groups** per order (route, source, warehouse/supplier); item-level quantities; shipments with shipment items; aggregate status derived | `orders`, `routing`, `fulfillment`, `procurement` | New tables `fulfillment_groups`, `shipment_items`, qty columns on `order_items`; drop single route | P0 | One order with owned + 2 suppliers + 2 portfolios is partially delivered/returned/refunded and reconciles |
| D3 | `procurement_tasks UNIQUE(order_item_id)`, `external_purchases UNIQUE(procurement_task_id)`, plan "UNIQUE(order_id)" | Blocks multi-supplier and split purchases | Purchase tasks per fulfillment group; **purchase lines with quantities**; DB invariant `procured_qty <= qty_allocated_dropship` on `order_items` + `UNIQUE(marketplace_account, external_order_id)` | `procurement` | Replace constraints; add `purchase_lines` | P0 | Concurrent duplicate purchase attempts → one succeeds; over-procurement rejected by DB |
| D4 | `oms_system` role with BYPASSRLS for webhooks/worker; RLS visibility via definer functions | Unnecessary bypass; cross-tenant FKs possible | No BYPASSRLS at runtime: worker/webhook use non-bypass roles + definer **resolver functions**; composite tenant FKs `(organization_id, id)`; explicit `WITH CHECK` everywhere; migration owner only for DDL | `core/db`, migrations, `messaging`, worker | Roles reworked; composite unique keys + FKs on all tenant tables | P0 | Negative tests: cross-org, cross-portfolio, cross-legal-entity reads/writes fail as app, worker and webhook roles |
| D5 | Hierarchy org → brand → portfolio | Missing legal entity, inventory owner, identity domain | Add `legal_entities`, `bank_accounts`, `identity_domains`, inventory owner = (legal_entity, portfolio) | `org`, `customers`, `inventory`, `finance` | New tables; FKs from brands/accounts/warehouses | P0 | Two legal entities in one org keep separate ledgers; consolidation eliminates intercompany |
| D6 | Customer identities unique per brand | BSUID is scoped to the Meta business; marketplace buyer IDs to the shop | `customer_identities UNIQUE(identity_domain_id, provider, identity_type, value)`; internal immutable customer UUIDs | `customers`, `messaging` | Identity domain backfill | P0 | Same phone in two businesses → two customers; same BSUID twice → one |
| D7 | Chatwoot WhatsApp inbox proxy + "reassert webhook" cron (V3.0), then R1 API-channel | Competing ownership; echo loops undefined | **OMS sole Meta owner**, Chatwoot API-channel inbox, message mapping table with origin, echo suppression, raw-event persistence before ack, async processing/replay | `messaging`, `integrations/chatwoot`, n8n | `wa_events`, `message_mappings`; remove reassert job | P0 | E2E: no duplicate sends, no loops, handoff works, replay idempotent |
| D8 | Capital reallocation executed as cash journals (Dr Cash/Cr 3100) | Manufactures bank movements; inflates assets | **Management capital ledger** (mandates, allocations, commitments) separate from GL; real transfers only between legal-entity bank accounts as intercompany journals | `capital` (new), `finance` | Remove cash postings from reallocation; new tables | P0 | Reallocating Rp10M changes mandates, GL unchanged |
| D9 | Manual transfer "verified" by a human from receipt → paid | Screenshot ≠ settlement | Payment states `reported → attested → bank_matched`; receipts/attestations post nothing; default: fulfil only after bank match; optional approval-backed "release on attestation" posts revenue to 1105 receivable pending verification (correction 3); ageing exception at T+2 business days | `payments`, `reconciliation` | New payment states, `bank_statement_lines` | P0 | Receipt alone never sets `bank_matched`; unmatched ages into exception |
| D10 | Reconciliation implicit in settlements table | No expected/actual/evidence/ageing model | **Reconciliation module**: runs, items (expected, actual, diff, evidence, operator, class, ageing, exposure, approvals) for DOKU, bank, Biteship COD, marketplaces, suppliers, stock counts, ads | `reconciliation` (new) | New tables | P0 | Every money movement traceable event → journal → settlement evidence |
| D11 | ADR-003 dropped Procrastinate for a custom SKIP LOCKED dispatcher | V3.1 stack retains Procrastinate | **Transactional outbox table retained** (atomic with business writes) + **Procrastinate** as executor (relay, leases, retries, periodic jobs, dead letters) | `core/outbox`, worker | Add procrastinate schema migration | P1 | Kill worker mid-job → job re-leased, effect applied once |
| D12 | Exceptions as cases (R4) | Not tied to reconciliation/ageing/exposure | Cases link to reconciliation items, carry financial exposure, ageing buckets, SLA, approval history | `exceptions` | Columns added | P0 | Exposure totals on dashboard equal open reconciliation differences |
| D13 | FP&A contribution (single formula) | Levels mixed; no CM1–CM3; no close | **Waterfall CM1/CM2/CM3/operating profit**, close periods, snapshots, effective-dated allocations, budget vs forecast | `fpa`, `finance` | `accounting_periods`, `fpa_snapshots`, effective dating | P1 | Editing allocation policy does not change closed periods |
| D14 | Provider facts inside plan text | Implemented adapter confused with approved access | **Integration Capability Registry** with states, verification date/source, blockers | `platforms` | `integration_capabilities` table | P1 | Dashboard shows "mocked" vs "available" per capability |
| D15 | AI limits (R5) | Matrix incomplete; untrusted input rules implicit | **AI authority matrix** (allowed / conditional / human) + per-tool contract (actor, scope, validation, idempotency, log, escalation, timeout) | `agent`, n8n | `agent_tools` policy config | P1 | Every denied tool call logged; prompt-injection fixtures cannot widen permissions |
| D16 | Ops requirements scattered | No RTO/RPO, rotation, DLQ, environments | §18 production requirements; four environments | deploy, docs | — | P1 | Restore drill meets RPO/RTO in staging |

---

## 1. Business model (preserved)

- **Portfolios** funded from a constrained pool (editable Rp100M scenario). Hierarchy:
  Organization → Legal entity → Brand → Portfolio → Products/SKUs → Listings on Platform accounts → Campaigns →
  Orders → Fulfillment → Ledger → FP&A. Many-to-many where real (SKU↔portfolio, campaign↔portfolio, account↔portfolio,
  warehouse↔portfolio, order lines↔portfolios).
- **Commerce channels**: WhatsApp Business (Meta CTWA), Shopee, Tokopedia & Shop / TikTok Shop, Lazada, future channels.
- **Advertising**: Meta, Google, TikTok, marketplace ads; CSV fallback.
- **Engine A — owned stock**: inventory owned by a (legal entity, portfolio); internal packing; Biteship; COD.
- **Engine B — human-operated dropshipping**: Shopee/Tokopedia supplier listings; the human operator buys; OMS gives
  recommendations, ceilings, financial checks, approvals, evidence capture. No marketplace browser automation (ToS:
  Shopee Syarat Layanan §3.1, §6.3(k); Tokopedia anti-robot clause — verified 2026-11-09).
- **Platform-managed fulfilment** for native marketplace orders (platform logistics rules apply).
- **Optimisation target**: risk-adjusted contribution, capital productivity (contribution per Rp committed), cash
  conversion, reliability, liquidity.

## 2. Technology stack and runtime (preserved, clarified)

| Layer | Choice | Notes |
|---|---|---|
| Backend | FastAPI, SQLAlchemy 2 (sync, psycopg 3), Alembic, Pydantic v2 | Modular monolith (ADR-001/002) |
| Database | PostgreSQL 16 | System of record; RLS; Sumopod managed |
| Jobs | **Transactional outbox + Procrastinate** (Postgres-backed) | Outbox row written in the business transaction; a Procrastinate relay job claims outbox rows and runs handlers; Procrastinate provides leases, retries with backoff, periodic tasks, locks per key, dead-letter (failed) state. **Gate G-PROC**: confirm Procrastinate version supports sync psycopg3 workers + periodic tasks + `lock`/`queueing_lock`; fallback = keep current SKIP LOCKED dispatcher behind the same interface |
| Frontend | Next.js + Refine + shadcn/ui | Ops + approvals + finance UI |
| Reporting | OMS API (RLS) for portfolio users; Metabase OSS for org-level users on PII-free `reporting` views via per-org login roles (DB-role identity, correction 7) | OSS has no row-level security |
| Orchestration | n8n (Sumopod) | Never authoritative; calls OMS commands only |
| Shared inbox | Chatwoot Community, **API-channel inbox** | Runtime deps: Rails web, Sidekiq, Redis 7, Postgres, object storage for attachments |
| WhatsApp | Meta WhatsApp Cloud API (Graph v27.0, configurable) | OMS is the only Meta client |
| Shipping | Biteship via `CarrierAdapter` | Mengantar not integrated (no API) |
| Payments | DOKU, manual bank transfer, COD | |
| Hosting | Sumopod managed services + app containers (api, worker, dashboard) | |
| Dev/CI | Docker Compose; CI Postgres service container | Local sandbox without Docker uses local Postgres (as in this session) |
| Tests | pytest, testcontainers (CI), respx, Playwright | |
No microservices, Kafka or extra queues unless a measured requirement appears.

## 3. Reuse vs build (preserved, updated)

| Capability | Reuse | Build |
|---|---|---|
| Orchestration, schedules, notifications | n8n + n8n-skills plugin (Apache-2.0) — orchestration only | Workflow JSON in `n8n/workflows/` |
| WhatsApp API knowledge | `whatsapp-cloud-api` skill (MIT, reference) | OMS gateway (ingest, send, templates, window) |
| Shared inbox, handoff | Chatwoot API-channel inbox (MIT core; Captain AI paid → not used) | Chatwoot adapter + mapping |
| Meta reporting / audit | `meta-ads-kit` (`meta-ads`, `budget-optimizer`, `pixel-capi`, MIT) + Meta `meta-ads` CLI; `facebook/agentic-tools` devtools (setup/diagnostics, user OAuth) | Insights ingestion, CAPI outbox |
| Ad copy | `silvanasolvers/meta-ads-skill` — not adopted (overlap) | — |
| Backend | FastAPI (+skill), SQLAlchemy, Alembic, Pydantic, Procrastinate | Domain modules |
| Auth / RBAC | argon2 + PyJWT; Postgres RLS | Scoped role assignments |
| Secrets | Fernet envelope encryption (`cryptography`), master key in Sumopod secret env; rotation via key versions | Vault functions |
| Procurement | Copilot (human). agent-browser = dev/QA only; browser-use rejected (LLM-per-step, CAPTCHA-solving cloud) | Procurement UI/API |
| Payments | DOKU adapter | `PaymentProvider` (doku, manual, cod) |
| Shipping | Biteship adapter | `CarrierAdapter` |
| Marketplace sellers | Official APIs (§17); aggregators (Jubelio/Ginee) not adopted (weak fee/settlement, no ads) | `SalesChannelAdapter` + CSV |
| Ads | Official APIs (§17) | `AdsProviderAdapter` + CSV |
| Dashboard | Next.js + Refine + shadcn/ui | 10 sections |
| Analytics | Metabase OSS | Views |
| FP&A narratives | openaccountant skills = formula/layout references only (they need unavailable "Wilson" tools, cash-basis, USD, single business) | Deterministic SQL/Python FP&A |
| Tests | pytest, testcontainers, respx, Playwright | Suites, fixtures |

Skills-audit conclusions (preserved): all repo skills are instructions except `agent-browser`; n8n-skills rules adopted
(validate→verify, credentials in n8n credential store, Respond-to-Webhook on both paths, explicit error workflow — avoid
"No Workflow" ([n8n#40824](https://github.com/n8n-io/n8n/issues/40824), closed 2026-10-20), verify HTTP Request error
routing on our version ([n8n#35899](https://github.com/n8n-io/n8n/issues/35899), closed 2026-08-13)); n8n Data Tables not
used for business state; n8n HITL cards not used for money approvals ("blind approval").

## 4. Architecture overview

```
Next.js/Refine ─► OMS API (FastAPI) ◄──────────────► PostgreSQL (RLS, composite tenant FKs)
                     │  commands only                     ▲
n8n (AI Sales Admin, notifications, schedules) ──────────┘ (never direct DB writes)
Meta Cloud API ─► /webhooks/whatsapp ─► wa_events (persist, commit, 200) ─► job: process ─► domain
OMS ─► Chatwoot API-channel inbox (mirror) ; Chatwoot agent reply ─► /webhooks/chatwoot ─► send command ─► Meta
Providers (DOKU, Biteship, Shopee, TikTok Shop, Lazada, Meta Ads, Google, TikTok Ads) ◄─► adapters (mock|sandbox|live)
Outbox ─► Procrastinate workers ─► handlers (send, mirror, CAPI, sync, reconcile, notify)
Read-only views ─► Metabase ; FP&A engine ─► recommendations (advisory) ─► human approvals
```
Bounded modules: `org`, `capital`, `platforms`, `catalog`, `suppliers`, `customers`, `messaging`, `agent`, `quotes`,
`orders`, `fulfillment`, `procurement`, `inventory`, `payments`, `settlements`, `reconciliation`, `finance`, `fpa`,
`marketing`, `conversions`, `exceptions`, `governance`.

## 5. Domain model / ERD (revised)

Conventions: every tenant table has `organization_id`; parents expose `UNIQUE(organization_id, id)`; children reference
parents with **composite FKs** `(organization_id, parent_id)` so cross-tenant references are impossible. Client-side
UUIDs. Money `NUMERIC(18,2)` IDR; unit costs `NUMERIC(18,4)`; rates `NUMERIC(7,4)`.

| Context | Tables (key constraints) |
|---|---|
| Org & entities | `organizations`; `legal_entities(org, name, npwp, pkp_status, accounting_framework, base_currency)`; `bank_accounts(legal_entity, provider[bank/ewallet/gateway/carrier_balance], account_ref)`; `brands(legal_entity)`; `portfolios(brand, code UNIQUE per org, status, risk, targets, approved channels/sources)`; `users`; `role_assignments(user, role, scope_type[organization/legal_entity/portfolio/platform_account], scope_id)` |
| Capital (management ledger, not GL) | `capital_scenarios`; `capital_mandates(portfolio, period, category, amount, effective_from/to, approval)`; `capital_allocation_events` (append-only: reallocate/increase/decrease, approval); `capital_commitments(portfolio, category, amount, status[proposed/approved/partially_consumed/consumed/released/expired], source, expires_at)`; `commitment_consumptions(commitment, journal_id/amount)`; `risk_policies` |
| Platforms | `providers`; `platform_accounts(legal_entity, brand, provider, provider_account_id, role, status, live_enabled, api_version, public_config)`; `account_portfolio_links`; `account_credentials` (no app grants; key_version); `integration_capabilities(account|provider, capability, state, scopes, api_version, sandbox, rate_limit, webhook_status, last_sync_ok, last_contract_verified_at, verification_source, blockers)`; `integration_sync_runs`; `integration_errors` |
| Catalog & suppliers | `products`; `skus(code UNIQUE per org, inventory_policy[owned/dropship_only/hybrid], safety_stock, reorder_point)`; `portfolio_skus`; `platform_listings(account, sku, portfolio, price, fee profile, max_discount_rate)`; `suppliers`; `supplier_offers(portfolio, sku, marketplace, listing_url, expected_variant, reference_price, price_ceiling, priority, approved)`; `supplier_observations` (append-only, human-recorded) |
| Customers & identity | `identity_domains(org, kind[meta_business/marketplace_shop/email], scope ref)`; `customers(org, immutable id, merged_into)`; `customer_identities UNIQUE(identity_domain, provider, identity_type, value)`; `customer_portfolios`; `addresses`; `privacy_preferences`; `consent_events` (append-only) |
| Messaging | `wa_events(raw, signature_ok, dedupe_key UNIQUE, received_at, processed_at, attempts)`; `conversations(account, customer, window_expires_at, ai_enabled, handoff_reason)`; `messages(direction, author[customer/ai/human/system], origin[meta/chatwoot/oms], body, media_ref, status)`; `message_mappings(message, wamid UNIQUE per account, chatwoot_message_id UNIQUE, echo_token)`; `message_templates(account, name, language, category, status)`; `media_objects(storage_key, mime, sha256, retention)` |
| Attribution & conversions | `landing_sessions`; `customer_attribution` (one row per touch, UNIQUE source message); `conversion_outbox` (UNIQUE subject+event+destination+occurrence; stable event_id); `conversion_delivery_logs` |
| Quotes & orders | `quotes`, `quote_lines`; `orders(channel, account, external_order_id UNIQUE per account, customer, aggregate_status derived, payment_terms[prepaid/cod/marketplace])`; `order_items(portfolio, sku, qty_ordered, qty_allocated_owned, qty_allocated_dropship, qty_procured, qty_shipped, qty_delivered, qty_canceled, qty_returned, qty_refunded, status; CHECKs: allocations ≤ ordered, procured ≤ allocated_dropship, shipped ≤ allocated, delivered ≤ shipped, returned ≤ delivered)`; `order_item_allocations(item, kind[discount/shipping_income/shipping_cost/payment_fee/cod_fee/marketplace_fee/tax/refund], amount, method, policy_version)`; `order_events`, `order_item_events` (append-only) |
| Fulfillment | `fulfillment_groups(order, route[OWNED_STOCK/SHOPEE_DROPSHIP/TOKOPEDIA_DROPSHIP/PLATFORM_MANAGED], source ref, status)`; `fulfillment_group_items(group, item, qty)`; `routing_decisions` (inputs, reasons, rules version); `shipments(group, carrier account, external_id, waybill, cod_amount, status)`; `shipment_items(shipment, item, qty)`; `shipment_events` (UNIQUE shipment+status+occurred_at); `returns`, `return_items` |
| Procurement | `purchase_tasks(group, supplier_offer, status, landed_cost, expected_contribution, approval)`; `purchase_lines(task, order_item, qty, unit_price)`; `external_purchases(task, marketplace_account, external_order_id UNIQUE per marketplace account, amount_paid, evidence media, purchased_by)`; `purchase_purchase_lines` link |
| Inventory | `warehouses(legal_entity)`; `inventory_balances(legal_entity, portfolio, sku, warehouse; on_hand, reserved, in_transit; CHECK reserved ≤ on_hand)`; `inventory_movements` (append-only); `inventory_reservations`; `stock_counts`, `stock_count_lines` |
| Payments & settlements | `payments(order, method, provider, provider_ref UNIQUE, amount, status[pending/authorized/captured/reported/attested/bank_matched/failed/expired/refunded])`; `payment_events`; `payment_notifications` (UNIQUE provider+notification_id); `refunds(order, items, amount, status, approval, method)`; `settlements(account, kind[gateway_disbursement/cod_remittance/marketplace_payout/carrier_withdrawal], external_id UNIQUE per account, gross, fees, net, status)`; `settlement_lines`; `bank_statement_lines(bank_account, booked_at, amount, reference, import_batch, UNIQUE fingerprint)` |
| Reconciliation & exceptions | `reconciliation_runs(kind, period, source)`; `reconciliation_items(expected_ref, expected_amount, actual_ref, actual_amount, difference, evidence, operator, classification, status, aged_bucket, exposure)`; `exception_cases(link to reconciliation item/order/shipment/task, exposure, ageing, SLA)`; `exception_events` |
| Finance | `ledger_accounts`; `accounting_periods(legal_entity, period, status[open/soft_closed/closed])`; `ledger_entries(legal_entity, journal_id, account, portfolio, platform_account, order_item, bank_account, debit, credit, accounting_date, event_time, source_type, source_id, posting_rule_version)`; deferred balance trigger; `intercompany_links`; `operating_expenses`; `tax_profiles` |
| Marketing | `ad_campaigns`, `ad_groups`, `advertisements`, `daily_ad_insights(… pulled_at, revision)`; `campaign_portfolio_allocations(effective_from/to, policy_version)` |
| FP&A | `fpa_snapshots(period, scope, version, payload, created_by)`; `budgets(version: budget/forecast)`; `forecast_runs`; `simulator_scenarios`, `stress_runs` |
| Governance & AI | `audit_logs`; `human_approvals` (CHECK approver ≠ requester); `agent_execution_logs(actor, org, portfolio, tool, input hash, decision, reason, idempotency key, latency, outcome)`; `agent_policies` |

## 6. Integration Capability Registry & provider verification

Registry rows exist per (platform account or provider) × capability. Adapter existence ≠ access: e.g. Shopee
`orders.read` may be `mocked` in code but `not_authorized` for the shop. Verification facts (2026-11-09 research;
re-verify at Milestone 0, dates recorded per row):

| Provider / capability | Verified facts (source) | Initial state | Blockers / gates |
|---|---|---|---|
| WhatsApp Cloud API (Graph v27.0) | `X-Hub-Signature-256` HMAC over raw body; BSUID `from_user_id` since Apr 2026, phone may be absent; CTWA `referral` with `source_id`=ad id, `ctwa_clid` absent for Status placements ([webhook ref](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/text.md), [BSUID](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids)) | mocked | WABA + system user token |
| Meta CAPI Business Messaging | Dataset `POST /{WABA_ID}/dataset`; `POST /{dataset}/events`, `action_source=business_messaging`, 14 events; **Meta does not dedupe**; ≤7 days ([doc](https://developers.facebook.com/docs/marketing-api/conversions-api/business-messaging)) | mocked | `whatsapp_business_manage_events`; Indonesia hashed-fallback support **unverified**; `test_event_code` on WABA dataset **unverified** |
| Meta Marketing API insights v27.0 | `level=ad,time_increment=1`; 28-day revisions; 7d/28d view windows removed Jan 2026; ~4-month version deprecation ([versioning](https://developers.facebook.com/docs/graph-api/changelog)) | mocked + CSV | `ads_read` system user |
| Chatwoot v4.19.1 | API channel inbox; Agent Bot events; `toggle_status`; `X-Chatwoot-Signature` over `{ts}.{body}` ([api](https://developers.chatwoot.com/api-reference)). Doc/source check 2026-11-10: messages created via the API are delivered back to the API-channel `webhook_url` (echo); `echo_id` is **not** in the callback payload → suppress by `source_id` set on OMS-created messages or by sender type ([api channel](https://www.chatwoot.com/docs/product/channels/api/create-channel), [webhook trigger](https://github.com/chatwoot/chatwoot/blob/develop/lib/webhooks/trigger.rb)) | mocked | self-host on Sumopod; **G-CW1 documentation-verified, not instance-verified** (confirm echo on our version in sandbox) |
| DOKU | Doc check 2026-11-10 (documentation-verified, not sandbox-verified): Checkout `POST /checkout/v1/payment`; non-SNAP signature header `Signature: HMACSHA256=base64(hmac(secret, components))`, `Digest` only for requests with body; `Request-Id` is the idempotency key (replay → 409); notifications SUCCESS/FAILED (Checkout ignores FAILED), retries at +30 min/+6 h/+12 h; status `GET /orders/v1/status/{invoice_number or request_id}` ([checkout](https://developers.doku.com/accept-payments/doku-checkout/integration-guide/backend-integration), [signature](https://developers.doku.com/get-started-with-doku-api/signature-component/non-snap/signature-component-from-request-header), [idempotency](https://developers.doku.com/get-started-with-doku-api/idempotency-request.md), [retry](https://developers.doku.com/get-started-with-doku-api/notification/retry-notification), [status](https://developers.doku.com/get-started-with-doku-api/check-status-api/non-snap.md)) | mocked | **G-DOKU partially closed** (docs); `invoice_number` uniqueness enforcement **unverified**; sandbox keys required for sandbox verification |
| Biteship | Orders API with `destination_cash_on_delivery`, rates, tracking, cancel, webhooks, sandbox ([webhook](https://biteship.com/docs/api/webhook/overview)). Doc check 2026-11-10: `Authorization: Bearer` with distinct test/live key prefixes; duplicate `reference_id` rejected (error 40002060); retrieve by Biteship order id only — **no lookup by reference_id** ([create](https://biteship.com/en/docs/api/orders/create), [retrieve](https://biteship.com/en/docs/api/orders/retrieve), [auth](https://biteship.com/en/docs/api/authentication)) | mocked | **G-BS1**: COD remittance reporting is dashboard/CSV only (no API found) → CSV import; **G-BS2**: webhook signature/authentication **unverified** (treat callbacks as triggers, re-fetch order); **G-BS3**: Topup (saldo) vs Invoice billing per account, withdrawal fee reportedly none (unverified); **G-BS4 closed negative**: no reference lookup → uncertain bookings go to human check (ADR-015) |
| Shopee Open Platform v2 | Seller In-house app (ERP lacks Ads); access 4h / refresh 30d single-use; auth ≤365d; 15-day windows; `get_escrow_detail(_batch)`, `get_escrow_list`, `get_income_detail`; push + `get_lost_push_message` ([guide](https://open.shopee.com/developer-guide/12)) | mocked | Shop eligibility (Mall/Managed/≥30 orders in 30d) → not_authorized |
| Tokopedia | Legacy Open API terminated 30 Sep 2025 → TikTok Shop API ("Tokopedia & Shop"); history only via Seller Center CSV ([notice](https://partner.tiktokshop.com/docv2/page/k1156vkq)) | unsupported (legacy); mocked via TikTok Shop | branding rule: no TikTok branding to ID sellers |
| TikTok Shop Partner API | Multi-shop needs ISV; access 7d; `shop_cipher`; finance statements/transactions incl. PPh 22, affiliate, GMV Max ([finance](https://partner.tiktokshop.com/docv2/page/finance-api-overview)) | mocked | ISV/partner access → not_authorized |
| Lazada Open Platform (ID) | Seller In-house app; token 10d / refresh 50d non-refreshable; push needs OV/EV TLS → polling ([auth](https://open.lazada.com/apps/doc/doc?docId=108260)) | mocked | app approval; Sponsored Solutions app category **unverified** |
| Google Ads API v26.1 | Cloud-project access (no dev token since 9 Sep 2026); GAQL; offline conversions via **Data Manager API** ([access](https://developers.google.com/google-ads/api/docs/api-policy/access-levels)) | mocked | customer data terms; gclid capture on landing page |
| TikTok Ads API v1.3 | Company developer account; async reports; GMV Max per store ([report](https://business-api.tiktok.com/portal/docs?id=1740302848100353)) | mocked | app review |
| Marketplace ads | Shopee Ads API (Seller In-house/Ads app types), Lazada Sponsored Solutions, TikTok GMV Max; Tokopedia TopAds → CSV | mocked / CSV | per above |
| Mengantar | No documented API | unsupported | replaced by Biteship |
| Shopee/Tokopedia buyer side | No buyer API; ToS prohibit bots | unsupported (by design) | human copilot only |
| n8n instance MCP | Settings → Instance-level MCP; currently disabled on Sumopod | not_authorized | owner enables; build-time only, not runtime |

CSV fallback (all sales/ads/settlement/bank imports): schema-validated import batches, provenance (file hash, uploader,
source), row fingerprints for duplicate prevention, dry-run preview, reconciliation against existing records.

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

## 8. State machines (revised)

Independent machines; aggregate order status is **derived** (never set directly).

- **Order item**: `open → allocated(owned|dropship|platform) → (procuring → procured) → shipped → delivered`, partial
  quantities tracked per stage; side branches `canceled`, `returned`, `refunded` per quantity.
- **Fulfillment group**: `pending → ready (paid or COD-approved) → (A) reserved → packing → packed → booked → in_transit →
  delivered | failed | returned` · `(B) purchase_pending → awaiting_approval → purchased → supplier_shipped → delivered |
  failed` · `(PM) platform_fulfilling → shipped → delivered`.
- **Shipment**: `draft → booked (approval for paid booking) → picked → in_transit → delivered | on_hold | failed |
  returning → returned`; idempotent carrier events.
- **Payment**: `pending → authorized/captured (gateway, re-checked) → settled (disbursement matched)`; manual:
  `reported (receipt) → attested (finance + bank ref; no ledger effect) → bank_matched (ledger)`;
  `failed | expired | refunded (partial)`. Fulfilment release default = `captured` (gateway, re-checked) or
  `bank_matched` (manual); optional approved policy = `attested` with exposure limit (1105).
- **Settlement**: `expected → received → matched | mismatch → resolved`.
- **Purchase task**: `open → observed → blocked | awaiting_approval → approved → purchased (lines, qty) → shipped →
  delivered | canceled | supplier_canceled`.
- **Derived order status**: placed / awaiting_payment / in_fulfillment / partially_delivered / delivered /
  partially_returned / closed / canceled / exception (any open P0 case).
- Concurrency: row locks on items/balances/tasks; DB CHECKs on quantities; idempotency keys on every command.

## 9. Accounting policy and journal mappings (revised — P0.1)

Framework: **G-ACC1** confirm with the company's accountant — SAK EP (private entities, effective 2025) or SAK EMKM;
principles below follow IFRS 15/PSAK 72-consistent control-transfer logic and are configurable per legal entity.
Tax: **G-TAX1** PKP status / PPN rate and PPh 22 withheld by marketplaces → `tax_profiles`; until confirmed, tax lines are
recorded as separate allocations (not netted into revenue silently).

Policies:
1. Order confirmation creates no journal.
2. Prepaid collection before control transfer → **contract liability** (customer advances), never revenue.
3. Revenue + COGS recognised **per shipment line at control transfer** (default trigger: carrier `delivered`;
   configurable per channel/payment term, e.g. platform "completed"). Partial deliveries recognise only delivered qty.
4. Owned stock leaving the warehouse moves Inventory → **Goods in transit** (keeps stock subledger = GL); COGS at delivery.
5. COD: at delivery Dr COD receivable (carrier) / Cr revenue; COD fee and carrier-deducted shipping accrued at delivery
   against the receivable; remittance clears the receivable to carrier balance or bank. **No revenue waiting on cash.**
6. Marketplace: at delivery Dr marketplace receivable / Cr revenue; expected fees accrued from listing fee profile;
   settlement trues up fees/tax/adjustments; payout clears receivable to bank.
7. Expenses are accrued once (source-keyed) and their payment only clears the payable (never re-expensed).
8. Advertising: accrued daily from insights (Dr Advertising / Cr ads payable or prepaid ad balance); invoice/charge
   clears; 28-day re-pulls post **delta adjustments** in the open period referencing the original date.
9. Refunds: before revenue → reduce contract liability; after revenue → sales returns & allowances; cash on payout.
10. Returns: resellable → Dr Inventory / Cr COGS at original cost; damaged → reclassify to return losses.
11. Dropship: supplier payment = **supplier prepayment** asset until customer delivery → COGS; supplier cancellation →
    supplier refund receivable.
12. Every posting rule is versioned (`posting_rule_version`), idempotent on (source_type, source_id), and posts into an
    open period of the legal entity; closed periods accept only adjustment journals in the current period.

Chart of accounts (renumbered; contract liability moved to the liability range):
1000 Bank · 1010 Gateway receivable (DOKU) · 1020 Carrier balance (Biteship) · 1030 Prepaid ad balances · 1100 COD
receivable · 1105 Customer receivable – transfer pending verification · 1109 Allowance for expected credit losses ·
1110 Marketplace receivable · 1120 Supplier refund receivable · 1200 Inventory · 1250 Goods in transit ·
1210 Supplier prepayments · 1400 Prepaid taxes (PPh 22 creditable) · 1900 Intercompany receivable · 2000 Supplier
payables · 2010 Carrier payables · 2020 Ads payables · 2100 Refunds payable · 2200 Contract liabilities (customer
advances) · 2300 Taxes payable · 2900 Intercompany payable · 3000 Owner capital · 3900 Retained earnings · 4000 Sales
revenue · 4100 Shipping income · 4900 Sales returns & allowances · 5000 COGS · 5100 Shipping expense · 5200 Payment &
COD fees · 5300 Marketplace fees · 5400 Packing · 5500 Return & failed-delivery losses · 5600 Variable AI/CS cost ·
5700 Expected credit losses (net of recoveries) ·
6000 Advertising · 6900 Operating expenses.

Journal fixtures (executable test data at Milestone 1; amounts IDR):
- **F1 Prepaid WhatsApp (DOKU)**, goods 190,000 + shipping 15,000, COGS 120,000, gateway fee 1,435, Biteship 12,000:
  capture Dr1010 205,000/Cr2200 205,000 · fee (known at capture) Dr5200 1,435/Cr1010 1,435 · pickup Dr1250 120,000/
  Cr1200 120,000; Dr5100 12,000/Cr2010 12,000 · delivery Dr2200 205,000/Cr4000 190,000/Cr4100 15,000; Dr5000 120,000/
  Cr1250 120,000 · disbursement Dr1000 203,565/Cr1010 203,565.
- **F2 COD**, goods 150,000 + shipping 15,000, COD fee 4,950, carrier-deducted shipping 12,000: pickup Dr1250/Cr1200 ·
  delivery Dr1100 165,000/Cr4000 150,000/Cr4100 15,000; Dr5000/Cr1250; Dr5200 4,950/Cr1100 4,950; Dr5100 12,000/
  Cr1100 12,000 · remittance Dr1020 148,050/Cr1100 148,050 · withdrawal Dr1000/Cr1020 (less withdrawal fee → 5200).
- **F3a Manual transfer, bank match before delivery** (default policy): reported (no journal) → attested (no journal)
  → bank line matched Dr1000 115,000/Cr2200 115,000 → delivery Dr2200/Cr4000 100,000/Cr4100 15,000.
- **F3b Manual transfer, release on attestation** (approved policy): reported/attested (no journal) → delivery
  Dr1105 115,000/Cr4000 100,000/Cr4100 15,000 → bank match Dr1000/Cr1105; if unmatched past SLA: Dr5700/Cr1109
  allowance (write-off Dr1109/Cr1105 only with approval).
- **F4 Marketplace**, 240,000, expected fee 19,200, actual 20,000, PPh 22 1,200: delivery Dr1110 240,000/Cr4000;
  Dr5300 19,200/Cr1110 · settlement Dr1000 218,800/Dr5300 800/Dr1400 1,200/Cr1110 220,800.
- **F5 Dropship**, prepaid 165,000, supplier 100,000: capture Dr1010/Cr2200 · purchase Dr1210 100,000/Cr1000 ·
  delivery Dr2200 165,000/Cr4000/Cr4100; Dr5000 100,000/Cr1210.
- **F6 Partial fulfilment**: 2-line prepaid order; line 1 delivered (F1 for line 1 only); line 2 canceled before
  shipment Dr2200/Cr2100; refund paid Dr2100/Cr1000.
- **F7 Refund (allowance, no return)** after delivery: Dr4900/Cr2100 → paid Dr2100/Cr1000.
- **F8 Return resellable**: Dr4900/Cr2100; Dr1200/Cr5000 at original cost; return shipping Dr5500/Cr2010.
- **F9 Settlement fee deduction** (DOKU fee unknown until disbursement): disbursement Dr1000/Dr5200/Cr1010.
- **F10 Failed delivery (RTS)**: no revenue; goods back Dr1200/Cr1250; outbound + return shipping Dr5100/Dr5500/
  Cr2010; prepaid → refund or reship per case decision.
- **F11 Supplier cancellation**: Dr1120/Cr1210 → refund received Dr1000/Cr1120.
- **F12 Ad spend & restatement**: daily accrual Dr6000/Cr2020 per allocation (portfolio or unallocated); re-pull delta
  ±Dr6000/Cr2020 in open period; card charge Dr2020/Cr1000.
Invariants: every journal balances (deferred trigger); stock subledger value = 1200 + 1250 per (legal entity, portfolio);
contract liabilities = Σ prepaid undelivered order value; COD/marketplace receivables = Σ open expected settlements.

## 10. Capital allocation, commitments, cash and liquidity (revised — P0.5)

Five distinct concepts:
1. **Capital mandate** (management budget per portfolio/category/period; effective-dated; approval-backed) — management
   ledger, **no GL entry**.
2. **Capital commitment** (reserved against a mandate for a specific purpose: inventory PO, supplier purchase, ad
   budget, opex) — proposed/approved/consumed/released; consumption linked to the GL journal that spent the money.
3. **Actual cash transaction** — GL journals on bank/gateway/carrier accounts of a **legal entity**.
4. **Working capital** — portfolio-dimensioned balance-sheet lines: inventory + goods in transit + receivables (COD,
   marketplace, gateway) + supplier prepayments − payables − contract liabilities.
5. **Unrestricted liquidity** — bank + clearing balances − committed outflows due − liquidity reserve − refund payables
   (shown both with and without customer-advance cash).
Reallocation between portfolios = mandate events only (D8). Real money moves only between **legal-entity bank
accounts** (intercompany journals 1900/2900, eliminated in consolidation). Capital productivity = CM3 ÷ average
committed + deployed capital. Commitment checks gate procurement and ad-budget recommendations.

## 11. Reconciliation and exceptions (revised — P0.6)

Reconciliation kinds: DOKU payments ↔ notifications ↔ disbursements ↔ bank lines · manual transfers ↔ bank lines ·
Biteship COD collected ↔ remittance ↔ deductions ↔ withdrawals · marketplace orders ↔ settlement lines (fees, tax,
refunds) ↔ payouts · dropship purchases ↔ supplier evidence ↔ payment out · stock movements ↔ physical counts ·
ad insights ↔ invoices/charges ↔ portfolio allocations.
Each `reconciliation_item`: expected ref/amount, actual ref/amount, difference, evidence (media/bank line/provider
payload), responsible operator, classification (timing, fee variance, missing, duplicate, unknown), ageing bucket
(0–2, 3–7, 8–14, >14 days), financial exposure, status, approval history. Rules: receipt screenshots never set
`bank_matched`; unmatched or uncertain items stay open and auto-open exception cases at thresholds; accepting a variance
needs an approval tier; every match is traceable operational event → journal → settlement evidence.
Exception playbooks (preserved from `docs/17`, extended): delivery_failed, returned_to_sender, cod_refused,
lost_or_damaged, refund_requested, cod_settlement_mismatch, cod_not_remitted, supplier_out_of_stock,
supplier_price_drift, supplier_canceled, dropship_late, wrong_item, payment_mismatch, marketplace_settlement_mismatch,
duplicate_order, oversold_marketplace, manual_review, **bank_unmatched**, **stock_count_variance**,
**ad_spend_unallocated**. Cases carry exposure + ageing; SLA breaches alert.

## 12. WhatsApp single-owner integration (revised — P0.4)

Flow: Meta → `/webhooks/whatsapp` (raw bytes, signature verified, `wa_events` insert with dedupe key = sha256 of
entry/message ids, **commit, then 200**) → job `wa.process` (parse, identity, messages, attribution, consent, statuses)
→ mirror to Chatwoot API inbox (`message_mappings.echo_token`) → Chatwoot Agent Bot → n8n AI Sales Admin → OMS
`/agent/*` commands (policy-checked) → OMS send command → outbox `wa.send` → Meta → mapping + status sync.
Human replies: Chatwoot API-channel callback → `/webhooks/chatwoot` (signature) → **echo suppression**: ignore messages
whose `echo_token`/source id was created by an OMS mirror; else create OMS send command (author human).
Covered: replay (re-run `wa.process` for an event), durable retries, dedupe, status sync (sent/delivered/read/failed),
media (download via Graph media API → private object storage with retention; outbound media upload), templates
(registry + approval status; required outside the 24h window), window enforcement, AI vs human authorship, handoff
(`toggle_status=open`, AI disabled on conversation), multi-WABA/multi-number routing via `platform_accounts`, read-only
webhook-config drift alert (never rewritten). n8n has no direct DB or Meta credentials.

## 13. Meta attribution & CAPI (preserved, aligned)

Capture per inbound message: `referral` → `customer_attribution` touch (`ad_id=source_id` when `source_type=ad`,
validated against ad accounts the org owns; `ctwa_clid`; raw referral); landing-page `ref_code` in prefilled `wa.me`
text links `landing_sessions` (fbp, fbc, fbclid, gclid, UTM); absent data → `unattributed` (never fabricated). Order
attribution = latest touch within 7 days, frozen at confirmation. Event policy (Business Messaging CAPI): first
consented inbound → LeadSubmitted; qualification → QualifiedLead; confirmed → OrderCreated; prepaid captured+accepted →
Purchase; COD → Purchase at delivery (aligned with revenue recognition); shipped/delivered/canceled/returned → Order*.
Send-time eligibility: supported event ∧ identifiers ∧ consent (`meta_ad_measurement`) ∧ ≤7 days → else suppressed /
expired. Own dedupe via unique business key; retries reuse event_id. Website Pixel+CAPI dedupe by event name+ID (48h).
Insights: daily, `level=ad`, 28-day re-pull, version configurable, CSV fallback. Three measures kept separate:
Meta-reported, internally attributed, financially realized. UU PDP 27/2022 consent capture and withdrawal; PP 33/2026
effective 2027-01-16 (legitimate-interest assessment documentation).

## 14. AI Sales Admin — authority matrix (revised — §6 of brief)

| Class | Actions | Enforcement |
|---|---|---|
| Allowed | product recommendations, inquiry classification, stock/price queries (ATP buckets, listing prices), order-draft (quote request), delivery-status queries, service explanations, escalation recommendation | read-only tools; channel/portfolio scope from RLS |
| Conditional (OMS-validated) | checkout/quote creation, payment-link creation for an offered quote, discount ≤ min(listing cap, AI cap), order confirmation (requires customer acceptance evidence: inbound message after quote matching acceptance pattern or interactive `accept_quote:{id}`), customer messaging (reply validator: amounts ∈ quote/catalog set; no refund/delivery-date/free-shipping promises outside policy; 24h window) | server-side policy; per-conversation & daily limits; channel kill switch |
| Human approval | supplier purchases, refunds, exceptional price changes, capital reallocation, ad-budget changes, credential changes, accounting adjustments, any irreversible exception | `human_approvals` (approver ≠ requester, amount tiers) |
Per-tool contract: actor (service principal), org + portfolio context, permitted action, input schema validation,
idempotency key policy, `agent_execution_logs` record (incl. denials via savepoint), escalation path (handoff), timeout
and failure behaviour (fail closed → handoff). Customer messages, marketplace descriptions and supplier text are
untrusted data: never interpreted as instructions; tools accept structured parameters only; prompt-injection fixtures
in tests.

## 15. FP&A methodology (revised — P1)

Levels: L1 SKU × platform × channel; L2 portfolio; L3 consolidated (eliminations: intercompany, inter-portfolio stock
transfers at cost).
Waterfall (per order line, aggregated):
Gross sales − discounts − refunds/returns/allowances − revenue adjustments = **Net sales**
Net sales − COGS − marketplace & payment fees − variable shipping & fulfilment = **CM1**
CM1 − attributable marketing (direct attribution first; effective-dated allocation policy) = **CM2**
CM2 − variable AI/customer-service cost − variable return/exception cost = **CM3**
CM3 − allocated fixed opex (documented driver) = **Operating profit**.
Rules: each cost appears at exactly one level (cost-to-level map); taxes per `tax_profiles` (output VAT excluded from net
sales; withholding as prepaid tax, not cost); ad prepayments expensed when consumed; unallocated marketing and shared
opex reported as separate lines at L2/L3 (never silently spread); accounting adjustments shown separately.
Metrics: actual vs budget vs forecast; rolling 13-week cash-flow forecast; working-capital forecast; portfolio liquidity
projection; cash conversion cycle; capital utilisation; contribution per Rp committed; break-even CPA/ROAS per SKU;
inventory turnover, ageing, sell-through; supplier concentration; settlement delay distribution; return and
failed-delivery rates; risk-adjusted bulk-buy evaluator (demand stability, savings, carrying cost, sell-through,
downside, post-buy liquidity); stress tests. Decimal arithmetic, explicit rounding (HALF_UP to 1 rupiah for allocations,
remainder to last line), IDR reporting currency, FX table if foreign currency appears.
**Rp100M simulator**: Portfolios A/B/C, reserve, ad budgets, inventory buys, dropship working capital, fulfilment cost,
settlement delays, fixed opex, test-and-learn acquisition cost, reinvestment/recovery. Stress (correction 8): S1 CPA
+30/+60%, S2 CVR −25%, S3 returns ×2, S4 COD delay +7/+14d, S5 supplier cost +10/+20%, S6 sell-through −40%, S7 ROAS −35%,
S8 channel outage 7d, S9 marketplace settlement delay +14d with platform fee +2pp; "combined downside" preset
(S1+S2+S4+S5). Outputs per portfolio and consolidated (deterministic): profit, liquidity path, minimum cash and shortfall
date, capital recovery horizon, capital productivity; base case reconciles to ledger actuals for elapsed periods.
Probabilities (Monte Carlo) only after distributions are documented and validated. Advisory only.
**Close & audit**: `accounting_periods` (open → soft close → closed), `fpa_snapshots` versioned, adjustment journals,
restatement only via adjustments with approval, effective-dated allocations, separate operational vs accounting
timestamps, immutable journals/audit.

## 16. Payments (revised)

DOKU payment links (adapter after G-DOKU verification); notification = trigger → status re-check via API → capture;
manual transfer reported → attested → bank_matched (receipts never debit bank; F3a/F3b); COD per F2; partial refunds per item allocation; payment fees as
allocations; idempotent notifications; mismatches → `payment_mismatch` case.

## 17. Marketplace & advertising integrations (preserved from V3.0 §1a)

| Provider | Path | Key constraints | Initial status |
|---|---|---|---|
| Shopee Open Platform v2 | Seller In-house app per selling entity | single-use refresh (atomic rotation), 365d auth, 15-day windows, escrow detail fees, income detail cash, push + lost-push recovery, Ads `v2.ads.*` | Mocked → Blocked (eligibility) |
| Tokopedia | via TikTok Shop API ("Tokopedia & Shop"); history CSV | branding rule; dual listing since 29 Oct 2026 | Mocked + CSV |
| TikTok Shop Partner API | ISV for multi-shop | 7d tokens, `shop_cipher`, month-versioned endpoints, webhooks 1/4/11/12/64/67/27/71/6/7/35, statements & transactions (PPh 22, affiliate, GMV Max), 429/36009002 backoff | Mocked → Blocked (ISV) |
| Lazada (ID) | Seller In-house app | 10d/50d tokens, re-auth reminders, polling (OV/EV TLS), at-least-once push dedupe, fee lines per item | Mocked |
| Google Ads v26.1 | Cloud-project access | GAQL daily, Data Manager API conversions, gclid capture | Mocked → Planned |
| TikTok Ads v1.3 | Company dev account | async reports, GMV Max per store, offline events | Mocked |
| Marketplace ads | Shopee Ads, Lazada SS, TikTok GMV Max, TopAds CSV | — | Mocked / CSV |
All versions/lifetimes/windows live in registry config, never hard-coded. Native orders: idempotent ingest
(`UNIQUE(account, external_order_id)`), platform-managed fulfilment groups, shared-stock reservation with
`oversold_marketplace` cases, settlement ingestion → reconciliation.

## 18. Operations, security, reliability (new — P1)

- **Environments**: local dev (compose, mocks), integration test (CI, testcontainers, mocks), sandbox (provider sandboxes
  where available; `live_enabled=false` except sandbox flags), production (Sumopod). No live integration enabled by
  default; live requires credentials + registry state `available` + explicit flag + approval for irreversible calls.
- **Secrets**: Sumopod secret env for master keys; credential key versions + rotation job; revocation; no secrets in
  logs/prompts/repo.
- **Data**: private object storage for media/evidence (signed URLs, retention per purpose); PII access audited; retention
  schedule (chat 24 months, evidence 10 years for financial records per tax law **G-TAX2 verify**).
- **Backup/recovery**: Sumopod Postgres PITR (**G-OPS1 verify availability**), daily logical backups off-site; targets
  RPO 15 min, RTO 4 h; quarterly restore drill.
- **Jobs**: leases, retry with backoff, dead-letter review queue, poison-message alerts; outbox guarantees
  (at-least-once + idempotent handlers); webhook replay protection (dedupe keys, timestamp windows).
- **Integrations**: circuit breakers per account, rate-limit budgets from registry, backoff on 429.
- **Observability**: structured JSON logs with correlation IDs (request → job → provider call), metrics, health checks
  (api, worker, DB, outbox lag, webhook lag, reconciliation backlog), alerts.
- **Delivery**: migrations forward-only with tested downgrade where safe; expand/contract for schema changes; release
  checklist; Chatwoot (web + Sidekiq + Redis + storage) and n8n sized and monitored.

## 19. Implementation milestones (gated)

**M0 — Architecture & provider verification** (≈5 engineer-days)
Deliverables: `docs/` set (§23), ERD, ADRs (incl. ADR-003 revised: outbox + Procrastinate), accounting policy + F1–F12
fixtures as YAML, tenant security spec, provider contracts with verification dates; resolve G-DOKU, G-BS1–3, G-CW1,
G-PROC, G-MB1; accountant input request for G-ACC1/G-TAX1. Dependencies: none. Migrations: none. Tests: fixture
balance checker. Mock/live: n/a. DoD: docs reviewed, gates resolved or explicitly deferred with owner. Blockers: DOKU
docs access, accountant availability.

**M1 — Financially correct OMS core** (≈22 days)
Modules: org/legal entities/identity domains, capital (mandates/commitments), platforms + capability registry, catalog,
customers, quotes, orders + items + allocations, fulfillment groups, procurement (purchase lines), inventory (goods in
transit, counts), payments, settlements, reconciliation, ledger (periods, rules v2), approvals, audit, outbox +
Procrastinate. Migrations: **forward-only 0004+** (expand/backfill/contract, ADR-013/014) with composite FKs, roles
without BYPASSRLS; history of 0001–0003 preserved. Tests: F1–F12, invariants, RLS negative suite, concurrency (oversell, over-procurement).
Mock/live: all providers mocked. Controls: approvals, audit. DoD: acceptance tests 1, 2, 3, 4, 7, 8, 9, 11a, 13a, 16, 17, 19, 20 green (local + CI).
Deployment readiness: staging DB migration dry-run. Blockers: G-ACC1/G-TAX1 defaults configurable.

**M2 — WhatsApp commerce vertical slice** (≈20 days)
Modules: Meta webhook bridge (`wa_events`, process job, replay), Chatwoot API inbox adapter + mapping + echo
suppression, agent API + authority matrix + reply validator, n8n AI Sales Admin workflow, DOKU sandbox adapter (after
G-DOKU), Biteship mock/sandbox, human dropship copilot UI/API, CAPI outbox sender (mock), dashboard slices (orders,
approvals, procurement, exceptions). Tests: 5, 6, 14, 15 + E2E WhatsApp → order → payment → fulfil → ledger. Mock vs
live: Meta/Chatwoot/DOKU/Biteship mocked; sandbox only with explicit flag. DoD: E2E green, no duplicate sends/loops.
Blockers: WABA + Chatwoot instance on Sumopod; DOKU sandbox keys.

**M3 — FP&A & capital intelligence** (≈15 days)
Modules: waterfall views, L1–L3, budgets/forecasts, 13-week cash forecast, capital utilisation, simulator + stress,
risk alerts, bulk-buy evaluator, close/snapshots, Metabase. Tests: 12, 13b, 18 + S1–S9. DoD: Rp100M
scenario reproducible; alerts fire on breach.

**M4 — Marketplace & advertising expansion** (≈25 days)
Modules: Shopee, TikTok Shop (+Tokopedia & Shop), Lazada adapters (orders, fees, settlements), Meta/Google/TikTok/
marketplace ads insights, CSV import framework. Tests: 10, 11b + contract tests against recorded sandbox payloads.
Mock vs live: mocked until registry states `available`. Blockers: Shopee eligibility, TikTok ISV, Lazada app approval,
Google/TikTok app reviews.

**M5 — Production hardening** (≈15 days)
Security tests (RLS fuzzing, authz), integration contract tests, reconciliation verification on sandbox data, failure
recovery drills, observability, backup restore drill, controlled deployment runbook. DoD: RPO/RTO drill passed,
runbooks signed off. No mocked integration is labelled production-ready.

## 20. Acceptance-test matrix and invariants

| # | Test | Milestone |
|---|---|---|
| 1 | Multi-portfolio order with split fulfilment (owned + 2 suppliers) | M1 |
| 2 | Partial refund and partial return reconcile (stock, revenue, refunds) | M1 |
| 3 | COD revenue recognised at delivery before remittance; remittance clears receivable | M1 |
| 4 | DOKU prepayment recorded as contract liability until delivery | M1 |
| 5 | Duplicate and out-of-order webhooks (Meta, DOKU, Biteship, marketplaces) | M2 |
| 6 | Failed and retried jobs apply effects exactly once (worker killed mid-job) | M2 |
| 7 | Cross-organization and cross-portfolio access attempts (app, worker, webhook, reporting roles) | M1 |
| 8 | Multiple supplier purchases within one order; duplicate purchase attempts rejected | M1 |
| 9 | Overselling prevented under concurrent reservations across channels | M1 |
| 10 | Missing marketplace credentials → capability `not_authorized`, no calls | M4 |
| 11a | DOKU/COD/bank settlement failure or delay ages into exception with exposure | M1 |
| 11b | Marketplace settlement failure or delay ages into exception with exposure | M4 |
| 12 | Ad cost allocation change effective-dated; closed periods unchanged | M3 |
| 13a | Ledger period close and adjusting entries | M1 |
| 13b | FP&A snapshot immutability after close | M3 |
| 19 | F1–F13 executed through OMS services against PostgreSQL match fixture lines | M1 |
| 20 | Order-slice and reporting-role isolation (session_user identity, set_config/SET ROLE spoof attempts) | M1 |
| 14 | AI tool authorization failures logged; prompt-injection fixtures | M2 |
| 15 | WhatsApp outbound loop prevention (Chatwoot echo) | M2 |
| 16 | Capital reallocation creates no cash journal | M1 |
| 17 | Consolidated reconciliation (eliminations net to zero) | M1 |
| 18 | Stress test breaches liquidity reserve → alert | M3 |
| + | 3 demo portfolios (A FMCG FB→WA, Shopee dropship, Biteship option; B IG→WA, Tokopedia supplier, own budget; C Shopee + TikTok Shop native, shared SKUs) and the 10-step mocked scenario | M2–M3 |
Invariants checked after every test: journals balance; stock subledger = GL inventory + GIT; Σ order-item quantity
stages consistent; procured ≤ allocated; payment allocations = payment amount; external events deduplicated; contract
liabilities = undelivered prepaid value.

## 21. Contradiction register (resolved)

| # | Topic | Superseded text | Resolution |
|---|---|---|---|
| C1 | Meta webhook ownership | V3.0 §2 "Chatwoot WhatsApp inbox + OMS proxy + cron reasserts override"; `docs/03-architecture.md` proxy section | OMS sole owner; Chatwoot API-channel; read-only drift alert (§12) |
| C2 | COD accounting | V3.0 §5 "COD → revenue only after delivered + COD remittance matched" | Revenue at delivery; remittance clears receivable (§9) |
| C3 | Procurement uniqueness | V3.0 §5 "UNIQUE(order_id) on approved purchase"; code `UNIQUE(order_item_id)`, `UNIQUE(procurement_task_id)` | Quantity-aware purchase lines + DB qty invariant (§5, §8) |
| C4 | Single route per order | `orders.fulfillment_route`; router MANUAL_REVIEW on mixed sources | Fulfillment groups (§5, §8) |
| C5 | Portfolio security | V3.0 §4 "app-layer portfolio filter"; `oms_system` BYPASSRLS | DB-enforced visibility; no BYPASSRLS runtime roles; composite FKs (§7) |
| C6 | n8n runtime role | V3.0 "approvals via n8n Send-and-Wait"; compose "n8n dev only" vs Sumopod prod | Approvals in OMS only; n8n Sumopod prod + compose dev; n8n MCP build-time only (§2, §14) |
| C7 | Provider access assumptions | Mengantar API, Tokopedia legacy API, Google developer token, TikTok seller custom app for many shops | Registry states + gates (§6) |
| C8 | Capital reallocation | Code `execute_capital_reallocation` posts Cash/3100 journals | Mandate events only; intercompany only for real transfers (§10) |
| C9 | Job runner | ADR-003 "procrastinate dropped" | Outbox + Procrastinate (§2, D11) |
| C10 | Manual transfer verification | Code `verify_manual_transfer` → paid | reported → attested → bank_matched (§16, §11) |
| C11 | Customer advances account | `1300` typed liability inside asset range | `2200 Contract liabilities` (§9) |
| C12 | Identity scope | identities unique per brand | per identity domain + provider (§5) |
| C13 | Inventory at shipment | stock reduced at ship but GL inventory until delivery | Goods in transit 1250 (§9) |
| C14 | Plan §1 "Payments DOKU…to verify" vs adapters described as implemented | DOKU adapter not built | G-DOKU gate (§6) |
| C15 | Migration strategy | ADR-013 "rebaseline 0001–0003" | Forward-only migrations 0004+ with expand/backfill/contract (§27.2) |
| C16 | Unverified manual payments | Draft "1015 payments pending bank verification" asset | No entry/asset on receipt or attestation; 1105 receivable only for approved release-on-attestation (§27.3) |
| C17 | Reporting identity | GUC/`ALTER ROLE SET` identity; `current_user` mapping | `session_user` → `reporting_principals`, GUCs ignored for reporting roles (§27.7) |
| C18 | Simulator probabilities | "seeded Monte Carlo" in M3 | Deterministic first; Monte Carlo flag-gated until assumptions validated (§27.8) |
| C19 | Ninth stress scenario | Undefined / "combined downside" counted as #9 | S9 = marketplace settlement delay +14d with fee +2pp; combined downside is a preset (§27.8) |
| C20 | Partial order visibility | Order header visible if any line visible | Header only with org scope or full-line visibility; `order_slices` otherwise (§27.4) |
| C21 | DB role cutover | Remove `oms_system` in one step | Expand/contract with `OMS_DB_ROLE_MODE` flag (§27 round 3) |
| C22 | Acceptance-test ownership | Tests #11/#13 shared by two milestones | Split into 11a/11b and 13a/13b with single owners (§20) |

## 22. Implementation backlog

| Pri | Item | Module | Depends | Effort (d) | Test gate | External blocker |
|---|---|---|---|---|---|---|
| P0 | V3.1 forward migrations 0004–0007 expand/backfill/contract (legal entities, identity domains, composite FKs, roles w/o BYPASSRLS, resolvers) | migrations, core/db | M0 | 5 | RLS negative suite (#7) | Sumopod role privileges (G-OPS2) |
| P0 | Accounting rules v2 + periods + CoA renumber + F1–F12 | finance | baseline | 4 | #3 #4 #13 + invariants | G-ACC1/G-TAX1 defaults |
| P0 | Order items qty model, allocations, fulfillment groups, derived status | orders, fulfillment | baseline | 5 | #1 #2 | — |
| P0 | Procurement purchase lines + qty invariant + copilot API | procurement | orders | 3 | #8 | — |
| P0 | Capital mandates/commitments; remove cash journals from reallocation | capital | baseline | 2 | #16 | — |
| P0 | Payments state machine (attested/bank_matched) + bank statement import | payments, reconciliation | finance | 3 | #11 | bank CSV formats (BCA/Mandiri) G-BANK1 |
| P0 | Reconciliation module + exception linkage (exposure, ageing) | reconciliation, exceptions | payments | 4 | #11 #17 | — |
| P0 | WhatsApp bridge: wa_events persist-then-ack, process job, replay, mappings, echo suppression | messaging | outbox | 4 | #5 #15 | G-CW1 |
| P0 | Inventory GIT + counts + concurrency tests | inventory | orders | 2 | #9 | — |
| P1 | Procrastinate integration behind outbox interface | core/outbox, worker | baseline | 2 | #6 | G-PROC |
| P1 | Agent API + authority matrix + reply validator + injection fixtures | agent | quotes, messaging | 4 | #14 | — |
| P1 | n8n AI Sales Admin workflow + error workflow | n8n | agent | 3 | E2E | n8n MCP optional |
| P1 | DOKU adapter (sandbox) | integrations/doku | G-DOKU | 2 | contract tests | DOKU sandbox keys |
| P1 | Biteship adapter (sandbox) + COD remittance reconciliation | integrations/biteship | G-BS1–3 | 3 | F2 fixture | Biteship sandbox key |
| P1 | CAPI sender + insights sync (mock/CSV) + effective-dated allocations | conversions, marketing | finance | 4 | #12 | Meta app/system user |
| P1 | Capability registry + health UI | platforms, dashboard | baseline | 2 | #10 | — |
| P1 | FP&A waterfall, L1–L3, close/snapshots, cash forecast | fpa | finance | 6 | #13 | — |
| P1 | Simulator + stress tests + alerts + bulk-buy evaluator | fpa | waterfall | 5 | #18 | — |
| P1 | Dashboard (Next.js + Refine) 10 sections | dashboard | APIs | 8 | Playwright | — |
| P1 | Ops: logging/correlation, health, DLQ review, backups doc | deploy | — | 4 | drills | G-OPS1 |
| P2 | Shopee adapter | integrations/shopee | M1 | 5 | contract | shop eligibility |
| P2 | TikTok Shop / Tokopedia & Shop adapter | integrations/tiktokshop | M1 | 6 | contract | ISV |
| P2 | Lazada adapter | integrations/lazada | M1 | 4 | contract | app approval |
| P2 | Google Ads + Data Manager conversions | integrations/google_ads | M1 | 4 | contract | access level |
| P2 | TikTok Ads + marketplace ads | integrations/* | M1 | 4 | contract | app review |
| P2 | Landing page + Pixel/website CAPI | landing | conversions | 3 | dedupe test | domain |
| P1 | Reporting roles + `reporting_principals` + PII-free views; OMS portfolio reporting API | reporting | baseline | 2 | #7 (reporting role spoof test) | — |
| P1 | `external_operations` + provider idempotency policies + uncertain-outcome cases | core/outbox, integrations | outbox | 2 | worker-kill test | G-DOKU, G-BS4 |

## 23. Documentation set (to write/update after approval; plan mode allows only this file)

`docs/00-plan.md` (copy of this V3.1 plan) · `01-skills-audit.md` (keep) · `02-build-vs-reuse.md` (update: Procrastinate,
API-channel, capability registry) · `03-architecture.md` (rewrite overview; remove proxy) · `04-integration-feasibility.md`
→ `04-provider-capabilities.md` (registry states, verification dates, gates) · `05-meta-attribution.md` (align COD
Purchase/revenue) · `06-design-revisions.md` (mark superseded by V3.1 delta) · new: `07-adr.md` (ADR-001…010),
`08-erd.md`, `09-tenant-security-rls.md`, `10-state-machines.md`, `11-accounting-policy.md` (+ `fixtures/journals/*.yaml`),
`12-payments-settlement-reconciliation.md`, `13-whatsapp-api-channel.md`, `14-fpa-methodology.md`,
`15-capital-simulator.md`, `16-ai-authority-matrix.md`, `17-exception-workflows.md` (extend), `18-deployment-recovery.md`,
`19-milestones.md`, `20-acceptance-tests.md`, `21-risks-blockers.md`, `22-backlog.md`, `23-contradiction-register.md`.

## 24. Current implementation status and refactor impact

Pushed (commits `21262ce`, `16c62c5`, `308e81a`, 21 tests green): Phase 0 docs; schema + RLS + approvals + ledger +
outbox; quotes/authority, state machine, inventory, payments, routing, posting, exceptions, marketplace ingest.
Uncommitted WIP on branch: `oms/app/integrations/{whatsapp,chatwoot,factory}.py`, `oms/app/modules/{messaging,
conversions}.py` — will be committed as WIP then reworked in M2 (persist-then-ack, mappings, echo suppression).
Reusable as-is: config, security helpers, audit, approvals (+ tiers), quotes policy core, exceptions playbooks, CAPI
enqueue keys, posting helpers (`post_journal`, idempotency). Requires refactor (D1–D13): `models/*` (baseline),
`modules/org.execute_capital_reallocation` (remove cash journals), `modules/routing` (groups), `modules/orders` (items
qty, derived status), `modules/fulfillment` (GIT, per-shipment recognition), `modules/payments` (states),
`core/db` (roles), `core/outbox` (Procrastinate executor), `finance/accounts` (CoA renumber). Tests remain the
regression baseline and are rewritten where semantics change (COD, capital).

## 25. Open risks and verification gates

Gates (owner → due M0 unless noted): **G-DOKU** DOKU product/signature/notification/status APIs · **G-BS1–3** Biteship
COD remittance reporting, webhook auth, billing model · **G-CW1** Chatwoot API-channel callback semantics and echo fields
· **G-PROC** Procrastinate sync worker/periodic/lock features · **G-MB1** DB-role reporting identity (`session_user`); PostgreSQL isolation
tests pass locally (M1), Metabase instance configuration not yet verified · **G-BS4** Biteship lookup-by-reference_id · **G-ACC1**
accounting framework (SAK EP vs EMKM) and revenue trigger confirmation by accountant · **G-TAX1** PKP/PPN/PPh 22
treatment · **G-TAX2** record retention period · **G-BANK1** bank statement CSV formats · **G-OPS1** Sumopod PITR ·
**G-OPS2** Sumopod Postgres role privileges (non-superuser roles, definer functions) · Meta: Indonesia hashed fallback,
`test_event_code` on WABA dataset · TikTok ISV for own-group shops · Lazada SS app category.
Risks: marketplace account bans (mitigated: no automation); provider version churn (registry + contract tests);
financial misstatement from mis-set triggers (fixtures + accountant sign-off); Chatwoot/n8n operational load on Sumopod;
single-founder approval bottleneck (approver ≠ requester requires ≥2 approvers — needs a second authorised person or a
delegated approver policy **G-GOV1**).

## 26. Executive summary

1. **Preserved**: business model, three demo portfolios and 10-step scenario, modular monolith stack (FastAPI, Postgres,
   Next.js/Refine, Metabase, n8n, Chatwoot, Biteship, DOKU, Sumopod), all provider research and adapters, human-operated
   procurement, approvals, CAPI/attribution design, exception playbooks, 21-test regression baseline.
2. **Changed**: control-transfer revenue recognition with goods-in-transit and contract liabilities; multi-line,
   multi-group, partial fulfilment with item-level state machines; quantity-aware procurement; RLS without BYPASSRLS,
   composite tenant FKs, legal entities and identity domains; OMS-only WhatsApp ownership with persist-then-ack, mapping
   and echo suppression; capital mandates/commitments separate from cash; formal reconciliation with bank evidence;
   CM1–CM3 waterfall, close periods and snapshots; capability registry; AI authority matrix; production requirements;
   Procrastinate restored behind the outbox.
3. **Why**: financial truth (no premature/delayed revenue, no fake cash), real-world order shapes, tenant safety at the
   database layer, replay-safe messaging, and decision-quality FP&A tied to capital productivity.
4. **Remaining risks**: accounting/tax confirmation, provider access approvals, Chatwoot API-channel echo semantics,
   Sumopod DB privilege model, approver availability.
5. **Provider blockers**: DOKU contract unverified; Biteship COD remittance API unverified; Shopee shop eligibility;
   TikTok Shop ISV; Lazada approval; Meta WABA/system user; Google/TikTok app access.
6. **Consistency**: V3.1 is internally consistent after the contradiction register is applied; ready for phased
   implementation gated by M0.
7. **First milestone**: **M0 — Architecture & Provider Verification**: write the §23 documentation set, journal fixtures
   F1–F12 (F3a/F3b) as executable YAML with a balance checker, ERD and ADRs, work G-DOKU/G-BS/G-CW1/G-PROC, request
   accountant input (G-ACC1/G-TAX1); then M1 begins with forward migration 0004 (expand) and the RLS negative suite.

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
   Per-org login roles `rpt_<org_short>` (INHERIT, member only of `oms_reporting`, which holds nothing but the
   reporting-schema grants; `CONNECTION LIMIT`,
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
