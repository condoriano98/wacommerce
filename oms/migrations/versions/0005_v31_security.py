"""V3.1 security (expand): roles without BYPASSRLS alongside the legacy roles, session_user reporting identity,
order-header visibility, shipment visibility, RLS for new tables, tenant-scoped composite foreign keys,
procurement quantity trigger, closed-period lock, webhook resolvers, outbox claiming, credential reads.

Backward compatible (ADR-014): ``oms_app`` and ``oms_system`` keep working; code selects the new roles with
``OMS_DB_ROLE_MODE=v31``. ``oms_system`` is removed only by a later contract migration.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

from app.core.rls import TABLE_POLICIES, V31_NEW_TABLES, V31_OVERRIDES, visibility_expr
from app.models import metadata

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

RUNTIME_ROLES = ("oms_app", "oms_worker", "oms_webhook")

ROLES = r"""
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'oms_runtime') then
    create role oms_runtime nologin;                     -- privilege group, no RLS bypass
  end if;
  if not exists (select 1 from pg_roles where rolname = 'oms_worker') then
    create role oms_worker nologin;                      -- LOGIN + password set by deployment
  end if;
  if not exists (select 1 from pg_roles where rolname = 'oms_webhook') then
    create role oms_webhook nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'oms_reporting_owner') then
    create role oms_reporting_owner nologin;             -- owns reporting views; not a table owner, no bypass
  end if;
  if not exists (select 1 from pg_roles where rolname = 'oms_reporting') then
    create role oms_reporting nologin;                   -- group for per-org reporting logins
  end if;
end $$;
grant oms_runtime to oms_app, oms_worker, oms_webhook;
"""

FUNCTIONS = r"""
-- Reporting identity comes from the *login* role (session_user), never from GUCs (spec §27.7, ADR-016).
create or replace function reporting_identity(out org uuid, out usr uuid)
language sql stable security definer set search_path = public as
$$ select rp.organization_id, rp.principal_user_id from reporting_principals rp where rp.db_role = session_user $$;

create or replace function app_org() returns uuid language plpgsql stable as
$$ declare r record; begin
  select * into r from reporting_identity();
  if r.org is not null then return r.org; end if;
  return nullif(current_setting('app.org_id', true), '')::uuid;
end $$;

create or replace function app_user() returns uuid language plpgsql stable as
$$ declare r record; begin
  select * into r from reporting_identity();
  if r.org is not null then return r.usr; end if;
  return nullif(current_setting('app.user_id', true), '')::uuid;
end $$;

-- Order header: org scope, or *every* line visible (spec §27.4).
create or replace function app_order_visible(o uuid) returns boolean
language sql stable security definer set search_path = public as
$$ select app_has_org_scope() or (
       exists (select 1 from order_items oi where oi.order_id = o)
       and not exists (select 1 from order_items oi where oi.order_id = o
                       and not app_portfolio_visible(oi.portfolio_id))) $$;

create or replace function app_shipment_visible(s uuid) returns boolean
language sql stable security definer set search_path = public as
$$ select exists (select 1 from shipments sh where sh.id = s and (
          (sh.portfolio_id is not null and app_portfolio_visible(sh.portfolio_id))
          or (sh.portfolio_id is null and app_order_visible(sh.order_id)))) $$;

-- Tenant resolution for inbound webhooks: returns identifiers only (no row data).
create or replace function resolve_platform_account(p_provider text, p_key text, p_value text,
                                                    out organization_id uuid, out platform_account_id uuid)
language sql stable security definer set search_path = public as
$$ select pa.organization_id, pa.id from platform_accounts pa
   where pa.provider = p_provider and pa.public_config ->> p_key = p_value limit 1 $$;

create or replace function resolve_payment(p_provider text, p_ref text,
                                           out organization_id uuid, out payment_id uuid)
language sql stable security definer set search_path = public as
$$ select p.organization_id, p.id from payments p where p.provider = p_provider and p.provider_ref = p_ref $$;

create or replace function resolve_shipment(p_external_id text, out organization_id uuid, out shipment_id uuid)
language sql stable security definer set search_path = public as
$$ select s.organization_id, s.id from shipments s where s.external_id = p_external_id limit 1 $$;

-- Per-org system principal used by workers/webhooks instead of BYPASSRLS.
create or replace function system_principal(p_org uuid) returns uuid
language sql stable security definer set search_path = public as
$$ select u.id from users u where u.organization_id = p_org and u.is_service and u.name = 'system' limit 1 $$;

