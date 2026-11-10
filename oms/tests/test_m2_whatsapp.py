"""M2 WhatsApp bridge + Chatwoot API channel (acceptance #5 Meta part, #15; spec §12).

Every provider is mocked. Meta webhooks are posted over HTTP with a valid ``X-Hub-Signature-256``; processing runs
through the outbox (``dispatch_due``) exactly as the worker does."""

import hashlib
import hmac
import json
import time
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text

import app.registry  # noqa: F401
from app.core.config import get_settings
from app.core.db import tenant_session
from app.core.outbox import dispatch_due
from app.integrations import mockstore
from app.integrations.chatwoot import MockChatwoot
from app.integrations.whatsapp import MockWhatsAppSender
from app.models.base import utcnow
from app.models.customers import Conversation, Message
from app.models.governance import ExternalOperation
from app.models.messaging import MessageMapping, MessageTemplate, WaEvent
from app.models.platforms import PlatformAccount
from app.modules import messaging
from tests.conftest import owner_session
from tests.world import build_world, principal


@pytest.fixture(autouse=True)
def _fresh_providers():
    mockstore.reset()
    yield
    mockstore.reset()


def _pnid(w) -> str:
    with owner_session() as s:
        return s.get(PlatformAccount, w.wa_account).public_config["phone_number_id"]


def _inbox(t) -> int:
    """Register the org's Chatwoot API-channel inbox (mock)."""
    inbox = int(t.org_id.int % 10**8)
    with owner_session() as s:
        s.add(PlatformAccount(organization_id=t.org_id, brand_id=t.brand_id, provider="chatwoot",
                              provider_account_id=f"cw-{inbox}", display_name="Chatwoot", role="messaging",
                              public_config={"inbox_id": str(inbox)}))
    return inbox


def wa_message(pnid, *, wamid=None, bsuid="ID.cust1", text_body="halo", ts=None) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": pnid},
        "contacts": [{"user_id": bsuid, "profile": {"name": "Budi"}}],
        "messages": [{"from_user_id": bsuid, "id": wamid or f"wamid.in.{uuid.uuid4().hex[:10]}",
                      "timestamp": str(int(ts or time.time())), "type": "text", "text": {"body": text_body}}]}}]}]}


def wa_status(pnid, wamid, st, ts=None) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": pnid},
        "statuses": [{"id": wamid, "status": st, "timestamp": str(int(ts or time.time()))}]}}]}]}


def post_meta(client, payload: dict, *, secret=None):
    raw = json.dumps(payload).encode()
    sig = hmac.new((secret or get_settings().meta_app_secret).encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": f"sha256={sig}",
                                                                   "Content-Type": "application/json"})


def post_chatwoot(client, payload: dict):
    raw = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = hmac.new(get_settings().chatwoot_webhook_secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/chatwoot", content=raw, headers={"X-Chatwoot-Timestamp": ts,
                                                                   "X-Chatwoot-Signature": f"sha256={sig}"})


def _count(model, *where) -> int:
    with owner_session() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def _conv(w, bsuid="ID.cust1") -> Conversation:
    with owner_session() as s:
        cid = s.execute(text("select customer_id from customer_identities where identity_value = :v"),
                        {"v": bsuid}).scalar()
        return s.scalars(select(Conversation).where(Conversation.platform_account_id == w.wa_account,
                                                    Conversation.customer_id == cid)).one()


