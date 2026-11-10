# 21 — Open Risks & Verification Gates

> Source of truth: `docs/00-plan.md` (V3.1, approved 2026-11-10). This file is the topic view of the listed sections.


## 25. Open risks and verification gates

Gates (owner → due M0 unless noted): **G-DOKU** DOKU product/signature/notification/status APIs · **G-BS1–3** Biteship
COD remittance reporting, webhook auth, billing model · **G-CW1** Chatwoot API-channel callback semantics and echo fields
· **G-PROC** Procrastinate sync worker/periodic/lock features · **G-MB1** Metabase OSS row-level access · **G-ACC1**
accounting framework (SAK EP vs EMKM) and revenue trigger confirmation by accountant · **G-TAX1** PKP/PPN/PPh 22
treatment · **G-TAX2** record retention period · **G-BANK1** bank statement CSV formats · **G-OPS1** Sumopod PITR ·
**G-OPS2** Sumopod Postgres role privileges (non-superuser roles, definer functions) · Meta: Indonesia hashed fallback,
`test_event_code` on WABA dataset · TikTok ISV for own-group shops · Lazada SS app category.
Risks: marketplace account bans (mitigated: no automation); provider version churn (registry + contract tests);
financial misstatement from mis-set triggers (fixtures + accountant sign-off); Chatwoot/n8n operational load on Sumopod;
single-founder approval bottleneck (approver ≠ requester requires ≥2 approvers — needs a second authorised person or a
delegated approver policy **G-GOV1**).
