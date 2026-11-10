# Plan — Ecommerce Portfolio Management & FP&A OS (wacommerce) — V3.0

## Context
The founder runs several product portfolios, sells first through WhatsApp (acquired via Meta Ads, mostly Click-to-WhatsApp)
and later natively on Shopee / Tokopedia / TikTok Shop / Lazada, with Google as another acquisition channel. Fulfillment is
**A** owned stock shipped via a courier aggregator (Biteship) or **B** human-operated dropship purchases on Shopee/Tokopedia. Capital (illustrative Rp100M) must
be allocated across portfolios, platforms, campaigns and fulfillment methods on measured contribution, liquidity and risk.
There is no system today: repo `condoriano98/wacommerce` holds only installed skills (`.claude/skills/*`, `skills-lock.json`).
n8n runs on Sumopod (`https://n8n-4bqpb9jbfunb.jkt2.sumopod.my.id`, instance MCP **disabled**).
Outcome of this build: architecture + audit docs, a working multi-tenant OMS/ledger/FP&A core, dashboard, and **mocked**
end-to-end flows for the 3 demo portfolios. Never: live purchases, paid shipments, ad-budget changes, refunds, fund moves.

**User decisions:**
- Payments = **DOKU** payment link + manual transfer (admin-confirmed) + COD (via Biteship).
- Engine A shipping = **Biteship API** (replaces Mengantar: Mengantar has no documented API). Carrier adapter interface kept
  so Mengantar (manual SOP) or KiriminAja can be added later.
- Engine B procurement = **human-driven copilot**: no bot/automation touches Shopee/Tokopedia (their ToS forbid it).
- Hosting = Sumopod managed apps (Postgres, Chatwoot, Metabase) + OMS api/worker containers; Docker Compose for dev/CI.
- WhatsApp = fresh Cloud API onboarding, new Chatwoot.

Deliverable status labels (V3 §23): **Implemented & tested · Implemented, not externally verified · Mocked · Planned ·
Blocked (credentials/provider access)**.

---

## 1. Reuse vs build (Phase 0 → `docs/01-audit.md`, `docs/02-build-vs-reuse.md`)
| Capability | Reuse | Build (adapter/glue only) |
|---|---|---|
| Orchestration, schedules, approvals UI-in-chat | n8n + n8n-skills plugin (Apache-2.0); n8n = orchestration only, never order storage | Workflow JSON in `n8n/workflows/` |
| WhatsApp API | `whatsapp-cloud-api` skill (instructions) | Webhook ingest + sender adapter |
| Shared inbox, handoff | Chatwoot (MIT core) | HTTP adapter; Agent Bot → n8n |
| Meta reporting / audit | `meta-ads-kit` (`meta-ads`, `budget-optimizer`, `pixel-capi`) + Meta `meta-ads` CLI; `facebook/agentic-tools` devtools for setup/diagnostics | Insights ingestion, CAPI outbox |
| Ad copy | `silvanasolvers/meta-ads-skill` → **not adopted** (overlap) | — |
| Backend | FastAPI (+skill), SQLAlchemy 2, Alembic, Pydantic v2, **procrastinate** (Postgres-backed durable jobs; no extra broker) | All domain modules |
| Auth / RBAC | argon2 + PyJWT in FastAPI; Postgres RLS as defense-in-depth | Scoped role assignments |
| Secrets | Fernet envelope encryption (`cryptography`) with master key in Sumopod env | `account_credentials` vault |
| Marketplace procurement | **Copilot mode (user decision)** — no browser automation on Shopee/Tokopedia. agent-browser and browser-use evaluated; **neither used against marketplaces** (browser-use: LLM per step + CAPTCHA-solving cloud = ToS risk; agent-browser kept as a dev/QA tool only). Playwright used only for testing our own dashboard | OMS procurement task UI (deep link to approved listing, expected variant/price/ceiling, checklist, operator-entered observations, approval, external order + evidence capture) |
| Payments | **DOKU** (user decision) — Checkout / payment-link API with signed HTTP notifications; exact endpoint, signature scheme (HMAC-SHA256 over Client-Id/Request-Id/Request-Timestamp/Request-Target/Digest — **to verify against developers.doku.com in Phase 0**) and sandbox | `DokuAdapter` (mock + sandbox-flagged live); webhook = trigger only → re-check status via API; notification IDs UNIQUE |
| Shipping (Engine A) | **Biteship API** (user decision): rates, orders with COD (`destination_cash_on_delivery`), tracking, cancel, webhooks, sandbox. Mengantar has no documented API → not integrated | `BiteshipAdapter` (mock + sandbox-flagged live) behind a `CarrierAdapter` interface; COD remittance reconciled from Biteship reports (method to verify in Phase 0) |
| Admin dashboard | **Next.js + Refine (MIT) + shadcn/ui** (CRUD/data-grid/RBAC hooks) | Pages for 10 sections |
| Analytics | **Metabase OSS** on read-only FP&A views, embedded in Next.js | Views + saved questions |
| FP&A narratives | openaccountant skills (`profit-loss`, `cash-flow-forecast`, `break-even-calc`, `pricing-optimizer`) — audit: they depend on "Wilson" tools (unavailable), cash-basis, USD, single-business → used only as **formula/layout references** and for analyst narratives on CSV exports of our views | Deterministic SQL views + simulator in OMS (accrual + cash, IDR, multi-portfolio) |

