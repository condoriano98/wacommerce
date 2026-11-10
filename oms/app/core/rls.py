"""Row-level security specification (docs/06-design-revisions.md R3).

The API sets only ``app.org_id`` and ``app.user_id``. Visibility is decided inside Postgres by
SECURITY DEFINER functions that read ``role_assignments`` — the application never supplies the
list of permitted portfolios.

Policy kinds:
* ``org``               organization match only (reference data any org member may read)
* ``org_scope``         organization-wide roles only
* ``portfolio``         row's portfolio must be visible (also enforced on INSERT)
* ``portfolio_shared``  NULL portfolio = org-level row visible to all members
* ``portfolio_strict``  NULL portfolio = org-level row visible to org-wide roles only
* ``order`` (V3.1): the order header and order-level children are visible only with org scope or when *every*
  line is visible (spec §27.4); portfolio-limited users read ``order_slices``.
* ``customer`` / ``platform_account`` / ``shipment``  visibility derived from the parent entity.
  INSERT only checks the organization because the parent's portfolio links are written in the
  same transaction; every read/update/delete is checked against visibility.
"""

ORG = "org"
ORG_SCOPE = "org_scope"
PORTFOLIO = "portfolio"
PORTFOLIO_SHARED = "portfolio_shared"
PORTFOLIO_STRICT = "portfolio_strict"
ORDER = "order"
CUSTOMER = "customer"
PLATFORM_ACCOUNT = "platform_account"
PLATFORM_ACCOUNT_NULLABLE = "platform_account_nullable"

