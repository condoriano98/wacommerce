"""Provider webhooks and messaging operations. Webhook endpoints do signature checks and persistence only
(persist-then-ack); processing runs in the worker."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from app.core.config import get_settings
from app.core.deps import CurrentPrincipal, DbSession
from app.modules import messaging, provider_webhooks

router = APIRouter(tags=["webhooks"])


@router.get("/webhooks/whatsapp", response_class=PlainTextResponse)
def whatsapp_verify(mode: Annotated[str, Query(alias="hub.mode")], token: Annotated[str, Query(alias="hub.verify_token")],
                    challenge: Annotated[str, Query(alias="hub.challenge")]) -> str:
    if mode != "subscribe" or token != get_settings().meta_verify_token:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "verification failed")
    return challenge


@router.post("/webhooks/whatsapp")
async def whatsapp_receive(request: Request,
                           x_hub_signature_256: Annotated[str | None, Header()] = None) -> dict:
    raw = await request.body()
    return await run_in_threadpool(messaging.receive_whatsapp, raw, x_hub_signature_256)


@router.post("/webhooks/chatwoot")
async def chatwoot_receive(request: Request, x_chatwoot_timestamp: Annotated[str | None, Header()] = None,
                           x_chatwoot_signature: Annotated[str | None, Header()] = None) -> dict:
    raw = await request.body()
    return await run_in_threadpool(messaging.receive_chatwoot, raw, x_chatwoot_timestamp, x_chatwoot_signature)


@router.post("/webhooks/doku")
async def doku_receive(request: Request) -> dict:
    raw = await request.body()
    return await run_in_threadpool(provider_webhooks.receive_doku, raw, dict(request.headers))


@router.post("/webhooks/biteship")
async def biteship_receive(request: Request) -> dict:
    raw = await request.body()
    return await run_in_threadpool(provider_webhooks.receive_biteship, raw)


@router.post("/wa-events/{event_id}/replay", tags=["messaging"], status_code=202)
def replay(event_id: uuid.UUID, session: DbSession, principal: CurrentPrincipal) -> dict:
    messaging.replay_wa_event(session, principal, event_id)
    return {"replayed": str(event_id)}
