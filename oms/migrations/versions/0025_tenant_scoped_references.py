"""Round 2 / P0-1 (expand + backfill + constrain): tenant- and account-scoped external references.

* ``reconciliation_items``: identity becomes (organization, kind, provider account, expected_ref). Provider-issued
  references (payout ids) are only unique per provider account, and nothing is unique across organizations.
  - ``platform_account_id`` is backfilled only where the data supports it: marketplace items from their order;
    bank-payout items from the platform account on the 1050 lines of their source journals.
  - A bank-payout item whose source journals name zero or several accounts is **quarantined**
    (``identity_quarantined``): never auto-matched, reported by an invariant for manual resolution. No account is
    invented.
  - The new unique key is created (NULLS NOT DISTINCT) and validated before the old global one is dropped.
* ``payment_notifications``: (organization, provider, notification_id) — a provider notification id seen by one tenant
  never suppresses another tenant's capture.
* ``external_operations``: (organization, provider, operation, idempotency_key).

Existing rows satisfy the new keys (each new key is a superset of the old one); nothing is deleted.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reconciliation_items", sa.Column("platform_account_id", sa.UUID(), nullable=True))
    op.add_column("reconciliation_items", sa.Column("identity_quarantined", sa.Boolean(), nullable=False,
                                                    server_default=sa.text("false")))
    op.execute("alter table reconciliation_items add constraint fkt_reconciliation_items_platform_account_id "
               "foreign key (organization_id, platform_account_id) references platform_accounts (organization_id, id)")
    # Backfill only what the data proves.
    op.execute("""
        update reconciliation_items ri set platform_account_id = o.platform_account_id
        from orders o
        where ri.kind = 'marketplace' and ri.platform_account_id is null and o.id::text = ri.expected_ref
          and o.organization_id = ri.organization_id
    """)
    op.execute("""
        with src as (
          select ri.id, count(distinct le.platform_account_id) as n, min(le.platform_account_id::text) as acc
          from reconciliation_items ri
          cross join lateral jsonb_array_elements(coalesce(ri.evidence -> 'sources', '[]'::jsonb)) s(x)
          left join ledger_entries le on le.organization_id = ri.organization_id and le.account_code = '1050'
               and le.source_type = s.x ->> 0 and le.source_id = s.x ->> 1
          where ri.kind = 'bank_payout' and ri.platform_account_id is null
          group by ri.id)
        update reconciliation_items ri
           set platform_account_id = case when src.n = 1 then src.acc::uuid end,
               identity_quarantined = (src.n <> 1)
        from src where src.id = ri.id
    """)
    op.execute("""
        update reconciliation_items set identity_quarantined = true
        where kind = 'bank_payout' and platform_account_id is null and not identity_quarantined
    """)
    op.execute("alter table reconciliation_items add constraint uq_reconciliation_items_identity "
               "unique nulls not distinct (organization_id, kind, platform_account_id, expected_ref)")
    op.execute("alter table reconciliation_items drop constraint reconciliation_items_kind_expected_ref_key")

    op.execute("alter table payment_notifications add constraint uq_payment_notifications_org "
               "unique (organization_id, provider, notification_id)")
    op.execute("alter table payment_notifications drop constraint payment_notifications_provider_notification_id_key")

    op.execute("alter table external_operations add constraint uq_external_operations_org_key "
               "unique (organization_id, provider, operation, idempotency_key)")
    op.execute("alter table external_operations drop constraint "
               "external_operations_provider_operation_idempotency_key_key")


def downgrade() -> None:
    # Re-tightening to global keys only succeeds if no two tenants share a reference (it fails loudly otherwise).
    op.execute("alter table external_operations add constraint external_operations_provider_operation_idempotency_key_key "
               "unique (provider, operation, idempotency_key)")
    op.execute("alter table external_operations drop constraint uq_external_operations_org_key")
    op.execute("alter table payment_notifications add constraint payment_notifications_provider_notification_id_key "
               "unique (provider, notification_id)")
    op.execute("alter table payment_notifications drop constraint uq_payment_notifications_org")
    op.execute("alter table reconciliation_items add constraint reconciliation_items_kind_expected_ref_key "
               "unique (kind, expected_ref)")
    op.execute("alter table reconciliation_items drop constraint uq_reconciliation_items_identity")
    op.execute("alter table reconciliation_items drop constraint fkt_reconciliation_items_platform_account_id")
    op.drop_column("reconciliation_items", "identity_quarantined")
    op.drop_column("reconciliation_items", "platform_account_id")
