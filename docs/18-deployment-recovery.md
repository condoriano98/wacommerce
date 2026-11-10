# 18 — Deployment, Operations & Recovery

> Source of truth: `docs/00-plan.md` (V3.1, approved 2026-11-10). This file is the topic view of the listed sections.


## 18. Operations, security, reliability (new — P1)

- **Environments**: local dev (compose, mocks), integration test (CI, testcontainers, mocks), sandbox (provider sandboxes
  where available; `live_enabled=false` except sandbox flags), production (Sumopod). No live integration enabled by
  default; live requires credentials + registry state `available` + explicit flag + approval for irreversible calls.
- **Secrets**: Sumopod secret env for master keys; credential key versions + rotation job; revocation; no secrets in
  logs/prompts/repo.
- **Data**: private object storage for media/evidence (signed URLs, retention per purpose); PII access audited; retention
  schedule (chat 24 months, evidence 10 years for financial records per tax law **G-TAX2 verify**).
- **Backup/recovery**: Sumopod Postgres PITR (**G-OPS1 verify availability**), daily logical backups off-site; targets
  RPO 15 min, RTO 4 h; quarterly restore drill.
- **Jobs**: leases, retry with backoff, dead-letter review queue, poison-message alerts; outbox guarantees
  (at-least-once + idempotent handlers); webhook replay protection (dedupe keys, timestamp windows).
- **Integrations**: circuit breakers per account, rate-limit budgets from registry, backoff on 429.
- **Observability**: structured JSON logs with correlation IDs (request → job → provider call), metrics, health checks
  (api, worker, DB, outbox lag, webhook lag, reconciliation backlog), alerts.
- **Delivery**: migrations forward-only with tested downgrade where safe; expand/contract for schema changes; release
  checklist; Chatwoot (web + Sidekiq + Redis + storage) and n8n sized and monitored.

## 2. Technology stack and runtime (preserved, clarified)

| Layer | Choice | Notes |
|---|---|---|
| Backend | FastAPI, SQLAlchemy 2 (sync, psycopg 3), Alembic, Pydantic v2 | Modular monolith (ADR-001/002) |
| Database | PostgreSQL 16 | System of record; RLS; Sumopod managed |
| Jobs | **Transactional outbox + Procrastinate** (Postgres-backed) | Outbox row written in the business transaction; a Procrastinate relay job claims outbox rows and runs handlers; Procrastinate provides leases, retries with backoff, periodic tasks, locks per key, dead-letter (failed) state. **Gate G-PROC**: confirm Procrastinate version supports sync psycopg3 workers + periodic tasks + `lock`/`queueing_lock`; fallback = keep current SKIP LOCKED dispatcher behind the same interface |
| Frontend | Next.js + Refine + shadcn/ui | Ops + approvals + finance UI |
| Reporting | Metabase OSS on read-only views (dedicated `oms_reporting` role, RLS-scoped) | |
| Orchestration | n8n (Sumopod) | Never authoritative; calls OMS commands only |
| Shared inbox | Chatwoot Community, **API-channel inbox** | Runtime deps: Rails web, Sidekiq, Redis 7, Postgres, object storage for attachments |
| WhatsApp | Meta WhatsApp Cloud API (Graph v27.0, configurable) | OMS is the only Meta client |
| Shipping | Biteship via `CarrierAdapter` | Mengantar not integrated (no API) |
| Payments | DOKU, manual bank transfer, COD | |
| Hosting | Sumopod managed services + app containers (api, worker, dashboard) | |
| Dev/CI | Docker Compose; CI Postgres service container | Local sandbox without Docker uses local Postgres (as in this session) |
| Tests | pytest, testcontainers (CI), respx, Playwright | |
No microservices, Kafka or extra queues unless a measured requirement appears.