### 1b. Skills audit conclusions (local inspection)
- All repo skills are instructions except `agent-browser` (Rust CLI v0.27.0 installed). Nothing covers Chatwoot, Mengantar,
  Meta CAPI or marketplaces → those are custom adapters.
- `whatsapp-cloud-api` references Graph v21/v25 → we use one configurable version (default v27.0, matching Meta research).
- `fastapi` skill conventions adopted (Annotated/Depends, return types, APIRouter, `uv` + Ruff); its gaps (DB, tenancy,
  idempotency, workers) are ours.
- n8n-skills rules adopted: validate→verify before publish, credentials only via n8n credential system, Respond-to-Webhook
  on success+error paths, workflow-level error workflow, Code node last resort, filter bot's own messages. n8n Data Tables
  **not** used for business state (Postgres is the single source of truth).
- **Approvals**: n8n HITL cards don't show arguments ("blind approval") and there is no WhatsApp approval node →
  authoritative approvals live in the OMS dashboard (shows amount, supplier, order, approver identity); n8n only notifies
  (Telegram/Slack/WhatsApp template link to the approval page). Approval-gated tools are never placed inside sub-workflows.
- agent-browser hardening if selected: `AGENT_BROWSER_ENCRYPTION_KEY` for saved state, `--session` per platform account,
  domain allowlist enforced by our wrapper, `confirm` dialogs never auto-accepted, HAR/video kept as procurement evidence.
- Meta webhook receiver = FastAPI (not n8n), so raw-body HMAC + idempotency live in one place.
| Marketplace seller APIs | Official APIs direct (see §1a); aggregators (Jubelio/Ginee) **not adopted** — weak fee/settlement data, no ads | `SalesChannelAdapter` per provider; CSV import fallback |
| Ads (Google/TikTok/marketplace ads) | Official APIs (see §1a) | `AdsProviderAdapter`; CSV fallback |

