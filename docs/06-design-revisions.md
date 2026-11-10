# 06 — Design Revisions after Architecture Review (2026-11-10)

> **Superseded by V3.1** (`00-plan.md`, delta table in `24-v31-delta-and-summary.md`). R1–R5 are integrated there; where
> this file differs (e.g. R2 cash/capital wording, BYPASSRLS mention), V3.1 wins. Kept for history.

Five weaknesses were raised in review. Each revision below supersedes the earlier text in `00-plan.md` / `03-architecture.md`.

---

## R1. Single owner of the WhatsApp channel (no webhook tug-of-war)

**Problem.** A Chatwoot *WhatsApp Cloud* inbox registers its own phone-number-level callback override with Meta on creation
and on voice toggles. An OMS that "reasserts" its override creates two systems fighting over one Meta setting; a silent flip
drops referral/BSUID capture and conversion tracking.

**Decision.** The OMS is the **only** system connected to Meta for WhatsApp. Chatwoot uses an **API channel inbox**, which
never talks to Meta.

```mermaid
sequenceDiagram
    participant Meta as Meta Cloud API
    participant OMS as OMS WhatsApp Gateway
    participant CW as Chatwoot (API channel inbox)
    participant N8N as n8n AI Sales Admin
    Meta->>OMS: inbound webhook (verify HMAC on raw bytes, persist)
    OMS->>CW: POST /api/v1/accounts/{a}/conversations/{c}/messages (message_type=incoming)
    CW->>N8N: Agent Bot event (pending conversation)
    N8N->>OMS: /agent/* tools (quote, availability, payment link) under AI authority policy
    N8N->>OMS: /agent/replies (policy-checked outbound text)
    OMS->>Meta: send message (Cloud API) and mirror into Chatwoot as outgoing
    CW->>OMS: API-channel webhook: human agent message_created (outgoing)
    OMS->>Meta: send human reply; record wamid
```

- Meta app callback and phone-number override both point at the OMS; nothing else ever writes them.
- A scheduled **read-only** check (`GET /{phone_number_id}?fields=webhook_configuration`) raises a **critical
  integration alert** if the callback differs. It never rewrites automatically; an operator investigates.
- Outbound sending (text, media, templates, interactive) lives in the OMS gateway, which also enforces the 24-hour service
  window and template rules. Chatwoot is a UI over the conversation, not a Meta client.
- Trade-off accepted: Chatwoot's native WhatsApp template UI is not used; templates are managed in the OMS
  (`message_templates`) and Meta WhatsApp Manager.

## R2. Financial definitions (four different numbers, never mixed)

| Measure | Question it answers | Basis | Source |
|---|---|---|---|
| **Order contribution** | Was this order/SKU/campaign worth doing? | Management accounting, per order line, includes *allocated* ad spend and *expected* return cost | `fpa.order_line_economics` |
| **Accounting profit** | What did the business earn in the period? | Accrual (PSAK 72 / IFRS 15-style): revenue recognised at delivery (control transfer); COGS recognised with revenue; ad spend and opex expensed when incurred | `ledger_entries` on P&L accounts |
| **Cash position** | What money do we actually have, and what is free to use? | Cash basis: bank + e-wallet + gateway balances actually received | `ledger_entries` on cash accounts |
| **Invested capital** | How much money is tied up in each portfolio? | Balance sheet: inventory at cost + receivables (COD, marketplace, gateway) + supplier prepayments − supplier payables + capital committed to approved purchases | Derived from balance-sheet accounts by portfolio |

Derived:
- **Available cash** = cash position − committed outflows (approved but unpaid purchases, payables due ≤ 14 days) − liquidity reserve.
- **Portfolio ROIC** = portfolio operating profit (after allocated fixed costs) ÷ average invested capital in the period.
- **Cash conversion cycle** = days inventory + days receivable − days payable (per portfolio and fulfilment method).

Rules:
- A marketplace "order value" is never revenue and never cash. Marketplace revenue is recognised on delivery; the
  receivable becomes cash only when a settlement line is matched to a bank receipt.
- COD revenue is recognised on delivery; the COD receivable becomes cash only when the courier remittance is matched.
- Dropship supplier payments before customer cash = **supplier prepayment** (an asset), expensed to COGS when the
  customer order is delivered.
