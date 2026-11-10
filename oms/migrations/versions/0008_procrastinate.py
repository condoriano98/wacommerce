"""Procrastinate job queue (ADR-003 revised, D11) in its own schema.

The queue lives in schema ``procrastinate`` so the RLS-restricted API role (``oms_app``) has no access at all: business
code only writes the transactional outbox, and the worker role relays outbox rows into Procrastinate jobs. Queue rows
are infrastructure (task name + ids), never tenant data or PII. Schema SQL comes from the pinned procrastinate package
version, so upgrades of procrastinate need a matching forward migration (procrastinate ships its own migration files).

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

SCHEMA = "procrastinate"


def upgrade() -> None:
    from procrastinate.schema import SchemaManager

    bind = op.get_bind()
    op.execute(f"create schema if not exists {SCHEMA}")
    op.execute(f"set local search_path = {SCHEMA}")
    # psycopg treats % as a placeholder marker when parameters are passed; escape as procrastinate itself does.
    bind.exec_driver_sql(SchemaManager.get_schema().replace("%", "%%"), ())
    op.execute("set local search_path = public")
    # Procrastinate's functions/triggers use unqualified names: pin their search_path so they resolve regardless of the
    # caller's settings (also prevents search_path hijacking).
    op.execute(f"""do $$ declare f record; begin
      for f in select p.oid::regprocedure as sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
               where n.nspname = '{SCHEMA}' loop
        execute format('alter function %s set search_path = {SCHEMA}, pg_temp', f.sig);
      end loop; end $$""")

    op.execute(f"revoke all on schema {SCHEMA} from public")
    op.execute(f"grant usage on schema {SCHEMA} to oms_worker")
    op.execute(f"grant select, insert, update, delete on all tables in schema {SCHEMA} to oms_worker")
    op.execute(f"grant usage, select on all sequences in schema {SCHEMA} to oms_worker")
    op.execute(f"revoke execute on all functions in schema {SCHEMA} from public")
    op.execute(f"grant execute on all functions in schema {SCHEMA} to oms_worker")


def downgrade() -> None:
    op.execute(f"drop schema if exists {SCHEMA} cascade")
