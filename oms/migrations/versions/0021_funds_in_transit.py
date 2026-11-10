"""Remediation B5 (expand): account 1050 "Funds in transit to bank (unverified payouts)".

Provider-reported payouts (gateway disbursements, carrier withdrawals, marketplace payouts, supplier refunds) are
booked to 1050 and move to 1000 (verified cash) only when a bank statement line proves receipt. Existing journals
are not rewritten (append-only ledger); payouts booked to 1000 before this migration stay as they are.

Policy default pending accountant confirmation (G-ACC1).

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(sa.text(
        "insert into ledger_accounts (code, name, type, is_cash, is_working_capital, active) "
        "values ('1050', 'Funds in transit to bank (unverified payouts)', 'asset', false, true, true) "
        "on conflict (code) do update set name = excluded.name, active = true"))


def downgrade() -> None:
    op.execute("update ledger_accounts set active = false where code = '1050'")
