"""Remediation C1 + C2: legal-entity role scope for financial reporting; portfolio-limited order slice status.

C1: ``role_assignments.scope_type`` gains ``legal_entity``. ``app_legal_entity_visible(le)`` is true for org-scope
roles and for roles assigned to that legal entity. Ledger lines and FP&A snapshots of an entity are visible to its
entity-scoped finance users (the application additionally checks the action permission, e.g. ``fpa.read``).

C2: ``order_slices.order_status`` no longer exposes the header status (computed over every portfolio's lines and
cases). It is derived only from the line rows visible to the current principal (a window over the already-filtered
rows), so another portfolio's deliveries, returns, refunds or exception cases cannot leak. Column shape unchanged.

Revision ID: 0022
Revises: 0021
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

LE_VISIBLE = """
create or replace function app_legal_entity_visible(p_le uuid) returns boolean
language sql stable security definer set search_path = public, pg_temp as
$$ select app_has_org_scope() or (p_le is not null and exists (
       select 1 from role_assignments ra
       where ra.user_id = app_user() and ra.organization_id = app_org()
         and ra.scope_type = 'legal_entity' and ra.scope_id = p_le)) $$;
"""

SLICES = r"""
create view order_slices with (security_barrier = true) as
select o.organization_id, o.id as order_id, o.order_number, o.sales_channel, o.placed_at,
       case
         when count(*) filter (where oi.qty_canceled < oi.qty) over w = 0 then 'canceled'
         when sum(oi.qty_returned) filter (where oi.qty_canceled < oi.qty) over w > 0
              and sum(oi.qty_returned) filter (where oi.qty_canceled < oi.qty) over w
                  = sum(oi.qty - oi.qty_canceled) filter (where oi.qty_canceled < oi.qty) over w then 'returned'
         when sum(oi.qty_returned) filter (where oi.qty_canceled < oi.qty) over w > 0 then 'partially_returned'
         when sum(oi.qty_delivered) filter (where oi.qty_canceled < oi.qty) over w
              = sum(oi.qty - oi.qty_canceled) filter (where oi.qty_canceled < oi.qty) over w then 'delivered'
         when sum(oi.qty_delivered) filter (where oi.qty_canceled < oi.qty) over w > 0 then 'partially_delivered'
         when sum(oi.qty_shipped + oi.qty_allocated_owned + oi.qty_allocated_dropship + oi.qty_allocated_platform)
              filter (where oi.qty_canceled < oi.qty) over w > 0 then 'in_fulfillment'
         else 'confirmed'
       end as order_status,
       oi.id as order_item_id, oi.portfolio_id, oi.sku_id, oi.qty, oi.unit_price, oi.discount, oi.line_total,
       oi.qty_allocated_owned, oi.qty_allocated_dropship, oi.qty_allocated_platform, oi.qty_procured,
       oi.qty_shipped, oi.qty_delivered, oi.qty_returned, oi.qty_refunded, oi.qty_canceled
from orders o join order_items oi on oi.order_id = o.id
where o.organization_id = app_org() and app_portfolio_visible(oi.portfolio_id)
window w as (partition by o.id);
grant select on order_slices to oms_runtime;
"""

OLD_SLICES = r"""
create view order_slices with (security_barrier = true) as
select o.organization_id, o.id as order_id, o.order_number, o.sales_channel, o.placed_at, o.status as order_status,
       oi.id as order_item_id, oi.portfolio_id, oi.sku_id, oi.qty, oi.unit_price, oi.discount, oi.line_total,
       oi.qty_allocated_owned, oi.qty_allocated_dropship, oi.qty_allocated_platform, oi.qty_procured,
       oi.qty_shipped, oi.qty_delivered, oi.qty_returned, oi.qty_refunded, oi.qty_canceled
from orders o join order_items oi on oi.order_id = o.id
where o.organization_id = app_org() and app_portfolio_visible(oi.portfolio_id);
grant select on order_slices to oms_runtime;
"""

LEDGER_SEL = "organization_id = app_org() and (app_portfolio_visible(portfolio_id) or app_legal_entity_visible(legal_entity_id))"
LEDGER_INS = ("organization_id = app_org() and (portfolio_id is null or app_portfolio_visible(portfolio_id) "
              "or app_legal_entity_visible(legal_entity_id))")
SNAP = ("organization_id = app_org() and ((scope_type = 'portfolio' and app_portfolio_visible(portfolio_id)) "
        "or app_legal_entity_visible(legal_entity_id))")


def upgrade() -> None:
    op.execute("alter table role_assignments drop constraint role_assignments_scope_type_check")
    op.execute("alter table role_assignments add constraint role_assignments_scope_type_check check "
               "(scope_type in ('organization','portfolio','platform_account','legal_entity'))")
    op.execute(LE_VISIBLE)
    # Same execute privileges as the other RLS visibility helpers (policies on reporting views evaluate it too).
    op.execute("drop policy p_select on ledger_entries")
    op.execute(f"create policy p_select on ledger_entries for select using ({LEDGER_SEL})")
    op.execute("drop policy p_insert on ledger_entries")
    op.execute(f"create policy p_insert on ledger_entries for insert with check ({LEDGER_INS})")
    op.execute("drop policy p_select on fpa_snapshots")
    op.execute(f"create policy p_select on fpa_snapshots for select using ({SNAP})")
    op.execute("drop view order_slices")
    op.execute(SLICES)


def downgrade() -> None:
    op.execute("drop view order_slices")
    op.execute(OLD_SLICES)
    op.execute("drop policy p_select on fpa_snapshots")
    op.execute("create policy p_select on fpa_snapshots for select using (organization_id = app_org() and "
               "((scope_type = 'portfolio' and app_portfolio_visible(portfolio_id)) "
               "or (scope_type = 'legal_entity' and app_has_org_scope())))")
    op.execute("drop policy p_insert on ledger_entries")
    op.execute("create policy p_insert on ledger_entries for insert with check (organization_id = app_org() and "
               "(portfolio_id is null or app_portfolio_visible(portfolio_id)))")
    op.execute("drop policy p_select on ledger_entries")
    op.execute("create policy p_select on ledger_entries for select using (organization_id = app_org() and "
               "app_portfolio_visible(portfolio_id))")
    op.execute("drop function if exists app_legal_entity_visible(uuid)")
    op.execute("alter table role_assignments drop constraint role_assignments_scope_type_check")
    op.execute("alter table role_assignments add constraint role_assignments_scope_type_check check "
               "(scope_type in ('organization','portfolio','platform_account'))")
