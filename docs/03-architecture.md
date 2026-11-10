# 03 — Architecture Overview (V3.1)

> Source of truth: `docs/00-plan.md` §1–§4. Supersedes the V3.0 version of this file, including the Chatwoot
> WhatsApp-inbox proxy design (contradiction C1).

## Purpose
A modular-monolith operating system that lets one founder run several e-commerce portfolios from a constrained capital
pool, optimising **risk-adjusted contribution, capital productivity, cash conversion, reliability and liquidity**.

## Layers

```mermaid
flowchart TB
    PM["Portfolio & Capital Manager<br/>(mandates, commitments, approvals)"]
    PM --> P1["Portfolio A"] & P2["Portfolio B"] & PN["Portfolio N"]
    P1 & P2 & PN --> REG["Platform accounts + Integration Capability Registry"]
    REG --> WA["WhatsApp (Meta CTWA)"] & MP["Shopee · Tokopedia & Shop / TikTok Shop · Lazada"] & ADS["Meta · Google · TikTok · marketplace ads"]
    WA & MP --> OMS["Unified OMS<br/>orders · items · fulfillment groups"]
    OMS --> RT{"Router (per fulfillment group)"}
    RT --> A["Engine A: owned stock → Biteship (COD)"]
    RT --> B["Engine B: human-operated dropship (copilot)"]
    RT --> PMF["Platform-managed fulfilment"]
    A & B & PMF --> LED["Double-entry ledger (accrual, periods)"]
    PAY["DOKU · manual transfer · COD · marketplace payouts"] --> REC["Reconciliation (expected vs actual, evidence, ageing)"]
    REC --> LED
    ADS --> FPA["FP&A: CM1–CM3, cash forecast, simulator"]
    LED --> FPA
    FPA --> REC2["Advisory recommendations → human approvals"]
    REC2 -.-> PM
```

## Runtime components (Sumopod)

| Component | Responsibility | Authoritative for |
|---|---|---|
| `oms-api` (FastAPI) | REST API, webhook receivers, commands, RBAC | — (stateless) |
| `oms-worker` (Procrastinate) | outbox relay, provider sync, CAPI, reconciliation, periodic jobs | — |
| PostgreSQL 16 | all business, financial, attribution data; RLS | **System of record** |
| Chatwoot (web + Sidekiq + Redis + object storage) | API-channel shared inbox, human handoff UI | Nothing financial |
| n8n | AI Sales Admin orchestration, notifications, schedules | Nothing (calls OMS commands) |
| Next.js + Refine dashboard | operations, approvals, finance UI | — |
| Metabase | read-only analytics on views | — |

## WhatsApp topology (single owner)
Meta → OMS webhook (persist raw event, commit, 200) → processing job → Chatwoot API-channel mirror → Agent Bot → n8n →
OMS policy-checked commands → OMS send → Meta. Human replies from Chatwoot reach Meta only through OMS send commands;
mirror echoes are suppressed. Details: `13-whatsapp-api-channel.md`.

## Cross-references
ERD `08-erd.md` · security `09-tenant-security-rls.md` · state machines `10-state-machines.md` · accounting
`11-accounting-policy.md` · reconciliation `12-…` · FP&A `14-…` · simulator `15-…` · AI `16-…` · exceptions `17-…` ·
ops `18-…` · ADRs `07-adr.md`.
