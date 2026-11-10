# 08 — Entity Relationship Model (V3.1)

> Source: `docs/00-plan.md` §5. Every tenant table carries `organization_id`; children reference parents with composite
> FKs `(organization_id, parent_id)`.

## Core ERD

```mermaid
erDiagram
    ORGANIZATION ||--o{ LEGAL_ENTITY : owns
    LEGAL_ENTITY ||--o{ BANK_ACCOUNT : holds
    LEGAL_ENTITY ||--o{ BRAND : operates
    BRAND ||--o{ PORTFOLIO : groups
    LEGAL_ENTITY ||--o{ WAREHOUSE : runs
    ORGANIZATION ||--o{ IDENTITY_DOMAIN : defines
    PORTFOLIO ||--o{ CAPITAL_MANDATE : funded_by
    CAPITAL_MANDATE ||--o{ CAPITAL_COMMITMENT : reserves
    PLATFORM_ACCOUNT }o--o{ PORTFOLIO : account_portfolio_links
    PLATFORM_ACCOUNT ||--o{ INTEGRATION_CAPABILITY : declares
    PRODUCT ||--o{ SKU : has
    SKU }o--o{ PORTFOLIO : portfolio_skus
    SKU ||--o{ PLATFORM_LISTING : listed_as
    SKU ||--o{ SUPPLIER_OFFER : sourced_from
    CUSTOMER ||--o{ CUSTOMER_IDENTITY : known_by
    IDENTITY_DOMAIN ||--o{ CUSTOMER_IDENTITY : scopes
    CUSTOMER ||--o{ CONVERSATION : chats
    CONVERSATION ||--o{ MESSAGE : contains
    MESSAGE ||--o| MESSAGE_MAPPING : mapped_to
    CUSTOMER ||--o{ CUSTOMER_ATTRIBUTION : touched_by
    CUSTOMER ||--o{ QUOTE : receives
    QUOTE ||--o| ORDER : becomes
    ORDER ||--o{ ORDER_ITEM : contains
    ORDER_ITEM }o--|| PORTFOLIO : attributed_to
    ORDER_ITEM ||--o{ ORDER_ITEM_ALLOCATION : allocated
    ORDER ||--o{ FULFILLMENT_GROUP : fulfilled_by
    FULFILLMENT_GROUP ||--o{ FULFILLMENT_GROUP_ITEM : covers
    FULFILLMENT_GROUP ||--o{ SHIPMENT : ships
    SHIPMENT ||--o{ SHIPMENT_ITEM : carries
    FULFILLMENT_GROUP ||--o{ PURCHASE_TASK : procures
    PURCHASE_TASK ||--o{ PURCHASE_LINE : quantities
    PURCHASE_TASK ||--o{ EXTERNAL_PURCHASE : evidenced_by
    ORDER ||--o{ PAYMENT : paid_by
    PAYMENT ||--o{ PAYMENT_EVENT : history
    PLATFORM_ACCOUNT ||--o{ SETTLEMENT : pays_out
    SETTLEMENT ||--o{ SETTLEMENT_LINE : details
    BANK_ACCOUNT ||--o{ BANK_STATEMENT_LINE : records
    RECONCILIATION_RUN ||--o{ RECONCILIATION_ITEM : matches
    RECONCILIATION_ITEM ||--o| EXCEPTION_CASE : escalates
    LEGAL_ENTITY ||--o{ ACCOUNTING_PERIOD : closes
    LEDGER_ENTRY }o--|| LEDGER_ACCOUNT : posts_to
    LEDGER_ENTRY }o--o| PORTFOLIO : dimension
    WAREHOUSE ||--o{ INVENTORY_BALANCE : stores
    INVENTORY_BALANCE ||--o{ INVENTORY_MOVEMENT : ledger
```

## Key invariants (DB-enforced)

| Invariant | Mechanism |
|---|---|
| No cross-tenant references | composite FKs `(organization_id, id)` |
| Journals balance | deferred constraint trigger per `journal_id` |
| No oversell | `CHECK reserved <= on_hand` + row locks |
| No over-procurement | `CHECK qty_procured <= qty_allocated_dropship` on `order_items` + locked updates |
| Quantity pipeline | `CHECK` delivered ≤ shipped ≤ allocated ≤ ordered; returned ≤ delivered |
| External dedupe | `UNIQUE(account, external_order_id)`, `UNIQUE(provider, notification_id)`, `UNIQUE(account, wamid)`, `UNIQUE(marketplace_account, external_purchase_id)`, bank line fingerprint |
| Conversion dedupe | `UNIQUE(subject, event, destination, occurrence)` |
| Identity scope | `UNIQUE(identity_domain, provider, identity_type, value)` |
| Separation of duties | `CHECK decided_by <> requested_by` |
| Append-only history | UPDATE/DELETE revoked on events, journals, audit, movements |

Full table list with columns: `docs/00-plan.md` §5.
