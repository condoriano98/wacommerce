"""Remediation A2 (expand + backfill): database-enforced posting identity.

``journals`` holds one header per journal. ``(organization_id, source_type, source_id)`` is unique for every journal
posted from now on, so two concurrent transactions posting the same business event cannot both succeed: the second
blocks on the unique index and then inserts nothing (``ON CONFLICT DO NOTHING``). Legitimately distinct postings that
belong to one business process already carry distinct keys (per shipment, per revision ``:r{n}``, per approval,
``reversal`` per original journal, intercompany legs by source_type).

Existing data is backfilled. If history already contains two journals for one key (possible before this migration),
the later ones are flagged ``legacy_duplicate`` — excluded from the unique index, never deleted — and reported by the
``legacy_duplicate_postings`` invariant for a human decision (reverse or accept).

Headers are append-only: no UPDATE/DELETE privilege for runtime roles and a trigger that rejects both.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "journals",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("legal_entity_id", sa.UUID(), sa.ForeignKey("legal_entities.id"), nullable=True),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("reverses_journal_id", sa.UUID(), nullable=True),
        sa.Column("legacy_duplicate", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute("alter table journals add constraint uq_journals_org_id unique (organization_id, id)")
    op.execute("create unique index uq_journals_posting_key on journals (organization_id, source_type, source_id) "
               "where not legacy_duplicate")
    op.execute("alter table journals add constraint fkt_journals_legal_entity_id foreign key "
               "(organization_id, legal_entity_id) references legal_entities (organization_id, id)")

    # Backfill: one header per existing journal; later duplicates of a key are flagged, not hidden.
    op.execute("""
        insert into journals (id, organization_id, created_at, legal_entity_id, source_type, source_id,
                              reverses_journal_id, legacy_duplicate)
        select j.journal_id, j.organization_id, j.created_at, j.legal_entity_id, j.source_type, j.source_id,
               j.reverses_journal_id,
               row_number() over (partition by j.organization_id, j.source_type, j.source_id
                                  order by j.created_at, j.journal_id) > 1
        from (select distinct on (journal_id) journal_id, organization_id, created_at, legal_entity_id, source_type,
                     source_id, reverses_journal_id
              from ledger_entries order by journal_id, line_no) j
    """)
    op.execute("alter table ledger_entries add constraint fkt_ledger_entries_journal_id foreign key "
               "(organization_id, journal_id) references journals (organization_id, id) not valid")
    op.execute("alter table ledger_entries validate constraint fkt_ledger_entries_journal_id")

    op.execute("""
        create or replace function reject_journal_change() returns trigger language plpgsql as $$
        begin raise exception 'journals are append-only' using errcode = '42501'; end $$;
    """)
    op.execute("create trigger journals_append_only before update or delete on journals "
               "for each row execute function reject_journal_change()")

    # Runtime roles never write headers directly: ``claim_journal`` inserts one for the caller's organization only
    # (``ON CONFLICT`` under RLS would otherwise require every poster to be able to read every header).
    op.execute("""
        create or replace function claim_journal(p_id uuid, p_legal_entity uuid, p_source_type text, p_source_id text,
                                                 p_reverses uuid) returns boolean
        language plpgsql volatile security definer set search_path = public, pg_temp as $$
        declare n int; org uuid := app_org();
        begin
          if org is null then raise exception 'no tenant context' using errcode = '42501'; end if;
          insert into journals (id, organization_id, created_at, legal_entity_id, source_type, source_id,
                                reverses_journal_id)
          values (p_id, org, now(), p_legal_entity, p_source_type, p_source_id, p_reverses)
          on conflict (organization_id, source_type, source_id) where not legacy_duplicate do nothing;
          get diagnostics n = row_count;
          return n = 1;
        end $$;
    """)
    op.execute("revoke execute on function claim_journal(uuid, uuid, text, text, uuid) from public")
    op.execute("grant execute on function claim_journal(uuid, uuid, text, text, uuid) to oms_runtime")
    op.execute("grant select on journals to oms_runtime")
    op.execute("alter table journals enable row level security")
    op.execute("alter table journals force row level security")
    # Headers carry only event keys; reading them is limited to org-scope roles.
    op.execute("create policy p_select on journals for select using "
               "(organization_id = app_org() and app_has_org_scope())")


def downgrade() -> None:
    op.execute("alter table ledger_entries drop constraint if exists fkt_ledger_entries_journal_id")
    op.execute("drop function if exists claim_journal(uuid, uuid, text, text, uuid)")
    op.drop_table("journals")
    op.execute("drop function if exists reject_journal_change()")