# table -> (kind, column)
TABLE_POLICIES: dict[str, tuple[str, str]] = {
    "organizations": ("self_org", "id"),
    "brands": (ORG, ""),
    "users": (ORG, ""),
    "role_assignments": ("role_assignments", ""),
    "products": (ORG, ""),
    "skus": (ORG, ""),
    "suppliers": (ORG, ""),
    "warehouses": (ORG, ""),
    "capital_scenarios": (ORG_SCOPE, ""),
    "outbox_messages": (ORG_SCOPE, ""),
    "inbound_webhooks": (ORG_SCOPE, ""),
    "landing_sessions": (ORG_SCOPE, ""),
    "agent_execution_logs": (ORG_SCOPE, ""),
    "payment_notifications": (ORG_SCOPE, ""),
    "portfolios": (PORTFOLIO, "id"),
    "portfolio_budgets": (PORTFOLIO, "portfolio_id"),
    "portfolio_skus": (PORTFOLIO, "portfolio_id"),
    "platform_listings": (PORTFOLIO, "portfolio_id"),
    "supplier_offers": (PORTFOLIO, "portfolio_id"),
    "supplier_observations": (PORTFOLIO, "portfolio_id"),
    "quote_lines": (PORTFOLIO, "portfolio_id"),
    "order_items": (PORTFOLIO, "portfolio_id"),
    "inventory_balances": (PORTFOLIO, "portfolio_id"),
    "inventory_movements": (PORTFOLIO, "portfolio_id"),
    "inventory_reservations": (PORTFOLIO, "portfolio_id"),
    "procurement_tasks": (PORTFOLIO, "portfolio_id"),
    "external_purchases": (PORTFOLIO, "portfolio_id"),
    "campaign_portfolio_allocations": (PORTFOLIO, "portfolio_id"),
    "account_portfolio_links": (PORTFOLIO, "portfolio_id"),
    "customer_portfolios": (PORTFOLIO, "portfolio_id"),
    "risk_policies": (PORTFOLIO_SHARED, "portfolio_id"),
    "human_approvals": (PORTFOLIO_STRICT, "portfolio_id"),
    "audit_logs": (PORTFOLIO_STRICT, "portfolio_id"),
    "exception_cases": (PORTFOLIO_STRICT, "portfolio_id"),
    "exception_events": (PORTFOLIO_STRICT, "portfolio_id"),
    "ledger_entries": (PORTFOLIO_STRICT, "portfolio_id"),
    "operating_expenses": (PORTFOLIO_STRICT, "portfolio_id"),
    "capital_allocations": ("capital_allocations", ""),
    "orders": (ORDER, "id"),
    "order_events": (ORDER, "order_id"),
    "payments": (ORDER, "order_id"),
    "refunds": (ORDER, "order_id"),
    "routing_decisions": (ORDER, "order_id"),
    "shipments": (ORDER, "order_id"),
    "shipment_events": (ORDER, "order_id"),
    "returns": (ORDER, "order_id"),
    "customers": (CUSTOMER, "id"),
    "customer_identities": (CUSTOMER, "customer_id"),
    "addresses": (CUSTOMER, "customer_id"),
    "privacy_preferences": (CUSTOMER, "customer_id"),
    "consent_events": (CUSTOMER, "customer_id"),
    "conversations": (CUSTOMER, "customer_id"),
    "messages": (CUSTOMER, "customer_id"),
    "customer_attribution": (CUSTOMER, "customer_id"),
    "quotes": (CUSTOMER, "customer_id"),
    "conversion_outbox": (CUSTOMER, "customer_id"),
    "conversion_delivery_logs": (CUSTOMER, "customer_id"),
    "platform_accounts": (PLATFORM_ACCOUNT, "id"),
    "integration_sync_runs": (PLATFORM_ACCOUNT, "platform_account_id"),
    "ad_campaigns": (PLATFORM_ACCOUNT, "platform_account_id"),
    "ad_groups": (PLATFORM_ACCOUNT, "platform_account_id"),
    "advertisements": (PLATFORM_ACCOUNT, "platform_account_id"),
    "daily_ad_insights": (PLATFORM_ACCOUNT, "platform_account_id"),
    "settlements": (PLATFORM_ACCOUNT, "platform_account_id"),
    "settlement_lines": (PLATFORM_ACCOUNT, "platform_account_id"),
    "integration_errors": (PLATFORM_ACCOUNT_NULLABLE, "platform_account_id"),
    # --- V3.1 (migration 0005) ---
    "legal_entities": (ORG, ""),
    "bank_accounts": (ORG_SCOPE, ""),
    "identity_domains": (ORG, ""),
    "capital_allocation_events": (PORTFOLIO, "portfolio_id"),
    "capital_commitments": (PORTFOLIO, "portfolio_id"),
    "commitment_consumptions": (PORTFOLIO, "portfolio_id"),
    "integration_capabilities": (PLATFORM_ACCOUNT, "platform_account_id"),
    "order_item_allocations": (PORTFOLIO, "portfolio_id"),
    "payment_events": (ORDER, "order_id"),
    "fulfillment_groups": (PORTFOLIO, "portfolio_id"),
    "fulfillment_group_items": (PORTFOLIO, "portfolio_id"),
    "shipment_items": (PORTFOLIO, "portfolio_id"),
    "purchase_tasks": (PORTFOLIO, "portfolio_id"),
    "purchase_lines": (PORTFOLIO, "portfolio_id"),
    "stock_counts": (ORG, ""),
    "stock_count_lines": (PORTFOLIO, "portfolio_id"),
    "accounting_periods": (ORG, ""),
    "bank_statement_lines": (ORG_SCOPE, ""),
    "reconciliation_runs": (ORG_SCOPE, ""),
    "reconciliation_items": (PORTFOLIO_STRICT, "portfolio_id"),
    "external_operations": ("org_scope_read", ""),
    "dead_letters": (ORG_SCOPE, ""),
}

# V3.1 overrides applied by migration 0005 (policies are dropped and recreated for these tables).
SHIPMENT = "shipment"
ORG_SCOPE_READ = "org_scope_read"  # any org member may INSERT (outbox, logs); only org-scope roles may read/modify

V31_OVERRIDES: dict[str, tuple[str, str]] = {
    "outbox_messages": (ORG_SCOPE_READ, ""),
    "agent_execution_logs": (ORG_SCOPE_READ, ""),
    "inbound_webhooks": (ORG_SCOPE_READ, ""),
    "payment_notifications": (ORG_SCOPE_READ, ""),
    # Shipments belong to a single-portfolio fulfillment group; legacy rows (no portfolio) need full order visibility.
    "shipments": (SHIPMENT, "id"),
    "shipment_events": (SHIPMENT, "shipment_id"),
}

