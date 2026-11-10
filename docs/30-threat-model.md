# 30 — Threat model (M5, 2026-11-10)

> Source of truth: `docs/00-plan.md`. STRIDE over the components that cross a trust boundary. Each row names the
> control and the test that evidences it; residual risks are listed explicitly, not hidden.

| Component / boundary | Threat (STRIDE) | Control | Evidence |
|---|---|---|---|
| Meta webhook `/webhooks/whatsapp` | Spoofing, replay, flooding | HMAC over raw body; dedupe key per delivery; persist-then-ack; worker processing | `test_m2_whatsapp.py` (#5), `test_m5_security.py::test_route_matrix_no_unauthenticated_access` |
| Chatwoot `/webhooks/chatwoot` | Spoofing, replay, echo loops | HMAC over `{ts}.{body}`; replay window; echo suppression by `source_id`; dedupe by Chatwoot message id | `test_m2_whatsapp.py::test_15_*`, `test_m5_ops.py::test_webhook_replay_window_*` |
| DOKU `/webhooks/doku` | Spoofing, forged capture, replay | Signature (doc-verified scheme); replay window; **status re-check before capture** | `test_m2_e2e.py`, `test_m5_ops.py` |
| Biteship `/webhooks/biteship` | Spoofing (provider auth unverified, G-BS2) | Shared secret token in callback URL; payload used only as a trigger; order re-fetched from Biteship | `test_m5_security.py::test_route_matrix_*`, `test_m2_e2e.py` |
| API (JWT) | Elevation, IDOR | Permissions in app layer; **row visibility in PostgreSQL RLS** for every role; composite tenant FKs | `test_m5_security.py::test_1_rls_fuzz_*`, `::test_2_*`, `test_m1_acceptance.py::test_7_*` |
| AI agent tools | Prompt injection, privilege escalation | Authority matrix; strict schemas; reply validator; acceptance evidence; kill switch; all decisions logged | `test_m2_agent.py` (#14) |
| Workers | Duplicate side effects, crash mid-call | `external_operations` committed before calls; provider policies; stalled-job recovery | `test_m2_reliability.py` (#6), `test_m4_review_fixes.py` |
| Credentials | Disclosure, theft, stale keys | Encrypted (Fernet, key rotation with previous keys); no app-role grants; worker-only audited reads; never logged/returned | `test_m5_security.py::test_7_*` |
| Reporting (Metabase) | Tenant spoofing via SQL | `session_user` identity; GUCs ignored for reporting logins; PII-free views | `test_m1_acceptance.py::test_20_*`, `test_m3_api.py::test_reporting_login_*` |
| CSV imports | Duplicate financial effects, bad data | Provenance; fingerprints; in-file dedupe; atomic row claims; all-or-nothing validation; currency/entity guards | `test_m4_imports_ads.py`, `test_m4_review_fixes.py` |
| Configuration | Dev secrets in production | Process refuses to start in staging/production with dev defaults | `test_m5_security.py::test_production_refuses_development_secrets` |
| Supply chain / repo | Leaked secrets, vulnerable deps | gitleaks (history), pip-audit, npm audit in CI | `.github/workflows/security.yml` |

## Residual risks (accepted for now, owner noted)
1. **Identity GUCs are trusted for server roles.** Anyone holding the `oms_app` database password can set
   `app.org_id`/`app.user_id` to any identity. Mitigation: credentials only in the secret store, network-restricted DB,
   distinct login roles per process. A stronger design (signed identity or `SET ROLE` per tenant) is not implemented.
   Owner: engineering, before multi-operator production.
2. **ORG-scoped tables check `organization_id = app_org()` only**, not membership. With a spoofed org GUC (requires (1))
   org-level reference rows of another tenant would be readable; portfolio-scoped rows are not (fuzz test). Owner:
   engineering.
3. Circuit-breaker state is per process (each worker trips independently).
4. Provider facts are documentation-verified only; no sandbox verification (G-DOKU, G-BS*, G-CW1).
5. Founder-only approvals (G-GOV1) weaken separation of duties until a second approver exists.
