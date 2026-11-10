# 23 — Contradiction Register

> Source of truth: `docs/00-plan.md` (canonical V3.1 specification incl. §27 normative corrections). This file is a topic view.


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
