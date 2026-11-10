"""Agent tool API (called by the n8n AI Sales Admin workflow with the AI service principal's token)."""

import uuid

from fastapi import APIRouter, Body

from app.core.deps import CurrentPrincipal, DbSession
from app.modules import agent

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/conversations/{conversation_id}/tools/{tool}")
def call_tool(conversation_id: uuid.UUID, tool: str, session: DbSession, principal: CurrentPrincipal,
              params: dict = Body(default_factory=dict)) -> dict:  # noqa: B008
    return agent.run_tool(session, principal, conversation_id, tool, params)


@router.get("/conversations/by-chatwoot/{chatwoot_conversation_id}")
def conversation_by_chatwoot(chatwoot_conversation_id: int, session: DbSession, principal: CurrentPrincipal) -> dict:
    """n8n receives Chatwoot ids; the AI may only resolve conversations it can see (RLS) and that are AI-enabled."""
    from fastapi import HTTPException, status
    from sqlalchemy import select

    from app.models.customers import Conversation

    conv = session.scalars(select(Conversation).where(
        Conversation.chatwoot_conversation_id == chatwoot_conversation_id)).first()
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return {"conversation_id": str(conv.id), "ai_enabled": conv.ai_enabled}


@router.get("/tools")
def list_tools(principal: CurrentPrincipal) -> dict:
    return {"allowed_or_conditional": sorted(agent.SCHEMAS), "human_only": sorted(agent.HUMAN_ONLY)}
