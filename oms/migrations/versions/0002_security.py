"""Database roles, row-level security, append-only tables, journal balance trigger, credential vault.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

from app.core.rls import APPEND_ONLY, GLOBAL_READONLY, NO_APP_ACCESS, TABLE_POLICIES, visibility_expr

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

FUNCTIONS = r"""
create or replace function app_org() returns uuid language sql stable as
$$ select nullif(current_setting('app.org_id', true), '')::uuid $$;

create or replace function app_user() returns uuid language sql stable as
$$ select nullif(current_setting('app.user_id', true), '')::uuid $$;

create or replace function app_has_org_scope() returns boolean
language sql stable security definer set search_path = public as
$$ select exists (select 1 from role_assignments ra
                  where ra.user_id = app_user() and ra.organization_id = app_org()
                    and ra.scope_type = 'organization') $$;

create or replace function app_has_org_role(p_role text) returns boolean
language sql stable security definer set search_path = public as
$$ select exists (select 1 from role_assignments ra
                  where ra.user_id = app_user() and ra.organization_id = app_org()
                    and ra.scope_type = 'organization' and ra.role = p_role) $$;

create or replace function app_portfolio_visible(p uuid) returns boolean
language sql stable security definer set search_path = public as
$$ select app_has_org_scope() or (p is not null and exists (
       select 1 from role_assignments ra
       where ra.user_id = app_user() and ra.organization_id = app_org()
         and ra.scope_type = 'portfolio' and ra.scope_id = p)) $$;

create or replace function app_order_visible(o uuid) returns boolean
language sql stable security definer set search_path = public as
$$ select app_has_org_scope() or exists (
       select 1 from order_items oi where oi.order_id = o and app_portfolio_visible(oi.portfolio_id)) $$;

create or replace function app_customer_visible(c uuid) returns boolean
language sql stable security definer set search_path = public as
$$ select app_has_org_scope() or exists (
       select 1 from customer_portfolios cp where cp.customer_id = c and app_portfolio_visible(cp.portfolio_id)) $$;

create or replace function app_platform_account_visible(a uuid) returns boolean
language sql stable security definer set search_path = public as
$$ select app_has_org_scope()
       or exists (select 1 from account_portfolio_links l
                  where l.platform_account_id = a and app_portfolio_visible(l.portfolio_id))
       or exists (select 1 from role_assignments ra
                  where ra.user_id = app_user() and ra.organization_id = app_org()
                    and ra.scope_type = 'platform_account' and ra.scope_id = a) $$;

-- Login happens before a tenant is known: a narrow definer function returns one user by email.
create or replace function auth_lookup_user(p_email text)
returns table (id uuid, organization_id uuid, password_hash varchar, is_active boolean)
language sql stable security definer set search_path = public as
$$ select u.id, u.organization_id, u.password_hash, u.is_active from users u
   where lower(u.email) = lower(p_email) and not u.is_service $$;

-- Secrets are written only through this function; the app role has no access to the table.
create or replace function store_credential(p_account uuid, p_name text, p_cipher bytea,
                                            p_expires timestamptz)
returns void language plpgsql security definer set search_path = public as
$$
begin
  if not app_has_org_role('owner') then
    raise exception 'credentials.manage permission required' using errcode = '42501';
  end if;
  if not exists (select 1 from platform_accounts pa where pa.id = p_account and pa.organization_id = app_org()) then
    raise exception 'platform account not in organization' using errcode = '42501';
  end if;
  insert into account_credentials (id, organization_id, created_at, platform_account_id, name, ciphertext,
                                   version, expires_at, created_by)
  values (gen_random_uuid(), app_org(), now(), p_account, p_name, p_cipher, 1, p_expires, app_user())
  on conflict (platform_account_id, name) do update
    set ciphertext = excluded.ciphertext, version = account_credentials.version + 1,
        expires_at = excluded.expires_at, created_by = excluded.created_by, created_at = now();
end
$$;

-- Lists credential names/metadata (never ciphertext) for the credentials page.
create or replace function list_credentials(p_account uuid)
returns table (name varchar, version int, expires_at timestamptz, updated_at timestamptz)
language sql stable security definer set search_path = public as
$$ select c.name, c.version, c.expires_at, c.created_at from account_credentials c
   where c.platform_account_id = p_account and c.organization_id = app_org()
     and app_platform_account_visible(p_account) $$;

-- Every journal must balance at commit time.
create or replace function check_journal_balanced() returns trigger
language plpgsql security definer set search_path = public as
$$
declare d numeric; c numeric;
begin
  select coalesce(sum(debit), 0), coalesce(sum(credit), 0) into d, c
  from ledger_entries where journal_id = new.journal_id;
  if d <> c then
    raise exception 'journal % unbalanced: debit % credit %', new.journal_id, d, c using errcode = '23514';
  end if;
  return null;
end
$$;

create constraint trigger ledger_journal_balanced
  after insert on ledger_entries deferrable initially deferred
  for each row execute function check_journal_balanced();
"""


def upgrade() -> None:
    op.execute(
        """
        do $$ begin
          if not exists (select 1 from pg_roles where rolname = 'oms_app') then
            create role oms_app nologin;
          end if;
          if not exists (select 1 from pg_roles where rolname = 'oms_system') then
            create role oms_system nologin bypassrls;
          end if;
        end $$;
        """
    )
    op.execute(FUNCTIONS)

    op.execute("grant usage on schema public to oms_app, oms_system")
    op.execute("grant select, insert, update, delete on all tables in schema public to oms_app, oms_system")
    op.execute("grant usage, select on all sequences in schema public to oms_app, oms_system")
    for table in NO_APP_ACCESS:
        op.execute(f"revoke all on {table} from oms_app")
    for table in APPEND_ONLY:
        op.execute(f"revoke update, delete on {table} from oms_app")
    for table in GLOBAL_READONLY:
        op.execute(f"revoke insert, update, delete on {table} from oms_app")
    op.execute("revoke all on alembic_version from oms_app")

    for table, (kind, col) in TABLE_POLICIES.items():
        using, insert_check = visibility_expr(kind, col)
        op.execute(f"alter table {table} enable row level security")
        op.execute(f"alter table {table} force row level security")
        op.execute(f"create policy p_select on {table} for select using ({using})")
        op.execute(f"create policy p_insert on {table} for insert with check ({insert_check})")
        op.execute(f"create policy p_update on {table} for update using ({using}) with check ({using})")
        op.execute(f"create policy p_delete on {table} for delete using ({using})")

    # Credentials: system role reads them; RLS still pins them to the org when org is set.
    op.execute("alter table account_credentials enable row level security")
    op.execute("create policy p_system on account_credentials using (true)")


def downgrade() -> None:
    for table in TABLE_POLICIES:
        for p in ("p_select", "p_insert", "p_update", "p_delete"):
            op.execute(f"drop policy if exists {p} on {table}")
        op.execute(f"alter table {table} disable row level security")
    op.execute("drop trigger if exists ledger_journal_balanced on ledger_entries")
