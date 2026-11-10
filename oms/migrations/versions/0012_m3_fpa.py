"""M3 FP&A (expand): cost-to-level map, effective-dated campaign allocations, immutable FP&A snapshots, simulation runs.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# Spec §15: each P&L account appears at exactly one waterfall level (frozen here).
LEVELS = [
    ("4000", "gross_sales", 10), ("4100", "gross_sales", 11), ("4900", "returns_allowances", 20),
    ("5000", "cm1", 30), ("5100", "cm1", 31), ("5200", "cm1", 32), ("5300", "cm1", 33), ("5400", "cm1", 34),
    ("6000", "cm2", 40),
    ("5500", "cm3", 50), ("5600", "cm3", 51), ("5700", "cm3", 52),
    ("6900", "opex", 60),
]
ORG = "organization_id = app_org()"


def upgrade() -> None:
    op.create_table(
        "cost_level_map",
        sa.Column("account_code", sa.String(), sa.ForeignKey("ledger_accounts.code"), primary_key=True),
        sa.Column("level", sa.String(), nullable=False),
        sa.Column("sort", sa.Integer(), nullable=False),
        sa.CheckConstraint("level in ('gross_sales','returns_allowances','cm1','cm2','cm3','opex')"),
    )
    op.bulk_insert(sa.table("cost_level_map", sa.column("account_code"), sa.column("level"), sa.column("sort")),
                   [{"account_code": a, "level": lv, "sort": s} for a, lv, s in LEVELS])
    op.execute("grant select on cost_level_map to oms_runtime, oms_reporting_owner")
    op.execute("revoke insert, update, delete on cost_level_map from oms_runtime")

    # Effective-dated allocations (#12): a policy change is a new version, never an in-place edit of history.
    op.add_column("campaign_portfolio_allocations", sa.Column("effective_from", sa.Date(), nullable=False,
                                                               server_default=sa.text("'2000-01-01'")))
    op.add_column("campaign_portfolio_allocations", sa.Column("effective_to", sa.Date(), nullable=True))
    op.add_column("campaign_portfolio_allocations", sa.Column("policy_version", sa.Integer(), nullable=False,
                                                               server_default="1"))
    op.drop_constraint("campaign_portfolio_allocations_campaign_id_portfolio_id_key",
                       "campaign_portfolio_allocations", type_="unique")
    op.create_unique_constraint("uq_cpa_campaign_portfolio_from", "campaign_portfolio_allocations",
                                ["campaign_id", "portfolio_id", "effective_from"])
    op.create_check_constraint("ck_cpa_effective_range", "campaign_portfolio_allocations",
                               "effective_to is null or effective_to > effective_from")

    op.create_table(
        "fpa_snapshots",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("legal_entity_id", sa.UUID(), sa.ForeignKey("legal_entities.id"), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("portfolio_id", sa.UUID(), sa.ForeignKey("portfolios.id"), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.CheckConstraint("scope_type in ('portfolio','legal_entity')"),
        sa.CheckConstraint("(scope_type = 'portfolio') = (portfolio_id is not null)"),
    )
    op.create_index("uq_fpa_snapshot_version", "fpa_snapshots",
                    ["legal_entity_id", "scope_type", sa.text("coalesce(portfolio_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
                     "period_start", "version"], unique=True)
    op.execute("""create or replace function reject_snapshot_change() returns trigger language plpgsql as
        $$ begin raise exception 'fpa_snapshots are immutable' using errcode = '42501'; end $$""")
    op.execute("create trigger fpa_snapshots_immutable before update or delete on fpa_snapshots "
               "for each row execute function reject_snapshot_change()")

    op.create_table(
        "simulation_runs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("portfolio_id", sa.UUID(), sa.ForeignKey("portfolios.id"), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("scenario", sa.String(), nullable=False),
        sa.Column("engine_version", sa.String(), nullable=False),
        sa.Column("inputs", postgresql.JSONB(), nullable=False),
        sa.Column("outputs", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.CheckConstraint("kind in ('base','stress','forecast')"),
    )

    snap = (f"{ORG} and ((scope_type = 'portfolio' and app_portfolio_visible(portfolio_id)) "
            f"or (scope_type = 'legal_entity' and app_has_org_scope()))")
    sim = f"{ORG} and ((portfolio_id is null and app_has_org_scope()) or app_portfolio_visible(portfolio_id))"
    for table, expr in (("fpa_snapshots", snap), ("simulation_runs", sim)):
        op.execute(f"grant select, insert on {table} to oms_runtime")
        op.execute(f"alter table {table} enable row level security")
        op.execute(f"alter table {table} force row level security")
        op.execute(f"create policy p_select on {table} for select using ({expr})")
        op.execute(f"create policy p_insert on {table} for insert with check ({expr})")
    for table, col, parent in (("fpa_snapshots", "portfolio_id", "portfolios"),
                               ("fpa_snapshots", "legal_entity_id", "legal_entities"),
                               ("simulation_runs", "portfolio_id", "portfolios")):
        op.execute(f"alter table {table} add constraint fkt_{table}_{col} foreign key (organization_id, {col}) "
                   f"references {parent} (organization_id, id)")


def downgrade() -> None:
    op.drop_table("simulation_runs")
    op.execute("drop trigger if exists fpa_snapshots_immutable on fpa_snapshots")
    op.drop_table("fpa_snapshots")
    op.execute("drop function if exists reject_snapshot_change()")
    op.drop_constraint("ck_cpa_effective_range", "campaign_portfolio_allocations", type_="check")
    op.drop_constraint("uq_cpa_campaign_portfolio_from", "campaign_portfolio_allocations", type_="unique")
    op.create_unique_constraint("campaign_portfolio_allocations_campaign_id_portfolio_id_key",
                                "campaign_portfolio_allocations", ["campaign_id", "portfolio_id"])
    for c in ("policy_version", "effective_to", "effective_from"):
        op.drop_column("campaign_portfolio_allocations", c)
    op.drop_table("cost_level_map")
