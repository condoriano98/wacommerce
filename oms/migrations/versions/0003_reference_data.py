"""Reference data: providers and chart of accounts.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

from app.modules.finance.accounts import ACCOUNTS

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

PROVIDERS = [
    ("whatsapp", "WhatsApp Business Cloud API", "messaging", "v27.0"),
    ("meta", "Meta Marketing API / CAPI", "advertising", "v27.0"),
    ("google_ads", "Google Ads API", "advertising", "v26.1"),
    ("tiktok_ads", "TikTok API for Business", "advertising", "v1.3"),
    ("shopee", "Shopee Open Platform", "sales", "v2"),
    ("tiktokshop", "TikTok Shop / Tokopedia & Shop Partner API", "sales", "202309"),
    ("lazada", "Lazada Open Platform", "sales", None),
    ("shopee_buyer", "Shopee buyer account (human-operated)", "procurement", None),
    ("tokopedia_buyer", "Tokopedia buyer account (human-operated)", "procurement", None),
    ("biteship", "Biteship", "shipping", "v1"),
    ("doku", "DOKU", "payments", None),
    ("chatwoot", "Chatwoot", "messaging", "v1"),
]


def upgrade() -> None:
    providers = sa.table(
        "providers",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("category", sa.String),
        sa.column("default_api_version", sa.String),
    )
    op.bulk_insert(
        providers, [dict(zip(("code", "name", "category", "default_api_version"), p, strict=True)) for p in PROVIDERS]
    )
    accounts = sa.table(
        "ledger_accounts",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("type", sa.String),
        sa.column("is_cash", sa.Boolean),
        sa.column("is_working_capital", sa.Boolean),
    )
    op.bulk_insert(
        accounts,
        [
            {"code": c, "name": n, "type": t, "is_cash": cash, "is_working_capital": wc}
            for c, (n, t, cash, wc) in ACCOUNTS.items()
        ],
    )


def downgrade() -> None:
    op.execute("delete from ledger_accounts")
    op.execute("delete from providers")