-- Claim due outbox messages across orgs; the row lock lasts for the caller's transaction.
create or replace function claim_due_outbox(p_limit int)
returns table (id uuid, organization_id uuid)
language sql volatile security definer set search_path = public as
$$ select o.id, o.organization_id from outbox_messages o
   where o.status = 'pending' and o.next_attempt_at <= now()
   order by o.next_attempt_at limit p_limit for update skip locked $$;

-- Credential reads: worker role only; every read is audited.
create or replace function read_credential(p_account uuid, p_name text) returns bytea
language plpgsql security definer set search_path = public as
$$ declare c bytea; begin
  if not pg_has_role(session_user, 'oms_worker', 'member') then
    raise exception 'credential reads are restricted to the worker role' using errcode = '42501';
  end if;
  select ac.ciphertext into c from account_credentials ac
   where ac.platform_account_id = p_account and ac.name = p_name and ac.organization_id = app_org();
  insert into audit_logs (id, organization_id, created_at, actor_type, action, entity, entity_id, after)
  values (gen_random_uuid(), app_org(), now(), 'system', 'credential.read', 'platform_account', p_account::text,
          jsonb_build_object('name', p_name, 'found', c is not null));
  return c;
end $$;

-- Procurement: purchase lines keep order_items.qty_procured in sync; the UPDATE takes the row lock and the CHECK
-- (procured <= allocated_dropship) rejects over-procurement across concurrent transactions (spec §27.5).
create or replace function sync_qty_procured() returns trigger language plpgsql as
$$ begin
  if tg_op in ('INSERT', 'UPDATE') then
    update order_items set qty_procured = qty_procured + new.qty where id = new.order_item_id;
  end if;
  if tg_op in ('UPDATE', 'DELETE') then
    update order_items set qty_procured = qty_procured - old.qty where id = old.order_item_id;
  end if;
  return null;
end $$;
create trigger purchase_lines_qty after insert or update or delete on purchase_lines
  for each row execute function sync_qty_procured();

-- Closed accounting periods are immutable: postings must go to an open period (adjustments).
create or replace function reject_closed_period() returns trigger
language plpgsql security definer set search_path = public as
$$ begin
  if new.legal_entity_id is not null and exists (
       select 1 from accounting_periods ap where ap.legal_entity_id = new.legal_entity_id
         and new.accounting_date between ap.period_start and ap.period_end and ap.status = 'closed') then
    raise exception 'accounting period for % is closed; post an adjustment in an open period', new.accounting_date
      using errcode = '23514';
  end if;
  return new;
end $$;
create trigger ledger_period_open before insert on ledger_entries
  for each row execute function reject_closed_period();
"""

ORDER_SLICES = r"""
-- Portfolio-limited view of orders: only visible lines, no header totals, address or payment data (spec §27.4).
create view order_slices with (security_barrier = true) as
select o.organization_id, o.id as order_id, o.order_number, o.sales_channel, o.placed_at, o.status as order_status,
       oi.id as order_item_id, oi.portfolio_id, oi.sku_id, oi.qty, oi.unit_price, oi.discount, oi.line_total,
       oi.qty_allocated_owned, oi.qty_allocated_dropship, oi.qty_allocated_platform, oi.qty_procured,
       oi.qty_shipped, oi.qty_delivered, oi.qty_returned, oi.qty_refunded, oi.qty_canceled
