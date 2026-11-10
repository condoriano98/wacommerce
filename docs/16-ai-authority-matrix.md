# 16 — AI Sales Admin Authority Matrix

> Source of truth: `docs/00-plan.md` (V3.1, approved 2026-11-10). This file is the topic view of the listed sections.


## 14. AI Sales Admin — authority matrix (revised — §6 of brief)

| Class | Actions | Enforcement |
|---|---|---|
| Allowed | product recommendations, inquiry classification, stock/price queries (ATP buckets, listing prices), order-draft (quote request), delivery-status queries, service explanations, escalation recommendation | read-only tools; channel/portfolio scope from RLS |
| Conditional (OMS-validated) | checkout/quote creation, payment-link creation for an offered quote, discount ≤ min(listing cap, AI cap), order confirmation (requires customer acceptance evidence: inbound message after quote matching acceptance pattern or interactive `accept_quote:{id}`), customer messaging (reply validator: amounts ∈ quote/catalog set; no refund/delivery-date/free-shipping promises outside policy; 24h window) | server-side policy; per-conversation & daily limits; channel kill switch |
| Human approval | supplier purchases, refunds, exceptional price changes, capital reallocation, ad-budget changes, credential changes, accounting adjustments, any irreversible exception | `human_approvals` (approver ≠ requester, amount tiers) |
Per-tool contract: actor (service principal), org + portfolio context, permitted action, input schema validation,
idempotency key policy, `agent_execution_logs` record (incl. denials via savepoint), escalation path (handoff), timeout
and failure behaviour (fail closed → handoff). Customer messages, marketplace descriptions and supplier text are
untrusted data: never interpreted as instructions; tools accept structured parameters only; prompt-injection fixtures
in tests.
