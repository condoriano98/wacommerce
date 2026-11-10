"""M2 messaging (expand): wa_events (persist-then-ack), message_mappings (echo suppression), message_templates,
media_objects; messages.origin. RLS + composite tenant FKs for the new tables.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import visibility_expr

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# Frozen here (not imported from TABLE_POLICIES) so the migration keeps its meaning.
POLICIES = {
    "wa_events": ("org_scope_read", ""),
    "message_mappings": ("org_scope_read", ""),
    "message_templates": ("platform_account", "platform_account_id"),
    "media_objects": ("org_scope_read", ""),
}
TENANT_FKS = [  # (table, column, parent)
    ("wa_events", "platform_account_id", "platform_accounts"),
    ("message_mappings", "message_id", "messages"),
    ("message_mappings", "platform_account_id", "platform_accounts"),
    ("message_templates", "platform_account_id", "platform_accounts"),
]


def _base() -> list:
    return [sa.Column("id", sa.UUID(), primary_key=True),
            sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))]


def upgrade() -> None:
    op.create_table(
        "wa_events", *_base(),
        sa.Column("platform_account_id", sa.UUID(), sa.ForeignKey("platform_accounts.id"), nullable=False, index=True),
        sa.Column("dedupe_key", sa.String(), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String()),
        sa.UniqueConstraint("organization_id", "dedupe_key"),
    )
    op.create_table(
        "message_mappings", *_base(),
        sa.Column("message_id", sa.UUID(), sa.ForeignKey("messages.id"), nullable=False, unique=True),
        sa.Column("platform_account_id", sa.UUID(), sa.ForeignKey("platform_accounts.id"), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("wamid", sa.String()),
        sa.Column("chatwoot_message_id", sa.BigInteger()),
        sa.Column("source_id", sa.String(), unique=True),
        sa.UniqueConstraint("platform_account_id", "wamid"),
        sa.UniqueConstraint("organization_id", "chatwoot_message_id"),
        sa.CheckConstraint("origin in ('meta','chatwoot','oms')"),
    )
    op.create_table(
        "message_templates", *_base(),
        sa.Column("platform_account_id", sa.UUID(), sa.ForeignKey("platform_accounts.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("language", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("param_count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("platform_account_id", "name", "language"),
    )
    op.create_table(
        "media_objects", *_base(),
        sa.Column("storage_key", sa.String(), nullable=False),
        sa.Column("mime", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("organization_id", "sha256"),
    )
    op.add_column("messages", sa.Column("origin", sa.String(), nullable=True))

    for table, (kind, col) in POLICIES.items():
        using, insert_check = visibility_expr(kind, col)
        op.execute(f"grant select, insert, update, delete on {table} to oms_runtime")
        op.execute(f"alter table {table} enable row level security")
        op.execute(f"alter table {table} force row level security")
        op.execute(f"create policy p_select on {table} for select using ({using})")
        op.execute(f"create policy p_insert on {table} for insert with check ({insert_check})")
        op.execute(f"create policy p_update on {table} for update using ({using}) with check ({using})")
        op.execute(f"create policy p_delete on {table} for delete using ({using})")
    conn = op.get_bind()
    for table, col, parent in TENANT_FKS:
        if not conn.execute(sa.text("select 1 from pg_constraint where conname = :n"),
                            {"n": f"uq_{parent}_org_id"}).first():
            op.execute(f"alter table {parent} add constraint uq_{parent}_org_id unique (organization_id, id)")
        op.execute(f"alter table {table} add constraint fkt_{table}_{col} foreign key (organization_id, {col}) "
                   f"references {parent} (organization_id, id)")


def downgrade() -> None:
    op.drop_column("messages", "origin")
    for table in ("media_objects", "message_templates", "message_mappings", "wa_events"):
        op.drop_table(table)
