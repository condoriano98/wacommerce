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


@router.get("/tools")
def list_tools() -> dict:
    return {"allowed_or_conditional": sorted(agent.SCHEMAS), "human_only": sorted(agent.HUMAN_ONLY)}