### 1a. Seller & ads integration decisions (verified research, 2026-11-09)
| Provider | Path | Key constraints baked into adapter | Initial status |
|---|---|---|---|
| Shopee Open Platform v2 | Direct, **Seller In-house** app per selling entity (ERP type lacks Ads API) | access 4h / refresh 30d **single-use** → atomic rotation (`SELECT … FOR UPDATE` per account); auth ≤365d + expiry push; 15-day windows for orders/returns; push + `get_lost_push_message`; fees from `get_escrow_detail(_batch)`, cash from `get_escrow_list`/`get_income_detail`; Ads via `v2.ads.*` | Mocked → Blocked until shop eligible (Mall/Managed/≥30 orders/30d) |
| Tokopedia | **No legacy API** (shut 30 Sep 2025) → via TikTok Shop API ("Tokopedia & Shop", platform field TOKOPEDIA/TTS); history via Seller Center CSV import | UI/labels must say "Tokopedia & Shop" (no TikTok branding to ID sellers); dual listing since 29 Oct 2026 | Mocked + CSV |
| TikTok Shop Partner API | Multi-shop needs **ISV** registration (company cert, non-seller email); seller Custom App = one shop only | access 7d; `shop_cipher`; per-endpoint month versions; webhooks 1/4/11/12/64/67/27/71/6/7/35; finance = statements + transactions-by-order (PPh 22, affiliate, GMV Max fees); dynamic QPS, 429/36009002 backoff | Mocked → Blocked on ISV/partner access |
| Lazada Open Platform (ID) | Direct, **Seller In-house APP** | token 10d / refresh 50d non-refreshable → re-auth reminders; push needs OV/EV TLS → **polling fallback**; at-least-once push → dedupe; fees = transaction lines aggregated per order item | Mocked |
| Google Ads API v26.1 | Direct; Cloud-project access (Explorer auto-approved; no dev token since 9 Sep 2026); manager account for multi-account | GAQL `SearchStream` daily; offline/enhanced conversions via **Data Manager API** (not UploadClickConversions); landing page captures gclid/gbraid/wbraid before WhatsApp handoff; consent field | Mocked → Planned live |
| TikTok Ads Marketing API v1.3 | Direct (company email + site, app review) | async reports (sync truncates at 20k ads); GMV Max report one store/call; offline events API | Mocked |
| Marketplace ads | Shopee Ads API; Lazada Sponsored Solutions API (app category unconfirmed); TikTok GMV Max via Marketing API; Tokopedia TopAds → CSV | — | Mocked / CSV |
Version strings, token lifetimes and windows live in `platform_accounts.api_version` / provider config, never hard-coded.
| Tests | pytest, testcontainers, respx, Playwright | Suites + synthetic data |

## 2. Architecture (modular monolith)
```
Portfolio Manager (Next.js+Refine) ─► OMS API (FastAPI) ◄─► PostgreSQL (tenant-scoped, RLS)
Platform adapters (registry-driven, one instance per platform_account):
  messaging: WhatsApp Cloud (multi-WABA/number) ─► /webhooks/whatsapp/{platform_account} ─► forward ─► Chatwoot inbox*
  sales:     WhatsApp | Shopee | TikTokShop/Tokopedia | Lazada  (order pull/push → unified orders, CSV fallback)
  ads:       Meta (v27.0 configurable) | Google | TikTok | marketplace ads (daily sync, 28d re-pull)
  procurement: Shopee/Tokopedia copilot tasks (human-operated)   shipping: Biteship   payments: DOKU | manual | COD
OMS state changes ─► outbox ─► procrastinate workers: CAPI sender, notifications (n8n webhooks), fulfillment, sync jobs
FP&A views ─► Metabase / Next.js ─► recommendations + risk alerts (recommend-only) ─► human approvals
```
\* **Verified webhook topology** (Chatwoot v4.19.1, MIT core; Captain AI is paid → n8n replaces it):
1. Meta app callback **and** phone-number-level override → OMS `POST /webhooks/whatsapp` (per WABA/number via
   `metadata.phone_number_id` → `platform_accounts`). OMS reads raw bytes first, verifies `X-Hub-Signature-256`, persists the
   raw payload (referral, ctwa_clid, `from_user_id` BSUID, statuses/pricing, tracking events), returns 200 fast.
2. Outbox job forwards the **identical bytes** + `X-Hub-Signature-256` + `Content-Type` to Chatwoot
   `/webhooks/whatsapp/{phone_number}` (Chatwoot holds the same App Secret; dedupes by wamid). Retries are durable because
   Meta won't resend after our 200.
3. Chatwoot **re-registers its own phone-number override** on inbox creation / voice toggle → OMS cron reasserts our
   override (`GET /{phone_number_id}?fields=webhook_configuration`) and alerts on drift (Integration Health page).
4. Chatwoot Agent Bot `outgoing_url` → n8n webhook (verify `X-Chatwoot-Signature` over `"{ts}.{body}"`) → AI Sales Admin →
   OMS API → reply via Chatwoot messages API (`private:true` for notes; `input_select` → WhatsApp interactive);
   handoff = `toggle_status {status:"open"}`; n8n ignores non-`pending` conversations and the bot's own messages.