def test_5_meta_webhook_is_persisted_before_ack_and_duplicates_collapse(tenant, client):
    w = build_world(tenant)
    pnid = _pnid(w)
    payload = wa_message(pnid, wamid="wamid.in.dup1", bsuid="ID.dup1")
    first = post_meta(client, payload)
    again = post_meta(client, payload)  # Meta re-delivery of the same body
    assert first.status_code == 200 and first.json()["stored"] == 1
    assert again.json() == {"stored": 0, "duplicates": 1, "unknown_numbers": 0}
    # Persisted, acknowledged, not yet processed: no business rows exist before the worker runs.
    assert _count(WaEvent, WaEvent.organization_id == tenant.org_id) == 1
    assert _count(Message, Message.organization_id == tenant.org_id) == 0
    dispatch_due()
    assert _count(Message, Message.organization_id == tenant.org_id, Message.direction == "inbound") == 1
    with owner_session() as s:
        mp = s.scalars(select(MessageMapping).join(Message, Message.id == MessageMapping.message_id)
                       .where(Message.external_id == "wamid.in.dup1")).one()
        assert mp.origin == "meta" and mp.source_id == f"oms:{mp.message_id}" and mp.chatwoot_message_id
        ev = s.scalars(select(WaEvent).where(WaEvent.organization_id == tenant.org_id)).one()
        assert ev.processed_at is not None
    # A different envelope carrying the same wamid (Meta retry with new metadata) is also deduplicated.
    post_meta(client, {**payload, "object": "whatsapp_business_account", "entry": payload["entry"] * 1,
                       "_retry": 1})
    dispatch_due()
    assert _count(Message, Message.organization_id == tenant.org_id, Message.direction == "inbound") == 1
    assert len([m for m in MockChatwoot().messages if m["message_type"] == "incoming"]) == 1


def test_bad_signature_and_unknown_number_store_nothing(tenant, client):
    w = build_world(tenant)
    assert post_meta(client, wa_message(_pnid(w)), secret="wrong").status_code == 401
    assert post_meta(client, wa_message("999000111")).json()["unknown_numbers"] == 1
    assert _count(WaEvent, WaEvent.organization_id == tenant.org_id) == 0


def test_5_out_of_order_statuses_never_move_backwards_and_replay_is_idempotent(tenant, client):
    w = build_world(tenant)
    pnid = _pnid(w)
    post_meta(client, wa_message(pnid, bsuid="ID.ooo"))
    dispatch_due()
    conv = _conv(w, "ID.ooo")
    cs = principal(tenant, "owner")
    with tenant_session(tenant.org_id, cs.user_id) as s:
        msg = messaging.queue_outbound(s, s.get(Conversation, conv.id), "Halo kak", author="human")
        mid = msg.id
    dispatch_due()
    with owner_session() as s:
        wamid = s.get(Message, mid).external_id
    for st in ("read", "delivered", "sent", "delivered"):  # arrive out of order
        post_meta(client, wa_status(pnid, wamid, st))
    dispatch_due()
    with owner_session() as s:
        assert s.get(Message, mid).status == "read"
        ev_id = s.scalars(select(WaEvent.id).where(WaEvent.organization_id == tenant.org_id)
                          .order_by(WaEvent.received_at)).first()
    r = client.post(f"/wa-events/{ev_id}/replay", headers=tenant.headers("owner"))
    assert r.status_code == 202, r.text
    dispatch_due()
    assert _count(Message, Message.organization_id == tenant.org_id, Message.direction == "inbound") == 1
    assert len([m for m in MockWhatsAppSender().sent if m["pnid"] == pnid]) == 1


def test_service_window_and_template_registry(tenant, client):
    w = build_world(tenant)
    pnid = _pnid(w)
    post_meta(client, wa_message(pnid, bsuid="ID.win"))
    dispatch_due()
    conv = _conv(w, "ID.win")
    with owner_session() as s:
        s.get(Conversation, conv.id).last_inbound_at = utcnow() - timedelta(hours=25)
        s.add(MessageTemplate(organization_id=tenant.org_id, platform_account_id=w.wa_account, name="order_update",
                              language="id", category="utility", status="approved", param_count=1))
        s.add(MessageTemplate(organization_id=tenant.org_id, platform_account_id=w.wa_account, name="promo",
                              language="id", category="marketing", status="pending", param_count=0))
    owner = principal(tenant, "owner")
    for call in (lambda s, c: messaging.queue_outbound(s, c, "free text", author="human"),
                 lambda s, c: messaging.queue_template(s, c, "promo", "id", [], author="system"),
                 lambda s, c: messaging.queue_template(s, c, "order_update", "id", [], author="system")):
        with pytest.raises(HTTPException):
            with tenant_session(tenant.org_id, owner.user_id) as s:
                call(s, s.get(Conversation, conv.id))
    with tenant_session(tenant.org_id, owner.user_id) as s:
        messaging.queue_template(s, s.get(Conversation, conv.id), "order_update", "id", ["WA-1001"], author="system")
    dispatch_due()
    sent = [m for m in MockWhatsAppSender().sent if m["pnid"] == pnid]
    assert len(sent) == 1 and sent[0]["template"] == "order_update" and sent[0]["params"] == ["WA-1001"]


