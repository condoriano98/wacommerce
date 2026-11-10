"""Remediation A1 (expand): atomic, fenced claiming of external operations.

* ``external_operations.claim_token`` / ``claimed_at``: the worker that wins the conditional transition
  ``prepared → sent`` owns the operation; every later transition is fenced on the token, so a superseded (stale)
  worker can never overwrite the outcome recorded by its successor.
* ``external_operation_attempts``: one row per claimed attempt (attempt number, claim token, outcome). Outcomes are
  updated once on finish; rows are never deleted by the application.

Liveness of a claim is decided by a session-level advisory lock held by the claiming worker for the duration of the
provider call (released by PostgreSQL when the worker's connection dies), see ``app/core/external_ops.py``.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("external_operations", sa.Column("claim_token", sa.UUID(), nullable=True))
    op.add_column("external_operations", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("alter table external_operations add constraint uq_external_operations_org_id "
               "unique (organization_id, id)")
    op.create_table(
        "external_operation_attempts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("operation_id", sa.UUID(), sa.ForeignKey("external_operations.id"), nullable=False, index=True),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.UUID(), nullable=False, unique=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.CheckConstraint("kind in ('call','takeover')"),
        sa.CheckConstraint("outcome is null or outcome in "
                           "('confirmed','failed','not_sent','uncertain','error','fenced','lookup_confirmed',"
                           "'lookup_absent')"),
    )
    op.execute("alter table external_operation_attempts add constraint fkt_external_operation_attempts_operation_id "
               "foreign key (organization_id, operation_id) references external_operations (organization_id, id)")
    op.execute("grant select, insert, update on external_operation_attempts to oms_runtime")
    op.execute("alter table external_operation_attempts enable row level security")
    op.execute("alter table external_operation_attempts force row level security")
    org, scope = "organization_id = app_org()", "organization_id = app_org() and app_has_org_scope()"
    op.execute(f"create policy p_select on external_operation_attempts for select using ({scope})")
    op.execute(f"create policy p_insert on external_operation_attempts for insert with check ({org})")
    op.execute(f"create policy p_update on external_operation_attempts for update using ({scope}) "
               f"with check ({scope})")


def downgrade() -> None:
    op.drop_table("external_operation_attempts")
    op.execute("alter table external_operations drop constraint if exists uq_external_operations_org_id")
    op.drop_column("external_operations", "claimed_at")
    op.drop_column("external_operations", "claim_token")
