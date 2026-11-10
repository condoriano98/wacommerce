"""V3.1 data backfill (expand-compatible): chart of accounts, default legal entities, identity domains,
per-org system principals. Idempotent; safe to re-run.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

from app.modules.finance.accounts import ACCOUNTS, INACTIVE

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    for code, (name, typ, cash, wc) in ACCOUNTS.items():
        conn.execute(sa.text(
            "insert into ledger_accounts (code, name, type, is_cash, is_working_capital, active) "
            "values (:c, :n, :t, :cash, :wc, :a) on conflict (code) do update set name = excluded.name, "
            "active = excluded.active"), {"c": code, "n": name, "t": typ, "cash": cash, "wc": wc,
                                         "a": code not in INACTIVE})
    # One default legal entity per organization (G-ACC1/G-TAX1 left 'unconfirmed'/'unknown').
    op.execute("""
        insert into legal_entities (id, organization_id, created_at, name, pkp_status, accounting_framework, base_currency)
        select gen_random_uuid(), o.id, now(), o.name || ' (default entity)', 'unknown', 'unconfirmed', o.base_currency
        from organizations o where not exists (select 1 from legal_entities le where le.organization_id = o.id)""")
    for table in ("brands", "platform_accounts", "inventory_balances", "ledger_entries"):
        op.execute(f"""update {table} t set legal_entity_id = le.id from legal_entities le
                       where le.organization_id = t.organization_id and t.legal_entity_id is null""")
    # Identity domain per brand for WhatsApp BSUIDs/phones (Meta business scope ref = brand until configured).
    op.execute("""
        insert into identity_domains (id, organization_id, created_at, kind, scope_ref, brand_id)
        select gen_random_uuid(), b.organization_id, now(), 'meta_business', b.id::text, b.id from brands b
        where not exists (select 1 from identity_domains d where d.brand_id = b.id and d.kind = 'meta_business')""")
    op.execute("""update customer_identities ci set identity_domain_id = d.id,
                    provider = coalesce(ci.provider, 'whatsapp')
                  from identity_domains d where d.brand_id = ci.brand_id and d.kind = 'meta_business'
                    and ci.identity_domain_id is null""")
    op.execute("""create unique index if not exists uq_identity_domain_value on customer_identities
                  (identity_domain_id, provider, identity_type, identity_value) where identity_domain_id is not null""")
    # Per-org system principal (replaces BYPASSRLS for workers/webhooks).
    op.execute("""
        insert into users (id, organization_id, created_at, email, name, password_hash, is_active, is_service)
        select gen_random_uuid(), o.id, now(), 'system+' || o.id || '@oms.internal', 'system', null, true, true
        from organizations o where not exists
          (select 1 from users u where u.organization_id = o.id and u.is_service and u.name = 'system')""")
    op.execute("""
        insert into role_assignments (id, organization_id, created_at, user_id, role, scope_type, scope_id)
        select gen_random_uuid(), u.organization_id, now(), u.id, 'system_worker', 'organization', null
        from users u where u.is_service and u.name = 'system' and not exists
          (select 1 from role_assignments r where r.user_id = u.id and r.role = 'system_worker')""")


def downgrade() -> None:
    op.execute("drop index if exists uq_identity_domain_value")
