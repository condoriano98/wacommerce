"""M5 operations: aggregate-only metrics and organization enumeration for scheduled invariant checks.

``ops_metrics()`` returns counts/amounts across all tenants **without identifiers** (monitoring must see backlog even
though no runtime role bypasses RLS). ``organization_ids()`` lets the worker iterate organizations for invariant
checks, each of which then runs under that organization's system principal (RLS applies).

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
create or replace function ops_metrics() returns table (metric text, label text, value numeric)
language sql stable security definer set search_path = public as
$$
  select 'outbox_pending', '', count(*)::numeric from outbox_messages where status = 'pending'
  union all
  select 'outbox_oldest_due_seconds', '', coalesce(extract(epoch from now() - min(next_attempt_at)), 0)::numeric
    from outbox_messages where status = 'pending' and next_attempt_at <= now()
  union all
  select 'dead_letters_open', topic, count(*)::numeric from dead_letters where resolved_at is null group by topic
  union all
  select 'external_operations', provider || ':' || status, count(*)::numeric from external_operations
   where status in ('sent','uncertain','failed') group by provider, status
  union all
  select 'integration_errors_24h', kind, count(*)::numeric from integration_errors
   where created_at > now() - interval '24 hours' group by kind
  union all
  select 'reconciliation_open_exposure', kind, coalesce(sum(abs(expected_amount - coalesce(actual_amount, 0))), 0)
    from reconciliation_items where status in ('open','variance') group by kind
  union all
  select 'exception_cases_open', severity, count(*)::numeric from exception_cases
   where status not in ('resolved','written_off') group by severity
  union all
  select 'jobs_stalled', '', count(*)::numeric from procrastinate.procrastinate_jobs j
    join procrastinate.procrastinate_workers w on w.id = j.worker_id
   where j.status = 'doing' and w.last_heartbeat < now() - interval '60 seconds'
  union all
  select 'schema_version', (select version_num from alembic_version limit 1), 1
$$;
create or replace function organization_ids() returns setof uuid
language sql stable security definer set search_path = public as $$ select id from organizations $$;
""")
    op.execute("revoke execute on function ops_metrics() from public")
    op.execute("grant execute on function ops_metrics() to oms_app, oms_worker")
    op.execute("revoke execute on function organization_ids() from public")
    op.execute("grant execute on function organization_ids() to oms_worker")


def downgrade() -> None:
    op.execute("drop function if exists ops_metrics()")
    op.execute("drop function if exists organization_ids()")
