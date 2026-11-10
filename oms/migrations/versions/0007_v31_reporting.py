"""V3.1 reporting (ADR-016) and warehouse legal entity.

Reporting identity: per-org LOGIN roles, members of ``oms_reporting``, mapped in ``reporting_principals``; identity is
``session_user`` (GUCs ignored for these roles). Views live in schema ``reporting`` owned by ``oms_reporting_owner``
(not a table owner, no BYPASSRLS), so base-table RLS applies with the mapped org identity. Views are PII-free.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

VIEWS = r"""
create or replace view reporting.daily_pnl with (security_barrier = true) as
select le.organization_id, le.accounting_date, le.portfolio_id, la.type as account_type, le.account_code,
       sum(le.debit) as debit, sum(le.credit) as credit
from public.ledger_entries le join public.ledger_accounts la on la.code = le.account_code
where la.type in ('revenue', 'expense')
group by 1, 2, 3, 4, 5;

create or replace view reporting.portfolio_orders with (security_barrier = true) as
select oi.organization_id, oi.portfolio_id, o.sales_channel, date(o.placed_at) as order_date,
       count(distinct o.id) as orders, sum(oi.qty) as units, sum(oi.line_total) as gross_sales,
       sum(oi.qty_delivered) as units_delivered, sum(oi.qty_returned) as units_returned
from public.order_items oi join public.orders o on o.id = oi.order_id
group by 1, 2, 3, 4;
"""

PROVISION = r"""
-- Owner-only provisioning of a per-org reporting login (deployment step; password supplied by the operator).
create or replace function provision_reporting_principal(p_role text, p_password text, p_org uuid, p_user uuid)
returns void language plpgsql security definer set search_path = public as
$$ begin
  if not exists (select 1 from pg_roles where rolname = p_role) then
    execute format('create role %I login inherit connection limit 5 password %L', p_role, p_password);
  end if;
  execute format('grant oms_reporting to %I', p_role);
  execute format('alter role %I set statement_timeout = %L', p_role, '30s');
  insert into reporting_principals (db_role, organization_id, principal_user_id) values (p_role, p_org, p_user)
  on conflict (db_role) do update set organization_id = excluded.organization_id,
                                      principal_user_id = excluded.principal_user_id;
end $$;
revoke execute on function provision_reporting_principal(text, text, uuid, uuid) from public;
"""


def upgrade() -> None:
    op.add_column("warehouses", sa.Column("legal_entity_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("warehouses_legal_entity_id_fkey", "warehouses", "legal_entities", ["legal_entity_id"], ["id"])
    op.execute("""update warehouses w set legal_entity_id = le.id from legal_entities le
                  where le.organization_id = w.organization_id and w.legal_entity_id is null""")
    op.execute("alter table warehouses add constraint fkt_warehouses_legal_entity_id foreign key "
               "(organization_id, legal_entity_id) references legal_entities (organization_id, id)")
    # reporting view owner may read the base tables it aggregates — still subject to RLS
    op.execute("grant usage on schema public to oms_reporting_owner")
    op.execute("grant select on ledger_entries, ledger_accounts, orders, order_items, role_assignments, "
               "customer_portfolios, account_portfolio_links to oms_reporting_owner")
    op.execute("set role oms_reporting_owner")
    op.execute(VIEWS)
    op.execute("reset role")
    op.execute("grant select on all tables in schema reporting to oms_reporting")
    op.execute("revoke all on all tables in schema public from oms_reporting")
    op.execute(PROVISION)


def downgrade() -> None:
    op.execute("drop function if exists provision_reporting_principal(text, text, uuid, uuid)")
    op.execute("drop view if exists reporting.portfolio_orders")
    op.execute("drop view if exists reporting.daily_pnl")
    op.drop_constraint("fkt_warehouses_legal_entity_id", "warehouses")
    op.drop_constraint("warehouses_legal_entity_id_fkey", "warehouses")
    op.drop_column("warehouses", "legal_entity_id")
