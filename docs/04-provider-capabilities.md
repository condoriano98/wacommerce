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
| Chatwoot v4.19.1 | API channel inbox; Agent Bot events; `toggle_status`; `X-Chatwoot-Signature` over `{ts}.{body}` ([api](https://developers.chatwoot.com/api-reference)) | mocked | self-host on Sumopod; API-channel callback echo semantics **to verify (G-CW1)** |
| DOKU | Product/endpoint, signature scheme, notification format, status re-check API | mocked | **G-DOKU: unverified** (verification task lost on worker restart) — must be verified against developers.doku.com before any sandbox call |
| Biteship | Orders API with `destination_cash_on_delivery`, rates, tracking, cancel, webhooks, sandbox ([webhook](https://biteship.com/docs/api/webhook/overview)) | mocked | **G-BS1**: COD remittance/withdrawal reporting API vs dashboard-only; **G-BS2**: webhook authentication; **G-BS3**: balance (saldo) vs invoiced billing model |
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
