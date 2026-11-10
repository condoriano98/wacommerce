"""Remediation B2 (expand): shipment-specific inventory cost.

``shipment_cost_allocations`` records, per shipment line, the exact quantity and cost consumed from each inventory
balance (warehouse) at carrier pickup. Goods in transit, COGS at delivery and the restoration on failed delivery all
use these recorded amounts instead of a single (last-warehouse) unit cost or an arbitrary in-transit balance.

Existing shipments keep their ``shipment_items.unit_cost`` (no backfill is possible for already-consumed stock; the
services fall back to it for shipments picked before this migration).

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

PARENTS = {"shipment_id": "shipments", "shipment_item_id": "shipment_items", "order_item_id": "order_items",
           "balance_id": "inventory_balances", "warehouse_id": "warehouses", "portfolio_id": "portfolios"}


def upgrade() -> None:
    op.execute("alter table shipment_items add constraint uq_shipment_items_org_id unique (organization_id, id)")
    op.create_table(
        "shipment_cost_allocations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("portfolio_id", sa.UUID(), sa.ForeignKey("portfolios.id"), nullable=False, index=True),
        sa.Column("shipment_id", sa.UUID(), sa.ForeignKey("shipments.id"), nullable=False, index=True),
        sa.Column("shipment_item_id", sa.UUID(), sa.ForeignKey("shipment_items.id"), nullable=False, index=True),
        sa.Column("order_item_id", sa.UUID(), sa.ForeignKey("order_items.id"), nullable=False, index=True),
        sa.Column("balance_id", sa.UUID(), sa.ForeignKey("inventory_balances.id"), nullable=False),
        sa.Column("warehouse_id", sa.UUID(), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("unit_cost", sa.Numeric(18, 4), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="in_transit"),
        sa.CheckConstraint("qty > 0"),
        sa.CheckConstraint("amount >= 0"),
        sa.CheckConstraint("status in ('in_transit','delivered','returned')"),
    )
    for col, parent in PARENTS.items():
        op.execute(f"alter table shipment_cost_allocations add constraint fkt_shipment_cost_allocations_{col} "
                   f"foreign key (organization_id, {col}) references {parent} (organization_id, id)")
    op.execute("grant select, insert, update on shipment_cost_allocations to oms_runtime")
    op.execute("alter table shipment_cost_allocations enable row level security")
    op.execute("alter table shipment_cost_allocations force row level security")
    e = "organization_id = app_org() and app_portfolio_visible(portfolio_id)"
    op.execute(f"create policy p_select on shipment_cost_allocations for select using ({e})")
    op.execute(f"create policy p_insert on shipment_cost_allocations for insert with check ({e})")
    op.execute(f"create policy p_update on shipment_cost_allocations for update using ({e}) with check ({e})")


def downgrade() -> None:
    op.drop_table("shipment_cost_allocations")
    op.execute("alter table shipment_items drop constraint if exists uq_shipment_items_org_id")
