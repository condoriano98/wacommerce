"""Remediation D1 + E2 (expand).

D1: ``capital_commitments.legal_entity_id`` (backfilled from portfolio → brand → legal entity) and
``expected_payment_date``, so the 13-week cash forecast counts only the entity's own obligations, in their week.

E2: ``notification_deliveries`` — one row per risk-alert notification attempt chain (channel, status, attempts,
last error, correlation id). Delivery failures never touch the underlying exception case.

Revision ID: 0024
Revises: 0023
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("capital_commitments", sa.Column("legal_entity_id", sa.UUID(), nullable=True))
    op.add_column("capital_commitments", sa.Column("expected_payment_date", sa.Date(), nullable=True))
    op.execute("""
        update capital_commitments c set legal_entity_id = b.legal_entity_id
        from portfolios p join brands b on b.id = p.brand_id
        where p.id = c.portfolio_id and c.legal_entity_id is null
    """)
    op.execute("alter table capital_commitments add constraint fkt_capital_commitments_legal_entity_id foreign key "
               "(organization_id, legal_entity_id) references legal_entities (organization_id, id)")

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("case_id", sa.UUID(), sa.ForeignKey("exception_cases.id"), nullable=False, index=True),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("recipient_role", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("provider_ref", sa.String(), nullable=True),
        sa.Column("correlation_id", sa.String(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("case_id", "channel"),
        sa.CheckConstraint("status in ('pending','sent','failed','dead')"),
    )
    op.execute("""
        do $$ begin
          if not exists (select 1 from pg_constraint where conrelid = 'exception_cases'::regclass and contype = 'u'
                         and pg_get_constraintdef(oid) = 'UNIQUE (organization_id, id)') then
            alter table exception_cases add constraint uq_exception_cases_org_id unique (organization_id, id);
          end if;
        end $$;
    """)
    op.execute("alter table notification_deliveries add constraint fkt_notification_deliveries_case_id foreign key "
               "(organization_id, case_id) references exception_cases (organization_id, id)")
    op.execute("grant select, insert, update on notification_deliveries to oms_runtime")
    op.execute("alter table notification_deliveries enable row level security")
    op.execute("alter table notification_deliveries force row level security")
    org, scope = "organization_id = app_org()", "organization_id = app_org() and app_has_org_scope()"
    op.execute(f"create policy p_select on notification_deliveries for select using ({scope})")
    op.execute(f"create policy p_insert on notification_deliveries for insert with check ({org})")
    op.execute(f"create policy p_update on notification_deliveries for update using ({scope}) with check ({scope})")


def downgrade() -> None:
    op.drop_table("notification_deliveries")
    op.execute("alter table capital_commitments drop constraint if exists fkt_capital_commitments_legal_entity_id")
    op.drop_column("capital_commitments", "expected_payment_date")
    op.drop_column("capital_commitments", "legal_entity_id")
