"""Registry state ``verification_pending``: credentials exist but no live authorization + capability check has passed.
``available`` is reserved for accounts whose live check succeeded (M4 review P1).

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

NEW = ("available", "unsupported", "not_authorized", "temporarily_unavailable", "mocked", "planned",
       "verification_pending")


def _drop_access_check():
    conn = op.get_bind()
    for (name,) in conn.execute(sa.text(
            "select conname from pg_constraint where conrelid = 'integration_capabilities'::regclass and contype = 'c' "
            "and pg_get_constraintdef(oid) like '%access_state%'")).all():
        op.execute(f'alter table integration_capabilities drop constraint "{name}"')


def upgrade() -> None:
    _drop_access_check()
    vals = ",".join(f"'{v}'" for v in NEW)
    op.execute(f"alter table integration_capabilities add constraint ck_capability_access_state "
               f"check (access_state in ({vals}))")


def downgrade() -> None:
    _drop_access_check()
    op.execute("update integration_capabilities set access_state = 'not_authorized' "
               "where access_state = 'verification_pending'")
    vals = ",".join(f"'{v}'" for v in NEW[:-1])
    op.execute(f"alter table integration_capabilities add constraint ck_capability_access_state "
               f"check (access_state in ({vals}))")
