# 18 — Deployment, Operations & Recovery

> Source of truth: `docs/00-plan.md` (canonical V3.1 specification incl. §27 normative corrections). This file is a topic view.


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
| Reporting | OMS API (RLS) for portfolio users; Metabase OSS for org-level users on PII-free `reporting` views via per-org login roles (DB-role identity, correction 7) | OSS has no row-level security |
| Orchestration | n8n (Sumopod) | Never authoritative; calls OMS commands only |
| Shared inbox | Chatwoot Community, **API-channel inbox** | Runtime deps: Rails web, Sidekiq, Redis 7, Postgres, object storage for attachments |
| WhatsApp | Meta WhatsApp Cloud API (Graph v27.0, configurable) | OMS is the only Meta client |
| Shipping | Biteship via `CarrierAdapter` | Mengantar not integrated (no API) |
| Payments | DOKU, manual bank transfer, COD | |
| Hosting | Sumopod managed services + app containers (api, worker, dashboard) | |
| Dev/CI | Docker Compose; CI Postgres service container | Local sandbox without Docker uses local Postgres (as in this session) |
| Tests | pytest, testcontainers (CI), respx, Playwright | |
No microservices, Kafka or extra queues unless a measured requirement appears.

## Production recovery checklist (remediation E4) — gate G-OPS1 stays open until executed on the target hosting

| # | Requirement | Evidence required |
|---|---|---|
| 1 | RPO ≤ 15 min | PITR / WAL archiving enabled on Sumopod; measured restore point vs failure time |
| 2 | RTO ≤ 4 h | Timed restore of a production-sized copy into an isolated environment |
| 3 | Backup integrity | Checksums / `pg_restore --list`; restore of the latest backup succeeds |
| 4 | Roles and RLS | Runtime roles without BYPASSRLS, FORCE RLS on all tenant tables, definer functions intact (`test_m5_security.py`, `test_remediation_security.py` against the restore) |
| 5 | Object storage | Media/evidence bucket restore with retention metadata |
| 6 | n8n | Workflow export restore; credentials re-linked from the secret store |
| 7 | Chatwoot | Database + storage restore; inbox/API-channel configuration verified |
| 8 | Secrets | Restore from the secret store; credential master key and previous keys available; rotation tested |
| 9 | Outbox and external operations | `invariants.post_restore_report()` / DR drill `post_restore`: pending outbox, `sent`/`uncertain` external operations resolved by lookup or a human **before** workers resume; payouts awaiting bank proof listed |
| 10 | Invariants | Full invariant run is empty (or every violation has a case and an owner) |

The local drill (docs/28) is preliminary evidence only and does not certify these targets.
