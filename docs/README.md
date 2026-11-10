# Wacommerce documentation (V3.1)

Canonical specification: [`00-plan.md`](00-plan.md) (incl. §27 normative corrections). Procedure: [`29-execution-playbook.md`](29-execution-playbook.md). Topic files below are views of it; on any conflict the
master plan wins, and superseded statements are listed in [`23-contradiction-register.md`](23-contradiction-register.md).

| File | Topic | Status |
|---|---|---|
| [00-plan.md](00-plan.md) | V3.1 master plan | Approved |
| [01-skills-audit.md](01-skills-audit.md) | Installed skills & repository audit | Done (V3.0, still valid) |
| [02-build-vs-reuse.md](02-build-vs-reuse.md) | Build-versus-reuse matrix | Updated V3.1 |
| [03-architecture.md](03-architecture.md) | Architecture overview | Rewritten V3.1 |
| [04-provider-capabilities.md](04-provider-capabilities.md) | Capability registry, provider verification, marketplace/ads adapters | V3.1 |
| [05-meta-attribution.md](05-meta-attribution.md) | Meta CTWA attribution, Pixel, CAPI | Aligned V3.1 |
| [06-design-revisions.md](06-design-revisions.md) | R1–R5 review (historical) | Superseded by V3.1 |
| [07-adr.md](07-adr.md) | Architecture decision records ADR-001…013 | V3.1 |
| [08-erd.md](08-erd.md) | ERD and DB invariants | V3.1 |
| [09-tenant-security-rls.md](09-tenant-security-rls.md) | Tenant authorization & RLS | V3.1 |
| [10-state-machines.md](10-state-machines.md) | Order/item/group/shipment/payment/settlement/purchase machines | V3.1 |
| [11-accounting-policy.md](11-accounting-policy.md) + [fixtures/journals.yaml](fixtures/journals.yaml) | Accounting policy, CoA, journal fixtures F1–F12 | V3.1 (gates G-ACC1, G-TAX1) |
| [12-payments-settlement-reconciliation.md](12-payments-settlement-reconciliation.md) | Payments, settlement, reconciliation | V3.1 |
| [13-whatsapp-api-channel.md](13-whatsapp-api-channel.md) | Single-owner WhatsApp + Chatwoot API channel | V3.1 (gate G-CW1) |
| [14-fpa-methodology.md](14-fpa-methodology.md) | FP&A waterfall CM1–CM3, capital concepts | V3.1 |
| [15-capital-simulator.md](15-capital-simulator.md) | Rp100M simulator & stress tests | V3.1 |
| [16-ai-authority-matrix.md](16-ai-authority-matrix.md) | AI Sales Admin authority | V3.1 |
| [17-exception-workflows.md](17-exception-workflows.md) | Exception playbooks (SOP) | Extended V3.1 |
| [18-deployment-recovery.md](18-deployment-recovery.md) | Environments, ops, backup/recovery | V3.1 |
| [19-milestones.md](19-milestones.md) | Gated milestones M0–M5 | V3.1 |
| [20-acceptance-tests.md](20-acceptance-tests.md) | Acceptance-test matrix & invariants | V3.1 |
| [21-risks-blockers.md](21-risks-blockers.md) | Risks & verification gates | V3.1 |
| [22-backlog.md](22-backlog.md) | P0/P1/P2 backlog | V3.1 |
| [23-contradiction-register.md](23-contradiction-register.md) | Superseded V3.0 statements | V3.1 |
| [24-v31-delta-and-summary.md](24-v31-delta-and-summary.md) | V3.0→V3.1 delta, executive summary, code impact | V3.1 |
| [25-m5-readiness-and-release-gates.md](25-m5-readiness-and-release-gates.md) | M5 readiness audit & release gates (PR #1) | NO-GO until evidence |
| [26-m5-controlled-deployment-runbook.md](26-m5-controlled-deployment-runbook.md) | Controlled deployment & recovery runbook (PR #1) | Draft, not executed |
| [27-implementation-status.md](27-implementation-status.md) | Implementation status per milestone (SHAs, CI, tests) | Live |
| [29-execution-playbook.md](29-execution-playbook.md) | Execution playbook (procedure only) | V3.1 |

Implementation state: V3.0 foundation and commerce core are implemented and tested (21 tests). They are the regression
baseline and get refactored in M1 per `24-v31-delta-and-summary.md`. WhatsApp/Chatwoot code is committed WIP.
