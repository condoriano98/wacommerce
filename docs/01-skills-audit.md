# 01 — Existing AI Skills & Repository Audit

Status: **Implemented** (audit performed 2026-11-09 against live repos, installed skill files and official docs).

## Installed in this repository (`.claude/skills/`, pinned in `skills-lock.json`)

| Skill | Source / licence | Type | What we use | Gaps we build |
|---|---|---|---|---|
| `whatsapp-cloud-api` | bellopushon/whatsapp-cloud-api v1.1.0, MIT | Reference docs (9 refs), no code | Payload shapes, interactive/product messages, 24h window, template rules, `X-Hub-Signature-256`, error codes, Coexistence | Webhook receiver, sender, multi-WABA routing, window tracking. Skill examples use Graph v21/v25 — we use one configurable version (default v27.0). |
| `agent-browser` | vercel-labs/agent-browser, Apache-2.0 (CLI v0.27 installed; upstream 0.41.x) | External Rust CLI (CDP) | Dev/QA only (exploring our own dashboard) | **Not used against Shopee/Tokopedia** — marketplace ToS forbid automation; procurement is a human-operated copilot. |
| `profit-loss`, `cash-flow-forecast`, `break-even-calc`, `pricing-optimizer` | openaccountant/skills (no licence file in skill) | Agent instructions depending on "Wilson" tools (not available); cash basis; USD; single business | Formula/layout references; analyst narratives over CSV exports of our FP&A views | All FP&A computation is deterministic SQL/Python in `oms/app/modules/fpa` (accrual + cash, IDR, multi-portfolio). |
| `fastapi` | fastapi/fastapi `master`, MIT upstream | Conventions | `Annotated` + `Depends`, return types, `APIRouter`, `uv` + Ruff | DB layer, tenancy, idempotency, workers. |

## n8n-skills plugin (user scope, `n8n-skills@n8n-io` v1.3.0, Apache-2.0)

15 instruction skills + advisory hooks; every action needs the n8n **instance-level MCP**
(`<n8n_url>/mcp-server/http`, n8n ≥ 2.2.0). On the Sumopod instance MCP is **disabled** (`{"message":"MCP access is disabled"}`),
so workflows are authored as JSON in `n8n/workflows/` and imported manually.

Rules adopted: validate → verify before publish; credentials only via n8n credential system; Respond-to-Webhook on success
**and** error paths; explicit workflow-level error workflow; Code node as last resort; filter the bot's own messages.
Not adopted: Data Tables for business state (200 MiB cap; Postgres is the single source of truth); in-chat HITL approval cards
for money movements (they don't show arguments — "blind approval"). Authoritative approvals live in the OMS.

## External repositories evaluated

| Repository | Exists | Licence | Decision |
|---|---|---|---|
| n8n-io/n8n | ✔ (2.47.x) | Sustainable Use License — internal business use allowed | **Adopt** as orchestrator only |
| n8n-io/skills | ✔ | Apache-2.0 | **Adopt** (installed) |
| chatwoot/chatwoot | ✔ (v4.19.1) | MIT core; `enterprise/` paid (Captain AI, SLA, roles, SSO) | **Adopt** Community edition; n8n replaces Captain |
| bellopushon/whatsapp-cloud-api | ✔ | MIT | **Adopt** (reference) |
| vercel-labs/agent-browser | ✔ | Apache-2.0 | Dev/QA only |
| browser-use/browser-use | ✔ (0.13.x) | MIT | **Rejected** — LLM per step, non-deterministic, cloud offering sells CAPTCHA solving (ToS risk) |
| fastapi/fastapi | ✔ | MIT | **Adopt** |
| TheMattBerman/meta-ads-kit | ✔ | MIT | **Adopt** `meta-ads`, `budget-optimizer`, `pixel-capi` for analyst-side reporting/audits (approval-gated, PAUSED-only uploads) |
| facebook/agentic-tools | ✔ | Meta | **Adopt** `devtools` plugin for setup/diagnostics (webhook-setup, debug-access-token) — needs user OAuth to Meta MCP |
| silvanasolvers/meta-ads-skill | ✔ | MIT | **Not adopted** — ad-copy focus, overlaps meta-ads-kit |
| openaccountant/skills | ✔ | — | Reference only (see above) |
| microsoft/playwright | ✔ | Apache-2.0 | **Adopt** for testing our own dashboard/API flows |

## Overlaps resolved
- Webhook handling (WA skill, n8n, FastAPI) → **FastAPI owns the Meta webhook** (raw-body HMAC + idempotency in one place).
- Idempotency/dedupe (n8n Data Tables vs Postgres) → Postgres unique constraints.
- Human approval (n8n HITL vs OMS) → OMS `human_approvals`; n8n only notifies with a link.
- Margin math (pricing-optimizer vs profit-loss vs break-even) → one cost model in `fpa/`.
