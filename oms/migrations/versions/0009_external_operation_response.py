"""External operations keep the provider's response summary (ADR-015) so a retried job can finish its local work
from the confirmed effect instead of calling the provider again.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("external_operations", sa.Column("response", postgresql.JSONB(), nullable=False,
                                                   server_default=sa.text("'{}'::jsonb")))
    op.add_column("external_operations", sa.Column("subject_type", sa.String(), nullable=True))
    op.add_column("external_operations", sa.Column("subject_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("external_operations", "subject_id")
    op.drop_column("external_operations", "subject_type")
    op.drop_column("external_operations", "response")
