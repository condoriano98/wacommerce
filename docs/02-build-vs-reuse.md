# 02 — Build-versus-Reuse Matrix

Legend: **Reuse** = adopt existing software/skill unchanged · **Adapter** = thin code against a documented API · **Build** = our
own domain code (no suitable OSS exists for this business logic).

| Capability | Decision | Component | Notes |
|---|---|---|---|
| Workflow orchestration, schedules, notifications | Reuse | n8n (self-hosted, Sumopod) | Never stores orders/money facts |
| Shared WhatsApp inbox, human handoff | Reuse | Chatwoot Community | Agent Bot → n8n; `toggle_status` handoff |
| AI Sales Admin conversation | Reuse + config | n8n AI Agent node + OMS tool endpoints | Prompt + tools; no custom LLM framework |
| WhatsApp Cloud API ingest | Adapter | `oms/app/modules/whatsapp` | Raw-byte HMAC, BSUID, referral capture, forward to Chatwoot |
| Order / customer / inventory / ledger | Build | `oms/app/modules/*` | Multi-portfolio OMS is the product's core |
| Multi-tenant RBAC | Build on Postgres RLS | `core/tenancy.py`, `governance` | Portfolio-scoped role assignments |
| Secrets | Reuse library | `cryptography` Fernet | Master key in env/secret manager |
| Durable jobs | Build (small) | Postgres outbox + `FOR UPDATE SKIP LOCKED` dispatcher | Replaces the planned `procrastinate` dependency: the transactional outbox already gives durable, idempotent delivery, so a second queue adds no value (ADR-003) |
| Meta Marketing API insights | Adapter | `integrations/meta` | v27.0 configurable; CSV fallback |
| Meta CAPI (Business Messaging + website) | Adapter + Build policy | `conversions/` | Our own dedupe (Meta doesn't dedupe BM events) |
| Meta reporting narratives / audits | Reuse | `meta-ads-kit` skills | Analyst-side, approval-gated |
| Google Ads / TikTok Ads | Adapter (mock) | `integrations/google_ads`, `integrations/tiktok_ads` | Generic `AdsProvider` interface |
| Shopee / TikTok Shop (+Tokopedia) / Lazada seller | Adapter (mock) | `integrations/{shopee,tiktokshop,lazada}` | Aggregators rejected (weak fee/settlement data) |
| Courier (owned stock) | Adapter | `integrations/biteship` | Mengantar has no documented API |
| Payment links | Adapter | `integrations/doku` | Webhook = trigger; re-check status |
| Dropship procurement | Build (copilot UI/API) | `procurement/` | Human-operated; no marketplace automation |
| FP&A computation | Build | `fpa/` | openaccountant skills = formula references |
| Analytics dashboards | Reuse | Metabase OSS on read-only views | |
| Admin dashboard | Reuse framework | Next.js + Refine | Refine provides CRUD/data-grid/auth hooks |
| Testing | Reuse | pytest, httpx TestClient, Playwright | |
