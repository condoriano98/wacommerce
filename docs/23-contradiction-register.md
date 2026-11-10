# 23 — Contradiction Register (V3.0 → V3.1)

> Source of truth: `docs/00-plan.md` (V3.1, approved 2026-11-10). This file is the topic view of the listed sections.


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
