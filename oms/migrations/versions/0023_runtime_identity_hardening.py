"""Remediation C3: runtime tenant-identity hardening.

* ``app_org()`` returns the transaction's organization only if the transaction's user is an active member of it.
  A real user claiming another organization's id, or an organization with no user, gets **no tenant** (NULL), so
  every org-matched policy (including the plain ``organization_id = app_org()`` ones and INSERT checks) fails closed.
  Reporting logins keep their ``session_user`` mapping (ADR-016).
* Every SECURITY DEFINER function pins ``search_path = public, pg_temp`` and is no longer executable by PUBLIC:
  visibility helpers are granted to the runtime and reporting roles, everything else only to the roles that call it.
* ``resolve_shipment`` refuses ambiguous carrier references (the same external id in two tenants resolves to
  nothing instead of an arbitrary tenant).

Residual (documented, gate G-SEC1): a holder of the ``oms_app`` database credentials can still claim a matching
(user, organization) pair. Removing that trust requires per-tenant database roles or an identity service.

Revision ID: 0023
Revises: 0022
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

APP_ORG = r"""
create or replace function app_org() returns uuid
language plpgsql stable security definer set search_path = public, pg_temp as
$$ declare r record; o uuid; u uuid; begin
  select * into r from reporting_identity();
  if r.org is not null then return r.org; end if;
  o := nullif(current_setting('app.org_id', true), '')::uuid;
  u := nullif(current_setting('app.user_id', true), '')::uuid;
  if o is null or u is null then return null; end if;
  if exists (select 1 from users where id = u and organization_id = o and is_active) then return o; end if;
  return null;
end $$;
"""

OLD_APP_ORG = r"""
create or replace function app_org() returns uuid language plpgsql stable as
$$ declare r record; begin
  select * into r from reporting_identity();
  if r.org is not null then return r.org; end if;
  return nullif(current_setting('app.org_id', true), '')::uuid;
end $$;
"""

RESOLVE_SHIPMENT = r"""
create or replace function resolve_shipment(p_external_id text, out organization_id uuid, out shipment_id uuid)
language sql stable security definer set search_path = public, pg_temp as
$$ select s.organization_id, s.id from shipments s
   where s.external_id = p_external_id
     and not exists (select 1 from shipments s2 where s2.external_id = p_external_id and s2.id <> s.id) $$;
"""

VISIBILITY = ("app_org()", "app_has_org_scope()", "app_has_org_role(text)", "app_portfolio_visible(uuid)",
              "app_order_visible(uuid)", "app_customer_visible(uuid)", "app_platform_account_visible(uuid)",
              "app_shipment_visible(uuid)", "app_legal_entity_visible(uuid)", "reporting_identity()")
RUNTIME_ONLY = ("auth_lookup_user(text)", "store_credential(uuid, text, bytea, timestamp with time zone)",
                "list_credentials(uuid)", "enqueue_outbox(text, text, jsonb, timestamp with time zone)")
TRIGGERS = ("check_journal_balanced()", "reject_closed_period()")


def upgrade() -> None:
    op.execute(APP_ORG)
    op.execute(RESOLVE_SHIPMENT)
    op.execute("""
        do $$ declare f record; begin
          for f in select p.oid::regprocedure as sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                   where n.nspname = 'public' and p.prosecdef loop
            execute format('alter function %s set search_path = public, pg_temp', f.sig);
            execute format('revoke execute on function %s from public', f.sig);
          end loop;
        end $$;
    """)
    # oms_system only exists in legacy installations (contract migration pending).
    legacy = "do $$ begin if exists (select 1 from pg_roles where rolname = 'oms_system') then execute '{}'; end if; end $$;"
    for fn in VISIBILITY:
        op.execute(f"grant execute on function {fn} to oms_runtime, oms_reporting, oms_reporting_owner")
        op.execute(legacy.format(f"grant execute on function {fn} to oms_system"))
    for fn in RUNTIME_ONLY:
        op.execute(f"grant execute on function {fn} to oms_runtime")
        op.execute(legacy.format(f"grant execute on function {fn} to oms_system"))
    for fn in ("resolve_shipment(text)",):
        op.execute(f"grant execute on function {fn} to oms_worker, oms_webhook")


def downgrade() -> None:
    op.execute(OLD_APP_ORG)
    op.execute("""
        create or replace function resolve_shipment(p_external_id text, out organization_id uuid,
                                                    out shipment_id uuid)
        language sql stable security definer set search_path = public as
        $$ select s.organization_id, s.id from shipments s where s.external_id = p_external_id limit 1 $$;
    """)
    for fn in VISIBILITY + RUNTIME_ONLY + TRIGGERS:
        if fn != "app_org()":
            op.execute(f"grant execute on function {fn} to public")
