# 09 — Tenant Authorization & Row-Level Security

> Source of truth: `docs/00-plan.md` (V3.1, approved 2026-11-10). This file is the topic view of the listed sections.


## 7. Tenancy, authorization and RLS (revised)

- **Transaction-local context only**: `set_config('app.org_id', …, true)` and `app.user_id` per transaction (already the
  V3.0 implementation); never session-level `SET`. Pooled connections can't leak identity.
- **Roles**: `oms_owner` (DDL/migrations only), `oms_app` (API), `oms_worker` (jobs), `oms_webhook` (receivers),
  `oms_reporting` (Metabase, read-only views). **None has BYPASSRLS.** Workers run each job as the job's org plus a
  per-org **system principal** user with explicit, minimal role assignments (e.g. `system_router` org-scoped read of
  cash/policies). Webhook tenant resolution via `SECURITY DEFINER` resolvers (`resolve_wa_account(phone_number_id)`,
  `resolve_doku_payment(ref)`, …) that return only (org_id, account_id).
- **Policies**: SELECT/UPDATE/DELETE `USING`, INSERT and UPDATE `WITH CHECK`; visibility from `role_assignments` via
  definer helpers (org, legal entity, portfolio, platform account scopes); orders visible if any item visible; customers
  via `customer_portfolios`; FORCE RLS on all tenant tables; composite tenant FKs.
- **Credentials**: no grants for `oms_app`; written via `store_credential()` (owner role check); read only by
  `oms_worker` through `read_credential()` definer that audits each read; key versioning for rotation; revocation flag.
- **Permissions** (actions) in app layer (`authz.PERMISSIONS`); **visibility** in Postgres. Every surface enforced: API,
  workers, imports, webhooks, reports (reporting role views filter by `app.user_id` set by Metabase sandboxing or
  per-portfolio Metabase groups — **G-MB1** verify Metabase OSS capability; fallback: per-portfolio read views).
- Sensitive actions audited (credentials, approvals, capital, refunds, price overrides, policy overrides, ledger
  adjustments, exports of personal data).