- Ad spend is a period expense in accounting profit; in contribution it is allocated to orders by a documented method
  (direct attribution first; remainder stays in an *unallocated marketing* bucket per portfolio — never spread silently).
- Inter-portfolio inventory transfers and capital reallocations are paired journals that net to zero on consolidation.
- Every journal balances (Σdebit = Σcredit) and is immutable; corrections are reversing journals.

Chart of accounts (codes in `app/modules/finance/accounts.py`): 1000 Cash, 1010 Gateway clearing, 1100 COD receivable,
1110 Marketplace receivable, 1200 Inventory, 1210 Supplier prepayments, 1300 Customer advances (unearned), 2000 Supplier
payables, 2100 Customer refunds payable, 3000 Owner capital, 3100 Inter-portfolio capital, 4000 Sales revenue,
4100 Shipping income, 4900 Sales returns & allowances, 5000 COGS, 5100 Shipping expense, 5200 Payment & COD fees,
5300 Marketplace fees, 5400 Packing, 5500 Return/failed-delivery losses, 6000 Advertising, 6900 Operating expenses.

## R3. Portfolio isolation enforced by PostgreSQL

**Problem.** App-layer filters fail open on any missed `WHERE`.

**Decision.** Row-level security decides visibility from **database-held role assignments**:
- The API authenticates the JWT and sets only `app.user_id` and `app.org_id` (transaction-local). It never passes a list of
  permitted portfolios — Postgres derives them.
- `SECURITY DEFINER` helper functions read `role_assignments`:
  `app_has_org_role()`, `app_portfolio_visible(portfolio_id)`, `app_order_visible(order_id)`,
  `app_customer_visible(customer_id)`, `app_platform_account_visible(account_id)`.
- Policies on every portfolio-scoped table: `organization_id = app_org() AND app_portfolio_visible(portfolio_id)`.
  Orders are visible if any line is visible; customers if linked (`customer_portfolios`) to a visible portfolio.
- `account_credentials` has **no grants** for the application role; secrets are written through a `SECURITY DEFINER`
  function that checks the `credentials.manage` permission and are read only by the system role (adapters/worker).
- The application role is not the table owner and has no `BYPASSRLS`; `FORCE ROW LEVEL SECURITY` is enabled.
- Tests connect as the application role, set identity to a Portfolio-A user and run raw SQL against Portfolio-B rows → 0 rows.

## R4. Exception workflows

Exceptions are first-class **cases** (`exception_cases`) with type, severity, owner, SLA due time, linked order/shipment/
settlement, decision log and resolution. Playbooks (`app/modules/exceptions/playbooks.py`) define for each type:
trigger → automatic containment → human decision options → approval needed → customer message → ledger treatment → close
criteria. See `17-exception-workflows.md`. Covered: failed delivery / RTS, COD refused, lost/damaged, refund request,
COD settlement mismatch, COD not remitted, supplier out of stock, supplier price drift, supplier cancellation,
dropship late/undelivered, wrong item, payment mismatch (under/over/duplicate), marketplace settlement mismatch,
duplicate order.

## R5. AI Sales Admin authority limits

The AI acts as a **service principal** (`ai_sales_agent` role) with a narrow tool API (`/agent/*`). It can propose; the
OMS decides.

| AI may | AI may not |
|---|---|
| Read active listings for the WhatsApp channel's portfolios | Quote SKUs not mapped to that channel/portfolio |
| Request a **server-computed quote** (price list, shipping, discounts within policy) | Set or change prices; promise free shipping outside policy |
| Place a short **stock hold** (TTL) when ATP is sufficient | Commit stock beyond available-to-promise; promise dropship stock not verified within the freshness window |
| Apply discounts ≤ policy auto-limit | Exceed discount limit (→ approval request + human handoff) |
| Send a payment link for an accepted quote | Confirm payments, cancel paid orders, issue refunds, change shipped addresses |
| Hand off to a human with a reason | Promise delivery dates beyond the SLA table |

Controls:
- Customer acceptance creates the order **from the quote id**, never from free text; quotes expire (default 2 h) and
  re-price on expiry.
- Every outbound AI message passes `/agent/replies`: Rupiah amounts and SKU references must match the active quote or
  catalog; otherwise the message is blocked and the conversation is handed to a human.
- Per-conversation and per-day limits (quotes, holds, discount value) and a kill switch per channel.
- All tool calls and policy decisions are logged in `agent_execution_logs` with the decision reason.