def test_15_chatwoot_echo_is_suppressed_and_human_reply_sent_exactly_once(tenant, client):
    w = build_world(tenant)
    pnid = _pnid(w)
    inbox = _inbox(tenant)
    post_meta(client, wa_message(pnid, bsuid="ID.echo", text_body="ada stok?"))
    dispatch_due()
    owner = principal(tenant, "owner")
    conv = _conv(w, "ID.echo")
    with tenant_session(tenant.org_id, owner.user_id) as s:  # AI/OMS reply: sent to Meta and mirrored to Chatwoot
        messaging.queue_outbound(s, s.get(Conversation, conv.id), "Ada kak", author="ai")
    dispatch_due()
    mirrored = MockChatwoot().messages
    assert len(mirrored) == 2 and all(m["source_id"].startswith("oms:") for m in mirrored)

    # Chatwoot echoes every API-created message back to the webhook: all are dropped.
    for m in mirrored:
        assert post_chatwoot(client, MockChatwoot.echo_payload(m, inbox)).json() == {"ignored": "echo"}

    # A human agent replies in Chatwoot (no source_id, sender type user, outgoing, public).
    conv_cw = _conv(w, "ID.echo").chatwoot_conversation_id
    human = {"event": "message_created", "id": 777001, "content": "Saya bantu ya kak", "message_type": "outgoing",
             "private": False, "source_id": None, "sender": {"type": "user"}, "conversation": {"id": conv_cw},
             "inbox": {"id": inbox}}
    assert "queued" in post_chatwoot(client, human).json()
    assert post_chatwoot(client, human).json() == {"ignored": "duplicate"}  # Chatwoot re-delivery
    private = {**human, "id": 777002, "private": True}
    assert post_chatwoot(client, private).json() == {"ignored": "private_note"}
    dispatch_due()
    dispatch_due()

    sent = [m for m in MockWhatsAppSender().sent if m["pnid"] == pnid]
    assert [m["body"] for m in sent] == ["Ada kak", "Saya bantu ya kak"]  # each exactly once, no loop
    assert len(MockChatwoot().messages) == 2  # the human reply is not mirrored back into Chatwoot
    with owner_session() as s:
        assert s.get(Conversation, conv.id).ai_enabled is False  # human took over
        human_msg = s.scalars(select(Message).where(Message.conversation_id == conv.id, Message.author == "human")).one()
        assert human_msg.origin == "chatwoot" and human_msg.status == "sent"


def test_whatsapp_send_timeout_after_accept_becomes_uncertain_not_resent(tenant, client, monkeypatch):
    w = build_world(tenant)
    pnid = _pnid(w)
    post_meta(client, wa_message(pnid, bsuid="ID.unc"))
    dispatch_due()
    conv = _conv(w, "ID.unc")
    with tenant_session(tenant.org_id, principal(tenant, "owner").user_id) as s:
        mid = messaging.queue_outbound(s, s.get(Conversation, conv.id), "Total Rp100.000", author="ai").id
    monkeypatch.setenv("OMS_MOCK_FAULT", "whatsapp.send_message:timeout_after_accept")
    dispatch_due()
    monkeypatch.delenv("OMS_MOCK_FAULT")
    dispatch_due(now=utcnow() + timedelta(hours=2))
    assert len([m for m in MockWhatsAppSender().sent if m["pnid"] == pnid]) == 1
    with owner_session() as s:
        assert s.get(Message, mid).status == "uncertain"
        op = s.scalars(select(ExternalOperation).where(ExternalOperation.idempotency_key == f"msg:{mid}")).one()
        assert op.status == "uncertain"
