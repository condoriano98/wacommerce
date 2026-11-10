"""Metabase (org-level users only): PII-free monthly contribution waterfall by entity/portfolio/level, built from the
ledger and the cost-to-level map. Same identity model as 0007 (session_user → reporting principal, RLS on base tables).

Revision ID: 0013
Revises: 0012
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("grant select on cost_level_map to oms_reporting_owner")
    op.execute("""
create or replace view reporting.monthly_waterfall with (security_barrier = true) as
select le.organization_id, le.legal_entity_id, le.portfolio_id, date_trunc('month', le.accounting_date)::date as month,
       m.level, sum(le.credit - le.debit) as contribution
from public.ledger_entries le join public.cost_level_map m on m.account_code = le.account_code
group by 1, 2, 3, 4, 5""")
    op.execute("alter view reporting.monthly_waterfall owner to oms_reporting_owner")
    op.execute("grant select on reporting.monthly_waterfall to oms_reporting")


def downgrade() -> None:
    op.execute("drop view if exists reporting.monthly_waterfall")