from orders o join order_items oi on oi.order_id = o.id
where o.organization_id = app_org() and app_portfolio_visible(oi.portfolio_id);
grant select on order_slices to oms_runtime;
"""


def _tenant_tables():
    return [t for t in metadata.sorted_tables if "organization_id" in t.c and t.name != "organizations"]


def upgrade() -> None:
    op.execute(ROLES)
    op.execute(FUNCTIONS)

    # Privileges: runtime group gets DML on all tables; specific exclusions follow.
    op.execute("grant usage on schema public to oms_runtime, oms_reporting_owner")
    op.execute("grant select, insert, update, delete on all tables in schema public to oms_runtime")
    op.execute("grant usage, select on all sequences in schema public to oms_runtime")
    for t in ("account_credentials", "reporting_principals", "alembic_version"):
        op.execute(f"revoke all on {t} from oms_runtime, oms_app")
    for t in ("audit_logs", "order_events", "ledger_entries", "consent_events", "exception_events",
              "inventory_movements", "shipment_events", "conversion_delivery_logs", "supplier_observations",
              "payment_events", "capital_allocation_events"):
        op.execute(f"revoke update, delete on {t} from oms_runtime, oms_app")
    for t in ("providers", "ledger_accounts"):
        op.execute(f"revoke insert, update, delete on {t} from oms_runtime")
    op.execute("revoke execute on function read_credential(uuid, text) from public")
    op.execute("grant execute on function read_credential(uuid, text) to oms_worker")
    for fn in ("claim_due_outbox(int)", "system_principal(uuid)"):
        op.execute(f"revoke execute on function {fn} from public")
        op.execute(f"grant execute on function {fn} to oms_worker, oms_webhook")
    for fn in ("resolve_platform_account(text, text, text)", "resolve_payment(text, text)",
               "resolve_shipment(text)"):
        op.execute(f"revoke execute on function {fn} from public")
        op.execute(f"grant execute on function {fn} to oms_worker, oms_webhook")

    # RLS for new tables.
    for table in V31_NEW_TABLES:
        kind, col = TABLE_POLICIES[table]
        using, insert_check = visibility_expr(kind, col)
        op.execute(f"alter table {table} enable row level security")
        op.execute(f"alter table {table} force row level security")
        op.execute(f"create policy p_select on {table} for select using ({using})")
        op.execute(f"create policy p_insert on {table} for insert with check ({insert_check})")
        op.execute(f"create policy p_update on {table} for update using ({using}) with check ({using})")
        op.execute(f"create policy p_delete on {table} for delete using ({using})")
    # Policy overrides for existing tables.
    for table, (kind, col) in V31_OVERRIDES.items():
        using, insert_check = visibility_expr(kind, col)
        for p in ("p_select", "p_insert", "p_update", "p_delete"):
            op.execute(f"drop policy if exists {p} on {table}")
        op.execute(f"create policy p_select on {table} for select using ({using})")
        op.execute(f"create policy p_insert on {table} for insert with check ({insert_check})")
        op.execute(f"create policy p_update on {table} for update using ({using}) with check ({using})")
        op.execute(f"create policy p_delete on {table} for delete using ({using})")

    # Tenant-scoped composite foreign keys: a child can only reference a parent in the same organization.
    conn = op.get_bind()
    tenant = {t.name for t in _tenant_tables()}
    parents_done: set[str] = set()
    for t in _tenant_tables():
        for fk in t.foreign_keys:
            parent = fk.column.table
            col = fk.parent.name
            if parent.name not in tenant or col == "organization_id" or fk.column.name != "id":
                continue
            if parent.name not in parents_done:
                exists = conn.execute(sa.text("select 1 from pg_constraint where conname = :n"),
                                      {"n": f"uq_{parent.name}_org_id"}).first()
                if not exists:
                    op.execute(f"alter table {parent.name} add constraint uq_{parent.name}_org_id "
                               f"unique (organization_id, id)")
                parents_done.add(parent.name)
            name = f"fkt_{t.name}_{col}"[:63]
            op.execute(f"alter table {t.name} add constraint {name} foreign key (organization_id, {col}) "
                       f"references {parent.name} (organization_id, id) not valid")
            op.execute(f"alter table {t.name} validate constraint {name}")

    op.execute(ORDER_SLICES)
    op.execute("create schema if not exists reporting")
    op.execute("alter schema reporting owner to oms_reporting_owner")
    op.execute("grant usage on schema reporting to oms_reporting")


def downgrade() -> None:
    op.execute("drop schema if exists reporting cascade")
    op.execute("drop view if exists order_slices")
    conn = op.get_bind()
    for (name, table) in conn.execute(sa.text(
            "select conname, conrelid::regclass::text from pg_constraint where conname like 'fkt\\_%'")).all():
        op.execute(f"alter table {table} drop constraint {name}")
    for (name, table) in conn.execute(sa.text(
            "select conname, conrelid::regclass::text from pg_constraint where conname like 'uq\\_%\\_org\\_id'")).all():
        op.execute(f"alter table {table} drop constraint {name}")
    op.execute("drop trigger if exists ledger_period_open on ledger_entries")
    op.execute("drop trigger if exists purchase_lines_qty on purchase_lines")
    for table in (*V31_NEW_TABLES, *V31_OVERRIDES):
        for p in ("p_select", "p_insert", "p_update", "p_delete"):
            op.execute(f"drop policy if exists {p} on {table}")
