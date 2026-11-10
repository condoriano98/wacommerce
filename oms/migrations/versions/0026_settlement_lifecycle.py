"""Round 2 / P0-3 (expand + backfill): settlement lifecycle.

* Account **2400 Unapplied settlement receipts (suspense)** — money a provider paid that no open receivable explains
  (duplicate payout under a new id, excess payout). It is never used to clear a receivable twice; a case is opened.
* ``settlements.content_sha256`` — a replay with the same payout id and the same content is a no-op; the same id with
  different content is flagged, never re-posted.
* ``settlement_lines.line_no`` with ``UNIQUE(settlement_id, line_no)`` and an append-only trigger: raw provider lines
  are immutable evidence. Existing lines are numbered in creation order.
* ``orders.settlement_status`` vocabulary: none | expected | partially_settled | matched | variance | under_review |
  adjusted | closed. Legacy ``settled`` → ``matched``, ``mismatch`` → ``variance``.

Revision ID: 0026
Revises: 0025
"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(sa.text(
        "insert into ledger_accounts (code, name, type, is_cash, is_working_capital, active) "
        "values ('2400', 'Unapplied settlement receipts (suspense)', 'liability', false, true, true) "
        "on conflict (code) do update set name = excluded.name, active = true"))
    op.add_column("settlements", sa.Column("content_sha256", sa.String(), nullable=True))
    op.add_column("settlement_lines", sa.Column("line_no", sa.Integer(), nullable=True))
    op.execute("""
        update settlement_lines sl set line_no = x.n
        from (select id, row_number() over (partition by settlement_id order by created_at, id) as n
              from settlement_lines) x
        where x.id = sl.id
    """)
    op.alter_column("settlement_lines", "line_no", nullable=False)
    op.execute("alter table settlement_lines add constraint uq_settlement_lines_line unique (settlement_id, line_no)")
    op.execute("""
        create or replace function reject_settlement_line_change() returns trigger language plpgsql as $$
        begin raise exception 'settlement lines are immutable evidence' using errcode = '42501'; end $$;
    """)
    op.execute("create trigger settlement_lines_immutable before update or delete on settlement_lines "
               "for each row execute function reject_settlement_line_change()")
    op.execute("update orders set settlement_status = 'matched' where settlement_status = 'settled'")
    op.execute("update orders set settlement_status = 'variance' where settlement_status = 'mismatch'")
    op.execute("alter table orders add constraint ck_orders_settlement_status check (settlement_status in "
               "('none','expected','partially_settled','matched','variance','under_review','adjusted','closed'))")


def downgrade() -> None:
    op.execute("alter table orders drop constraint if exists ck_orders_settlement_status")
    op.execute("update orders set settlement_status = 'settled' where settlement_status in ('matched','closed','adjusted')")
    op.execute("update orders set settlement_status = 'mismatch' where settlement_status in ('variance','under_review')")
    op.execute("drop trigger if exists settlement_lines_immutable on settlement_lines")
    op.execute("drop function if exists reject_settlement_line_change()")
    op.execute("alter table settlement_lines drop constraint if exists uq_settlement_lines_line")
    op.drop_column("settlement_lines", "line_no")
    op.drop_column("settlements", "content_sha256")
    op.execute("update ledger_accounts set active = false where code = '2400'")
