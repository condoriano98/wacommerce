# 04 — Integration Capability Registry & Provider Verification

> Source of truth: `docs/00-plan.md` (canonical V3.1 specification incl. §27 normative corrections). This file is a topic view.


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

Adapter implementation status (M2, 2026-11-10): DOKU Checkout, Biteship orders, WhatsApp Cloud API send, Chatwoot
API channel and Meta BM CAPI have **mock implementations exercised by tests** plus documentation-derived live clients
covered only by offline respx contract tests (`oms/tests/test_m2_adapters.py`). None is sandbox- or
production-verified; every registry row stays `mocked` and no live client is selected without `live_enabled` +
credentials (none exist).

CSV fallback (all sales/ads/settlement/bank imports): schema-validated import batches, provenance (file hash, uploader,
source), row fingerprints for duplicate prevention, dry-run preview, reconciliation against existing records.

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
