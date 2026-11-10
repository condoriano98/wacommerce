# 24 — V3.0 → V3.1 Delta & Executive Summary

> Source of truth: `docs/00-plan.md` (canonical V3.1 specification incl. §27 normative corrections). This file is a topic view.


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
