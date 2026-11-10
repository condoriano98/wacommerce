"""M4 (expand): single-use token rotation for marketplace credentials and the CSV import framework.

* ``credential_for_rotation`` takes a transaction-scoped advisory lock per (account, credential) and returns the current
  ciphertext and version; ``rotate_credential`` writes the new value only if the version is unchanged (compare-and-set).
  The lock is held across the provider refresh call, so a single-use refresh token (Shopee) is never spent twice.
  Worker role only; every rotation is audited (values never logged).
* ``import_batches`` / ``import_rows``: provenance (file sha256, uploader, source), row fingerprints for duplicate
  prevention across batches, dry-run vs committed.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

FUNCS = r"""
create or replace function credential_for_rotation(p_account uuid, p_name text, out ciphertext bytea, out version int)
language plpgsql security definer set search_path = public as
$$ begin
  if not pg_has_role(session_user, 'oms_worker', 'member') then
    raise exception 'credential rotation is restricted to the worker role' using errcode = '42501';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_account::text || ':' || p_name, 0));
  select ac.ciphertext, ac.version into ciphertext, version from account_credentials ac
   where ac.platform_account_id = p_account and ac.name = p_name and ac.organization_id = app_org();
end $$;

create or replace function rotate_credential(p_account uuid, p_name text, p_expected_version int, p_cipher bytea,
                                             p_expires timestamptz) returns boolean
language plpgsql security definer set search_path = public as
$$ declare n int; begin
  if not pg_has_role(session_user, 'oms_worker', 'member') then
    raise exception 'credential rotation is restricted to the worker role' using errcode = '42501';
  end if;
  if p_expected_version is null then
    insert into account_credentials (id, organization_id, created_at, platform_account_id, name, ciphertext, version,
                                     expires_at)
    values (gen_random_uuid(), app_org(), now(), p_account, p_name, p_cipher, 1, p_expires)
    on conflict (platform_account_id, name) do nothing;
  else
    update account_credentials set ciphertext = p_cipher, version = version + 1, expires_at = p_expires
     where platform_account_id = p_account and name = p_name and organization_id = app_org()
       and version = p_expected_version;
  end if;
  get diagnostics n = row_count;
  insert into audit_logs (id, organization_id, created_at, actor_type, action, entity, entity_id, after)
  values (gen_random_uuid(), app_org(), now(), 'system', 'credential.rotated', 'platform_account', p_account::text,
          jsonb_build_object('name', p_name, 'applied', n = 1, 'from_version', p_expected_version));
  return n = 1;
end $$;

-- Worker scheduling across organizations without BYPASSRLS: identifiers only, no tenant data.
create or replace function sync_targets() returns table (organization_id uuid, platform_account_id uuid, provider text)
language sql stable security definer set search_path = public as
$$ select pa.organization_id, pa.id, pa.provider from platform_accounts pa
   where pa.provider in ('shopee','tiktokshop','lazada','meta','google_ads','tiktok_ads') and pa.status <> 'revoked' $$;
"""


def upgrade() -> None:
    op.execute(FUNCS)
    for fn in ("credential_for_rotation(uuid, text)", "rotate_credential(uuid, text, int, bytea, timestamptz)",
               "sync_targets()"):
        op.execute(f"revoke execute on function {fn} from public")
        op.execute(f"grant execute on function {fn} to oms_worker")

    op.create_table(
        "import_batches",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("platform_account_id", sa.UUID(), sa.ForeignKey("platform_accounts.id"), nullable=True),
        sa.Column("file_name", sa.String(), nullable=False),
        sa.Column("file_sha256", sa.String(), nullable=False),
        sa.Column("uploaded_by", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("stats", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("errors", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.CheckConstraint("status in ('dry_run','committed','rejected')"),
    )
    op.create_table(
        "import_rows",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("batch_id", sa.UUID(), sa.ForeignKey("import_batches.id"), nullable=False, index=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("result", sa.String(), nullable=False),
        sa.UniqueConstraint("organization_id", "source", "fingerprint"),
    )
    org = "organization_id = app_org() and app_has_org_scope()"
    for table in ("import_batches", "import_rows"):
        op.execute(f"grant select, insert, update on {table} to oms_runtime")
        op.execute(f"alter table {table} enable row level security")
        op.execute(f"alter table {table} force row level security")
        op.execute(f"create policy p_all on {table} using ({org}) with check ({org})")
    op.execute("alter table import_batches add constraint uq_import_batches_org_id unique (organization_id, id)")
    op.execute("alter table import_rows add constraint fkt_import_rows_batch_id foreign key (organization_id, batch_id) "
               "references import_batches (organization_id, id)")


def downgrade() -> None:
    op.drop_table("import_rows")
    op.drop_table("import_batches")
    op.execute("drop function if exists rotate_credential(uuid, text, int, bytea, timestamptz)")
    op.execute("drop function if exists credential_for_rotation(uuid, text)")
    op.execute("drop function if exists sync_targets()")