5. Chatwoot account webhook → OMS (agent replies, `conversation_id ↔ order` link). All outbound customer messages,
   including OMS-owned order/payment templates, go **through Chatwoot's API** so the inbox history stays complete.
- Chatwoot keeps phone and BSUID as separate contacts (no auto-merge) → OMS identity table is the merge authority.
- No official n8n Chatwoot node → HTTP Request nodes with a custom credential. Candidate template to adapt:
  n8n.io/workflows/16039 (Chatwoot bot → AI/human handoff).
- n8n: Sustainable Use License (internal use OK); Data Tables capped (200 MiB) → not used for business state; always set an
  **explicit** error workflow (avoid "No Workflow" — [n8n#40824](https://github.com/n8n-io/n8n/issues/40824), closed
  2026-10-20, 2 comments); integration test that HTTP Request error outputs route correctly on our n8n version
  ([n8n#35899](https://github.com/n8n-io/n8n/issues/35899), closed 2026-08-13, 4 comments).
- n8n instance MCP: enable at Settings → Instance-level MCP (owner/admin), per-workflow opt-in; currently disabled on Sumopod
  — only needed for Claude-driven workflow building, not for runtime.
Processes: `postgres`, `api`, `worker`, `chatwoot`(+redis, sidekiq), `metabase`, `dashboard` (Next.js), `n8n` (dev only),
`mocks` (fake Meta / Biteship / DOKU / marketplace seller APIs / Google / TikTok).

## 3. Repository layout
```
docs/                 01-audit … 23-backlog (one file per V3 §23 deliverable, each with status labels)
oms/app/
  core/               config, db (async), tenancy (request scope → SET app.org_id for RLS), authz, audit, outbox, idempotency
  modules/
    org/              organizations, brands, portfolios, portfolio_members, budgets, capital_allocations, risk_policies
    platforms/        providers, platform_accounts, account_credentials (encrypted), account_portfolio_links, sync_runs
    catalog/          products, skus, portfolio_skus, platform_listings (per-channel price/fees)
    suppliers/        suppliers, supplier_offers, price history
    customers/        customers, customer_identities (BSUID/phone/wa_id), addresses, privacy_preferences, consent_events
    attribution/      customer_attribution touches, landing_sessions
    orders/           orders, order_items (line-level portfolio_id), order_events, state machine
    payments/         payments, refunds (provider interface: gateway/manual/cod)
    inventory/        warehouses, inventory_balances, movements, reservations, transfers (inter-portfolio w/ journal)
    fulfillment/      router, fulfillment_tasks, shipments, shipment_events, returns
    procurement/      procurement_tasks, agent_tasks, human_approvals, external purchase refs
    marketing/        ad_accounts, campaigns, ad_groups, advertisements, daily_ad_insights, campaign_portfolio_allocations
    conversions/      conversion_outbox, conversion_delivery_logs, eligibility policy
    finance/          ledger (double-entry), financial_transactions, marketplace_settlements, reconciliations, opex, budget_actuals
    fpa/              views, snapshots, simulator, rebalancing & bulk-buy recommender, risk alerts
    governance/       users, role_assignments, approvals, audit_logs, integration_errors, agent_execution_logs
  integrations/<provider>/{base.py (Protocol), mock.py, live.py}   live disabled by default per account flag
oms/migrations/ (Alembic) · oms/tests/ · oms/seed/demo_portfolios.py
dashboard/ (Next.js + Refine) · dashboards/metabase/ · n8n/workflows/ · e2e/ (Playwright) · deploy/
```

## 4. Data model rules (DDL in Alembic; ERD in `docs/06-erd.md`)
- **Tenancy**: every business table carries `organization_id`; portfolio-scoped tables also `portfolio_id`. Postgres RLS
  policies on `current_setting('app.org_id')` + app-layer portfolio filter from the caller's role assignments. Tests prove
  Portfolio-A users cannot read Portfolio-B customers, orders, credentials.
- **Hierarchy & many-to-many**: org → brands → portfolios; `portfolio_skus`, `account_portfolio_links`,
  `campaign_portfolio_allocations(campaign_id, portfolio_id, method, share)`; warehouses shared; `order_items.portfolio_id`.
- **Platform registry**: `platform_accounts(provider, provider_account_id, role[advertising|sales|messaging|procurement|
  shipping|payments], status, credential_ref, scopes, api_version, rate_limit_cfg, webhook_status, last_sync_ok/failed,
  UNIQUE(organization_id, provider, provider_account_id))`; secrets only in `account_credentials` (encrypted, never serialized).
- **Identity**: `customer_identities(identity_type, identity_value, UNIQUE(business_id, type, value))`; BSUID primary,
  phone optional; no cross-business merges; merges recorded (`merged_into_customer_id`).
- **Orders**: `UNIQUE(platform_account_id, external_order_id)`; `order_events` append-only; financial facts never overwritten.
- **Attribution**: one `customer_attribution` row per touch (`UNIQUE(inbound_message_id)`), `ad_id = referral.source_id`
  validated against ad accounts owned by the org; `orders.attribution_touch_id` frozen at confirmation; methods
  `ctwa_referral | landing_ref_code | platform_native | manual | unattributed`.
- **Conversions**: `conversion_outbox(event_id stable, subject_type, subject_id, event_type, destination, occurrence_key,
  event_time, status[pending|sent|failed|expired|suppressed], UNIQUE(subject_type, subject_id, event_type, destination,
  occurrence_key))`; retries reuse event_id; delivery logs per attempt.
- **Ledger**: double-entry `ledger_entries` (balanced per journal) with `portfolio_id`, `platform_account_id`, `order_item_id`;
  accrual vs cash separated (marketplace receivable ≠ cash until settlement matched); inter-portfolio transfers as paired
  journals eliminated in consolidation; unattributed ad spend kept in its own account (never silently spread).

## 5. Key behaviours
- **Order state machine** (`docs/07`): placed → confirmed → payment_pending → payment_verified | cod_confirmed →
  routed → (A) reserved → packing → packed → shipped → delivered → settled; (B) procurement_pending → awaiting_approval →
  purchased → shipped → delivered → reconciled; plus canceled / returned / refunded / exception. Native marketplace orders
  follow the platform's own fulfillment rules (router returns `PLATFORM_MANAGED` or MANUAL_REVIEW, never auto-Biteship).
- **Payments**: DOKU link + signed notification → status re-checked → verified; manual transfer → AI pre-reads receipt →
  human confirms; COD (Biteship) → revenue only after delivered + COD remittance matched.
- **Router** (deterministic, decision + inputs persisted) returns `OWNED_STOCK_BITESHIP | SHOPEE_DROPSHIP |
  TOKOPEDIA_DROPSHIP | PLATFORM_MANAGED | MANUAL_REVIEW`: channel restrictions → owned stock (shared, reservation-locked, no
  oversell) → approved supplier offer within ceiling → contribution ≥ floor → cash ≥ reserve → else MANUAL_REVIEW.
- **Engine A (Biteship)**: reserve → packing queue → packed → rate quote → booking (live call only when the account's live
  flag is on **and** a human confirmed paid shipments) → AWB → webhook/poll tracking → delivered/failed/returned → COD
  remittance reconciliation.
- **Engine B (procurement copilot, human-operated)**: OMS creates a task listing the approved supplier offers (deep links),
  expected variant, qty, price ceiling and destination; operator opens the listing in their own browser and records the
  observed price/stock/shipping; OMS computes landed cost + contribution and blocks if above ceiling or below floor;
  approval by an authorized approver (separation of duties above a configurable amount) naming user + amount; operator buys
  manually, then enters external order ID + receipt screenshot; per-order lock + `UNIQUE(order_id)` on approved purchase +
  `UNIQUE(marketplace, external_order_id)` prevent duplicates; tracking entered/updated manually with reminders; price
  drift, out-of-stock, substitution requests → MANUAL_REVIEW. No credentials of buyer accounts are stored by the OMS.
- **Meta/CAPI**: per earlier verified research — Business Messaging CAPI (14 supported events; Purchase as optimization
  candidate), our own dedupe, 7-day window, consent-gated at send time, Pixel/website CAPI dedupe by event name+ID,
  insights `level=ad, time_increment=1`, daily 28-day re-pull, version configurable, CSV fallback. Three measurement
  columns kept separate: Meta-reported, internally attributed, financially realized.
- **Approvals** (`human_approvals`): procurement purchase, refunds, capital reallocation, price changes beyond policy,
  budget increase recommendations, credential changes, ledger adjustments — via dashboard or n8n Send-and-Wait; idempotent.
- **FP&A**: L1 SKU×platform, L2 portfolio, L3 consolidated (eliminating inter-portfolio transfers); contribution formula;
  break-even CPA/ROAS; cash conversion cycle; budget vs actual; risk alerts (reserve Rp12M, concentration 35%, op margin 5%,
  below break-even ROAS, supplier price jump, slow turnover, delayed settlement, high returns, duplicates); simulator and
  dropship→bulk evaluator (risk-adjusted: demand stability, savings, carrying cost, sell-through, downside, post-buy
  liquidity); rebalancing recommendations — recommend-only. Rp100M allocation seeded as an editable scenario.

## 6. Build order (this implementation)
Commit and push to `claude/intelligent-cori-6qujr0` after each step with tests green, so progress survives the ephemeral
container. Every external adapter ships mock-first; live mode requires credentials in the registry + an explicit per-account
flag, and irreversible calls (paid shipment booking, refunds, ad changes) additionally require a recorded human approval.
1. Phase 0 docs (audit, matrix, API feasibility, architecture, backlog) from completed research.
2. Foundation: compose, FastAPI core, tenancy/RLS, auth + role assignments, org/portfolio/budget/risk-policy, platform
   registry + encrypted credentials, audit, outbox + procrastinate worker.
3. Commerce core: catalog/listings, suppliers, customers/identity, orders + state machine, payments, inventory, router, ledger.
4. WhatsApp ingest + Chatwoot adapter + attribution + consent; n8n AI Sales Admin workflow (mock tools).
5. Meta: insights sync (mock + CSV), CAPI outbox sender (mock, live-flagged).
6. Biteship adapter (mock + sandbox-flagged) + packing flow + COD reconciliation; DOKU adapter (mock + sandbox-flagged);
   procurement copilot tasks + approvals.
7. Sales-channel adapters (mock Shopee native, TikTok Shop native) + settlement ingestion/reconciliation (mock + CSV).
8. FP&A views, simulator, recommender, risk alerts; Metabase; Next.js dashboard (10 pages).
9. Seed 3 demo portfolios; pytest + Playwright scenarios; SOPs; backlog; commit & push to `claude/intelligent-cori-6qujr0`.

## 7. Verification
- `docker compose up` healthy; `alembic upgrade head` clean; `pytest` green on Postgres testcontainer.
- Unit/integration: state-machine guards; duplicate webhook / polled order / Purchase / procurement → single record;
  identity merge; CAPI eligibility (no consent, >7d, missing ctwa_clid); oversell across channels; ledger balances; COD
  mismatch; settlement reconciliation differences; inter-portfolio transfer elimination; RBAC isolation (A cannot see B);
  credentials never in API responses or logs.
- Playwright e2e (mock mode) — the 10-step V3 §22 scenario across Portfolio A (FMCG, FB→WA, Shopee dropship copilot, Biteship owned-stock option),
  B (IG→WA, Tokopedia supplier, own budget), C (Shopee + TikTok Shop native, shared SKUs, separate fees/settlements),
  ending with risk alerts and a capital-reallocation recommendation.
- Live checks only with explicit approval (Meta Test Events, webhook verification).

## Research-driven constraints (verified 2026-11-09)
- **Marketplace ToS**: Shopee Syarat Layanan §3.1 (no robots/automated monitoring or copying) and §6.3(k) (no bots);
  Tokopedia terms prohibit emulators/robots/macros/crawlers; robots.txt disallows `/cart/`, `/checkout/`. No official buyer
  API gives per-variant price/stock/shipping (Shopee Affiliate API = price ranges only, also bans automated querying).
  → User chose human-driven copilot mode: zero automation on marketplace buyer sites.
- **Mengantar**: no documented API → replaced by Biteship (user decision).
- **To verify in Phase 0 before coding live adapters**: DOKU payment-link endpoint + notification signature; Biteship COD
  remittance/settlement reporting and webhook signature. Until verified they are labeled "Mocked".
