"""Agent rate limits (spec §14): the AI principal cannot read ``agent_execution_logs`` (org-scope only), so usage is
counted by a narrow definer function that returns a number for the caller's own organization only.

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
create or replace function agent_usage(p_conversation uuid, p_tool text, p_since timestamptz) returns bigint
language sql stable security definer set search_path = public as
$$ select count(*) from agent_execution_logs l
   where l.organization_id = app_org()
     and (p_conversation is null or l.conversation_id = p_conversation)
     and (p_tool is null or (l.tool = p_tool and l.decision = 'allowed'))
     and l.created_at >= p_since $$""")
    op.execute("revoke execute on function agent_usage(uuid, text, timestamptz) from public")
    op.execute("grant execute on function agent_usage(uuid, text, timestamptz) to oms_runtime")


def downgrade() -> None:
    op.execute("drop function if exists agent_usage(uuid, text, timestamptz)")
