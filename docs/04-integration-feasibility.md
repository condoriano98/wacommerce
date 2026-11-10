# 04 — Platform & API Feasibility Assessment

Researched 2026-11-09 from official developer portals. Status labels: **Mocked** (adapter + fake provider in repo) ·
**Blocked** (needs credentials/provider access) · **Manual** (no API; SOP/CSV).

| Provider | Role | Access path | Key constraints | Status |
|---|---|---|---|---|
| WhatsApp Cloud API (Graph v27.0) | messaging, sales | Meta app + system user; WABA | `X-Hub-Signature-256`; BSUID (`from_user_id`) since Apr 2026 — phone may be absent; `referral` on CTWA messages (`source_id` = ad ID, `ctwa_clid` absent for Status placements) | Mocked → Blocked (credentials) |
| Meta Marketing API v27.0 | advertising | `ads_read`, system-user token | `/act_{id}/insights level=ad time_increment=1`; data revises 28 days → daily 28-day re-pull; 7d/28d view windows removed Jan 2026; versions expire ~4 months after successor | Mocked + CSV import |
| Meta CAPI for Business Messaging | conversions | Dataset via `POST /{WABA_ID}/dataset`; `whatsapp_business_manage_events` | 14 supported events; **Meta does not dedupe** → our unique keys; ≤7 days old; hashed fallback when no `ctwa_clid` (Indonesia support unconfirmed) | Mocked |
| Meta website CAPI + Pixel | conversions | Pixel + dataset | Dedupe by event_name + event_id (48h); `fbc = fb.1.{ms}.{fbclid}` | Planned (landing page optional) |
| Chatwoot v4.19.1 | messaging UI | Self-hosted | Re-registers WA webhook override; phone and BSUID contacts not merged | Adapter + Mocked |
| Shopee Open Platform v2 | sales, ads | Seller In-house app (shop must be Mall/Managed/≥30 orders in 30d) | refresh token single-use (atomic rotation); 15-day windows; escrow detail for fees | Mocked → Blocked |
| Tokopedia | sales | **Legacy API shut 30 Sep 2025** → via TikTok Shop API as "Tokopedia & Shop" | No TikTok branding to Indonesian sellers; history only via CSV | Mocked + CSV |
| TikTok Shop Partner API | sales | Multi-shop requires ISV registration | access token 7d; `shop_cipher`; statements/transactions finance | Mocked → Blocked |
| Lazada Open Platform (ID) | sales | Seller In-house app | refresh 50d non-refreshable; push needs OV/EV TLS → polling | Mocked |
| Google Ads API v26.1 | advertising | Cloud-project access (no developer token since 9 Sep 2026) | GAQL daily; offline conversions via Data Manager API | Mocked |
| TikTok Ads API v1.3 | advertising | Company developer account, app review | async reports; GMV Max per store | Mocked |
| Biteship | shipping | API key | rates, orders (COD), tracking, webhooks | Mocked (sandbox-ready adapter) |
| DOKU | payments | Client-Id + secret key | Checkout payment link; signed notifications; re-check status | Mocked (sandbox-ready adapter) |
| Mengantar | shipping | **No documented API** | Dashboard only | Not integrated (replaced by Biteship) |
| Shopee / Tokopedia buyer side | procurement | **No buyer API; ToS forbid bots** (Shopee §3.1, §6.3(k)) | Human-operated copilot only | Manual (by design) |
| n8n instance MCP | build tooling | Settings → Instance-level MCP | Currently disabled on Sumopod | Blocked (owner must enable) |

## Unconfirmed items carried as risks
- Indonesia support for Meta BM CAPI hashed-identifier fallback (no `ctwa_clid`).
- `test_event_code` on WABA-linked datasets.
- Whether TikTok accepts an ISV that only serves its own group's shops.
- Lazada Sponsored Solutions API app category.
- DOKU notification signature details and Biteship COD remittance reporting (verification in progress; adapters stay **Mocked** until confirmed).