# Tables the application role can never modify after insert.
APPEND_ONLY = (
    "audit_logs",
    "order_events",
    "ledger_entries",
    "consent_events",
    "exception_events",
    "inventory_movements",
    "shipment_events",
    "conversion_delivery_logs",
    "supplier_observations",
    "payment_events",
    "capital_allocation_events",
)

# Global reference tables: readable by everyone, no tenant data.
GLOBAL_READONLY = ("providers", "ledger_accounts")

# No privileges at all for the application role.
NO_APP_ACCESS = ("account_credentials", "reporting_principals")

ORG_MATCH = "organization_id = app_org()"


def visibility_expr(kind: str, col: str) -> tuple[str, str]:
    """Return (using_expr, insert_check_expr)."""
    if kind == "self_org":
        return "id = app_org()", "false"
    if kind == ORG:
        return ORG_MATCH, ORG_MATCH
    if kind == ORG_SCOPE:
        e = f"{ORG_MATCH} and app_has_org_scope()"
        return e, e
    if kind == "role_assignments":
        e = f"{ORG_MATCH} and (user_id = app_user() or app_has_org_scope())"
        return e, f"{ORG_MATCH} and app_has_org_role('owner')"
    if kind == PORTFOLIO:
        e = f"{ORG_MATCH} and app_portfolio_visible({col})"
        if col == "id":  # creating a portfolio requires an org-wide owner/manager role
            return e, f"{ORG_MATCH} and app_has_org_scope()"
        return e, e
    if kind == PORTFOLIO_SHARED:
        e = f"{ORG_MATCH} and ({col} is null or app_portfolio_visible({col}))"
        return e, e
    if kind == PORTFOLIO_STRICT:
        e = f"{ORG_MATCH} and app_portfolio_visible({col})"
        # Inserts of org-level rows (NULL portfolio) are allowed (e.g. an approval request by a
        # portfolio user about shared opex is still recorded), reads stay strict.
        return e, f"{ORG_MATCH} and ({col} is null or app_portfolio_visible({col}))"
    if kind == "capital_allocations":
        e = f"{ORG_MATCH} and (app_portfolio_visible(to_portfolio_id) or app_portfolio_visible(from_portfolio_id))"
        return e, f"{ORG_MATCH} and app_has_org_scope()"
    if kind == ORDER:
        return f"{ORG_MATCH} and app_order_visible({col})", ORG_MATCH
    if kind == CUSTOMER:
        return f"{ORG_MATCH} and app_customer_visible({col})", ORG_MATCH
    if kind == PLATFORM_ACCOUNT:
        return f"{ORG_MATCH} and app_platform_account_visible({col})", ORG_MATCH
    if kind == ORG_SCOPE_READ:
        return f"{ORG_MATCH} and app_has_org_scope()", ORG_MATCH
    if kind == SHIPMENT:
        return f"{ORG_MATCH} and app_shipment_visible({col})", ORG_MATCH
    if kind == PLATFORM_ACCOUNT_NULLABLE:
        e = f"{ORG_MATCH} and (({col} is null and app_has_org_scope()) or app_platform_account_visible({col}))"
        return e, ORG_MATCH
    raise ValueError(kind)

# Frozen inputs for migration 0002 (V3.0). Migrations must not change meaning when this module evolves.
V31_NEW_TABLES = (
    "legal_entities", "bank_accounts", "identity_domains", "capital_allocation_events", "capital_commitments",
    "commitment_consumptions", "integration_capabilities", "order_item_allocations", "payment_events",
    "fulfillment_groups", "fulfillment_group_items", "shipment_items", "purchase_tasks", "purchase_lines",
    "stock_counts", "stock_count_lines", "accounting_periods", "bank_statement_lines", "reconciliation_runs",
    "reconciliation_items", "external_operations", "dead_letters",
)
V30_TABLE_POLICIES = {k: v for k, v in TABLE_POLICIES.items() if k not in V31_NEW_TABLES}
V30_APPEND_ONLY = tuple(t for t in APPEND_ONLY if t not in V31_NEW_TABLES)
V30_NO_APP_ACCESS = ("account_credentials",)
