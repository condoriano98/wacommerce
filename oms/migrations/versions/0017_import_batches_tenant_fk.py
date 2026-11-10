"""Tenant-scoped FK for import_batches.platform_account_id (found by the M5 FK catalogue test; 0014 omitted it).

Revision ID: 0017
Revises: 0016
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table import_batches add constraint fkt_import_batches_platform_account_id foreign key "
               "(organization_id, platform_account_id) references platform_accounts (organization_id, id) not valid")
    op.execute("alter table import_batches validate constraint fkt_import_batches_platform_account_id")


def downgrade() -> None:
    op.execute("alter table import_batches drop constraint if exists fkt_import_batches_platform_account_id")
