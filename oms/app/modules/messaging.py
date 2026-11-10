"""OMS WhatsApp gateway — the single owner of the Meta WhatsApp connection (R1).

Inbound: verify signature on raw bytes -> resolve account by phone_number_id -> store raw -> resolve customer
(BSUID-first) -> dedupe message by wamid -> capture attribution (CTWA referral / landing ref code) -> consent
keywords -> LeadSubmitted -> mirror to Chatwoot (outbox).
Outbound: 24-hour service window enforced; free text outside the window is refused (templates only).
"""

import hashlib
import json
import logging
import re
import uuid
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import act_as_system, set_tenant, system_session, webhook_session
from app.core.outbox import enqueue, handler
from app.core.security import normalize_id_phone, verify_meta_signature
from app.integrations import factory
from app.integrations.whatsapp import InboundMessage, parse_webhook
from app.models.base import utcnow
from app.models.customers import Conversation, CustomerAttribution, LandingSession, Message
from app.models.governance import IntegrationError, OutboxMessage
from app.models.marketing import AdCampaign, AdGroup, Advertisement
from app.models.messaging import MessageMapping, MessageTemplate, WaEvent
from app.models.platforms import AccountPortfolioLink, PlatformAccount
from app.modules import conversions, customers

log = logging.getLogger(__name__)
REF_CODE = re.compile(r"\bkode[:\s]+([A-Z0-9]{6})\b", re.IGNORECASE)
CONSENT_YES = {"consent_yes", "setuju data"}
CONSENT_NO = {"consent_no", "tolak data", "stop data"}
NOTICE_VERSION = "pdp-2026-11-v1"
SERVICE_WINDOW = timedelta(hours=24)


STATUS_RANK = {"queued": 0, "uncertain": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}


def receive_whatsapp(raw_body: bytes, signature: str | None) -> dict:
    """Meta webhook receiver (persist-then-ack). Verify the signature over the raw bytes, resolve each WhatsApp number to
    its tenant with the definer resolver, store the delivery in ``wa_events`` and queue ``wa.process`` in the same
    commit, then let the caller return 200. No business processing happens before the ack."""
    if not verify_meta_signature(get_settings().meta_app_secret, raw_body, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")
    payload = json.loads(raw_body)
    pnids = {c.get("value", {}).get("metadata", {}).get("phone_number_id")
             for e in payload.get("entry", []) for c in e.get("changes", [])} - {None}
    result = {"stored": 0, "duplicates": 0, "unknown_numbers": 0}
    for pnid in sorted(pnids):
        with webhook_session() as session:
            row = session.execute(text("select * from resolve_platform_account('whatsapp', 'phone_number_id', :p)"),
                                  {"p": pnid}).first()
            if row is None or row.organization_id is None:
                result["unknown_numbers"] += 1
                log.warning("webhook for unknown phone_number_id")
                continue
            act_as_system(session, row.organization_id)
            key = hashlib.sha256(pnid.encode() + b"|" + raw_body).hexdigest()
            eid = session.execute(insert(WaEvent).values(
                id=uuid.uuid4(), created_at=utcnow(), organization_id=row.organization_id,
                platform_account_id=row.platform_account_id, dedupe_key=key, raw=payload, received_at=utcnow(),
                attempts=0).on_conflict_do_nothing(index_elements=["organization_id", "dedupe_key"])
                .returning(WaEvent.id)).scalar()
            if eid is None:
                result["duplicates"] += 1
                continue
            enqueue(session, row.organization_id, "wa.process", f"wa:{eid}",
                    {"wa_event_id": str(eid), "phone_number_id": pnid})
            result["stored"] += 1
    return result


@handler("wa.process")
def process_wa_event(session: Session, outbox: OutboxMessage) -> None:
    """Idempotent: messages dedupe on (account, wamid); statuses only move forward."""
    ev = session.get(WaEvent, uuid.UUID(outbox.payload["wa_event_id"]), with_for_update=True)
    account = session.get(PlatformAccount, ev.platform_account_id)
    pnid = outbox.payload["phone_number_id"]
    ev.attempts += 1
    messages, statuses = parse_webhook(ev.raw)
    for m in sorted((m for m in messages if m.phone_number_id == pnid), key=lambda m: m.timestamp):
        process_inbound(session, account, m)
    for st in (st for st in statuses if st.phone_number_id == pnid):
        apply_status(session, account, st.wamid, st.status)
    ev.processed_at, ev.last_error = utcnow(), None


def apply_status(session: Session, account: PlatformAccount, wamid: str, new_status: str) -> bool:
    msg = session.scalars(select(Message).where(Message.platform_account_id == account.id,
                                                Message.external_id == wamid)).first()
    if msg is None:
        return False  # status for a message we never recorded (e.g. sent outside OMS): ignored
    if STATUS_RANK.get(new_status, -1) <= STATUS_RANK.get(msg.status, -1):
        return False  # out-of-order or duplicate status never moves a message backwards
    msg.status = new_status
    return True


def replay_wa_event(session: Session, principal, event_id: uuid.UUID) -> None:
    principal.require("conversations.manage")
    ev = session.get(WaEvent, event_id)
    if ev is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")
    pnids = {c.get("value", {}).get("metadata", {}).get("phone_number_id")
             for e in ev.raw.get("entry", []) for c in e.get("changes", [])} - {None}
    account = session.get(PlatformAccount, ev.platform_account_id)
    pnid = account.public_config.get("phone_number_id")
    if pnid not in pnids:
        raise HTTPException(status.HTTP_409_CONFLICT, "event does not belong to this account")
    enqueue(session, ev.organization_id, "wa.process", f"wa:{ev.id}:replay:{utcnow():%Y%m%d%H%M%S%f}",
            {"wa_event_id": str(ev.id), "phone_number_id": pnid})


def _identity_domain(session: Session, brand_id) -> uuid.UUID | None:
    from app.models.customers import IdentityDomain

    return session.scalars(select(IdentityDomain.id).where(IdentityDomain.brand_id == brand_id,
                                                           IdentityDomain.kind == "meta_business")).first()


def _mapping(session: Session, msg: Message, origin: str) -> MessageMapping:
    mp = session.scalars(select(MessageMapping).where(MessageMapping.message_id == msg.id)).first()
    if mp is None:
        mp = MessageMapping(organization_id=msg.organization_id, message_id=msg.id,
                            platform_account_id=msg.platform_account_id, origin=origin)
        session.add(mp)
        session.flush()
    return mp


def process_inbound(session: Session, account: PlatformAccount, m: InboundMessage) -> bool:
    org = account.organization_id
    portfolio_ids = list(session.scalars(select(AccountPortfolioLink.portfolio_id).where(
        AccountPortfolioLink.platform_account_id == account.id)))
    identities = {"bsuid": m.bsuid, "phone_e164": normalize_id_phone(m.wa_id) if m.wa_id else None}
    customer = customers.resolve_customer(session, org, account.brand_id, identities, portfolio_ids, m.profile_name,
                                          identity_domain_id=_identity_domain(session, account.brand_id),
                                          provider="whatsapp")
    conv = session.scalars(select(Conversation).where(Conversation.platform_account_id == account.id,
                                                      Conversation.customer_id == customer.id)).first()
    if conv is None:
        conv = Conversation(organization_id=org, platform_account_id=account.id, customer_id=customer.id)
        session.add(conv)
        session.flush()
    inserted = session.execute(insert(Message).values(
        id=uuid.uuid4(), created_at=utcnow(), organization_id=org, platform_account_id=account.id,
        conversation_id=conv.id, customer_id=customer.id, direction="inbound", author="customer",
        external_id=m.wamid, body=m.text, payload=m.raw, status="received", sent_at=m.timestamp, origin="meta",
    ).on_conflict_do_nothing(index_elements=["platform_account_id", "external_id"]).returning(Message.id)).first()
    if inserted is None:
        return False  # duplicate delivery by Meta
    message_id = inserted[0]
    session.add(MessageMapping(organization_id=org, message_id=message_id, platform_account_id=account.id,
                               origin="meta", wamid=m.wamid))
    first_contact = conv.last_inbound_at is None
    conv.last_inbound_at = max(conv.last_inbound_at or m.timestamp, m.timestamp)

    if m.referral:
        capture_referral(session, account, customer.id, conv.id, m)
    elif m.text and (code := REF_CODE.search(m.text)):
        link_landing_session(session, account, customer.id, conv.id, m, code.group(1).upper())

    key = (m.interactive_reply_id or (m.text or "")).strip().lower()
    if key in CONSENT_YES:
        customers.record_consent(session, org, customer.id, "meta_ad_measurement", "granted",
                                 notice_version=NOTICE_VERSION, source="whatsapp_reply", source_message_id=m.wamid)
    elif key in CONSENT_NO:
        customers.record_consent(session, org, customer.id, "meta_ad_measurement",
                                 "withdrawn" if key == "stop data" else "denied", notice_version=NOTICE_VERSION,
                                 source="whatsapp_reply", source_message_id=m.wamid)
        conversions.suppress_pending_for_customer(session, customer.id, "consent withdrawn")

    if first_contact:
        conversions.enqueue_event(session, org_id=org, subject_type="customer", subject_id=customer.id,
                                  customer_id=customer.id, event_name="LeadSubmitted", event_time=m.timestamp,
                                  occurrence_key=str(account.id), dataset_account_id=account.id)
    enqueue(session, org, "chatwoot.mirror", f"cw:{message_id}", {"message_id": str(message_id)})
    return True


def capture_referral(session: Session, account: PlatformAccount, customer_id, conversation_id,
                     m: InboundMessage) -> None:
    ref = m.referral or {}
    ad_id = ref.get("source_id") if ref.get("source_type") == "ad" else None
    campaign_id = adset_id = ad_account_id = None
    resolution = "unresolved"
    if ad_id:
        row = session.execute(
            select(Advertisement.platform_account_id, AdGroup.external_id, AdCampaign.external_id)
            .join(AdGroup, AdGroup.id == Advertisement.ad_group_id)
            .join(AdCampaign, AdCampaign.id == AdGroup.campaign_id)
            .where(Advertisement.external_id == ad_id,
                   Advertisement.organization_id == account.organization_id)).first()
        if row:  # only ads in ad accounts owned by this organization resolve
            ad_account_id, adset_id, campaign_id = row
            resolution = "resolved"
    session.execute(insert(CustomerAttribution).values(
        id=uuid.uuid4(), created_at=utcnow(), organization_id=account.organization_id, customer_id=customer_id,
        conversation_id=conversation_id, source_message_id=m.wamid,
        attribution_method="ctwa_referral" if ad_id else "unattributed", provider="meta" if ad_id else None,
        ad_account_id=ad_account_id, campaign_id=campaign_id, adset_id=adset_id, ad_id=ad_id,
        resolution_status=resolution, source_type=ref.get("source_type"), source_url=ref.get("source_url"),
        ctwa_clid=ref.get("ctwa_clid"), raw_referral=ref, touched_at=m.timestamp,
    ).on_conflict_do_nothing(index_elements=["organization_id", "source_message_id"]))


def link_landing_session(session: Session, account, customer_id, conversation_id, m: InboundMessage,
                         code: str) -> None:
    ls = session.scalars(select(LandingSession).where(
        LandingSession.ref_code == code, LandingSession.organization_id == account.organization_id)).first()
    if ls is None:
        return  # unknown code: stays unattributed, never fabricated
    session.execute(insert(CustomerAttribution).values(
        id=uuid.uuid4(), created_at=utcnow(), organization_id=account.organization_id, customer_id=customer_id,
        conversation_id=conversation_id, source_message_id=m.wamid, attribution_method="landing_ref_code",
        provider="meta" if (ls.fbc or ls.fbp) else ("google" if ls.gclid else None), resolution_status="resolved",
        landing_session_id=ls.id, fbp=ls.fbp, fbc=ls.fbc, gclid=ls.gclid, touched_at=m.timestamp,
        campaign_id=ls.utm.get("utm_campaign"),
    ).on_conflict_do_nothing(index_elements=["organization_id", "source_message_id"]))


def in_service_window(conv: Conversation) -> bool:
    return conv.last_inbound_at is not None and utcnow() - conv.last_inbound_at <= SERVICE_WINDOW


def queue_outbound(session: Session, conv: Conversation, body: str, *, author: str, origin: str = "oms",
                   mirror_to_chatwoot: bool = True) -> Message:
    """Queue a free-text reply. Refused outside the 24h customer service window (templates required)."""
    if not in_service_window(conv):
        raise HTTPException(status.HTTP_409_CONFLICT, "outside 24h service window: use an approved template")
    return _queue(session, conv, body, author=author, origin=origin, mirror=mirror_to_chatwoot, payload={})


def queue_template(session: Session, conv: Conversation, name: str, language: str, params: list[str], *,
                   author: str) -> Message:
    """Templates may be sent outside the window, but only if registered and approved for this WhatsApp account."""
    tpl = session.scalars(select(MessageTemplate).where(
        MessageTemplate.platform_account_id == conv.platform_account_id, MessageTemplate.name == name,
        MessageTemplate.language == language)).first()
    if tpl is None or tpl.status != "approved":
        raise HTTPException(status.HTTP_409_CONFLICT, f"template {name}/{language} is not approved")
    if len(params) != tpl.param_count:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "template parameter count mismatch")
    return _queue(session, conv, f"[template:{name}]", author=author, origin="oms", mirror=True,
                  payload={"template": name, "language": language, "params": params})


def _queue(session: Session, conv: Conversation, body: str, *, author: str, origin: str, mirror: bool,
           payload: dict) -> Message:
    msg = Message(organization_id=conv.organization_id, platform_account_id=conv.platform_account_id,
                  conversation_id=conv.id, customer_id=conv.customer_id, direction="outbound", author=author,
                  body=body, status="queued", origin=origin, payload=payload)
    session.add(msg)
    session.flush()
    _mapping(session, msg, origin)
    enqueue(session, conv.organization_id, "whatsapp.send", f"send:{msg.id}",
            {"message_id": str(msg.id), "mirror": mirror})
    return msg


@handler("whatsapp.send")
def send_job(session: Session, outbox: OutboxMessage) -> None:
    """ADR-015 policy for Meta sends: no provider idempotency and no lookup → an unconfirmed send is never repeated
    automatically; it becomes ``uncertain`` (the status webhook or a human resolves it)."""
    from app.core import external_ops
    from app.models.customers import CustomerIdentity

    msg = session.get(Message, uuid.UUID(outbox.payload["message_id"]), with_for_update=True)
    if msg.external_id or msg.status == "uncertain":
        return
    account = session.get(PlatformAccount, msg.platform_account_id)
    idents = dict(session.execute(select(CustomerIdentity.identity_type, CustomerIdentity.identity_value).where(
        CustomerIdentity.customer_id == msg.customer_id)).all())
    to = {"phone": idents.get("phone_e164"), "bsuid": idents.get("bsuid")}
    sender = factory.whatsapp_sender(account)
    pnid = account.public_config["phone_number_id"]
    tpl = msg.payload.get("template")

    def call() -> dict:
        if tpl:
            return {"ref": sender.send_template(pnid, to, tpl, msg.payload["language"], msg.payload["params"])}
        return {"ref": sender.send_text(pnid, to, msg.body or "")}

    try:
        resp = external_ops.execute(msg.organization_id, "whatsapp", "send_message", f"msg:{msg.id}", call,
                                    platform_account_id=account.id, subject_type="message", subject_id=str(msg.id),
                                    summary={"message_id": str(msg.id), "template": tpl})
    except external_ops.OperationUncertain:
        msg.status = "uncertain"
        return
    msg.external_id = resp["ref"]
    msg.status, msg.sent_at = "sent", utcnow()
    _mapping(session, msg, msg.origin or "oms").wamid = resp["ref"]
    if outbox.payload.get("mirror"):
        enqueue(session, msg.organization_id, "chatwoot.mirror", f"cw:{msg.id}", {"message_id": str(msg.id)})


def _chatwoot_account(session: Session, org_id) -> PlatformAccount | None:
    return session.scalars(select(PlatformAccount).where(PlatformAccount.provider == "chatwoot",
                                                         PlatformAccount.organization_id == org_id)).first()


@handler("chatwoot.mirror")
def mirror_job(session: Session, outbox: OutboxMessage) -> None:
    """Mirror into the Chatwoot API inbox with ``source_id = oms:<message id>`` so the echo is recognised."""
    from app.core import external_ops

    msg = session.get(Message, uuid.UUID(outbox.payload["message_id"]))
    conv = session.get(Conversation, msg.conversation_id, with_for_update=True)
    client = factory.chatwoot_client(_chatwoot_account(session, msg.organization_id))
    if conv.chatwoot_conversation_id is None:
        conv.chatwoot_conversation_id = client.ensure_conversation("whatsapp", str(conv.customer_id), None)
    source_id = f"oms:{msg.id}"
    content = msg.body or f"[{msg.payload.get('type', 'media')}]"
    mtype = "incoming" if msg.direction == "inbound" else "outgoing"
    try:
        resp = external_ops.execute(
            msg.organization_id, "chatwoot", "post_message", source_id,
            lambda: {"ref": client.post_message(conv.chatwoot_conversation_id, content, mtype, source_id=source_id)},
            lookup=lambda: client.find_by_source_id(conv.chatwoot_conversation_id, source_id),
            subject_type="message", subject_id=str(msg.id))
    except external_ops.OperationUncertain:
        return
    mp = _mapping(session, msg, msg.origin or "oms")
    mp.source_id, mp.chatwoot_message_id = source_id, int(resp["ref"])


def receive_chatwoot(raw_body: bytes, timestamp: str | None, signature: str | None) -> dict:
    """Chatwoot API-channel callback. Only a human agent's public outgoing message becomes a WhatsApp send; every
    message the OMS itself posted (``source_id`` ``oms:…``) is an echo and is dropped (acceptance #15)."""
    from app.integrations.chatwoot import verify_chatwoot_signature

    if not verify_chatwoot_signature(get_settings().chatwoot_webhook_secret, raw_body, timestamp, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")
    p = json.loads(raw_body)
    reason = classify_chatwoot_event(p)
    if reason:
        return {"ignored": reason}
    with webhook_session() as session:
        row = session.execute(text("select * from resolve_platform_account('chatwoot', 'inbox_id', :i)"),
                              {"i": str(p["inbox"]["id"])}).first()
        if row is None or row.organization_id is None:
            return {"ignored": "unknown_inbox"}
        act_as_system(session, row.organization_id)
        if session.scalars(select(MessageMapping).where(MessageMapping.chatwoot_message_id == int(p["id"]))).first():
            return {"ignored": "duplicate"}
        conv = session.scalars(select(Conversation).where(
            Conversation.chatwoot_conversation_id == int(p["conversation"]["id"]))).first()
        if conv is None:
            return {"ignored": "unknown_conversation"}
        conv.ai_enabled = False  # a human agent has taken over this conversation
        msg = queue_outbound(session, conv, p.get("content") or "", author="human", origin="chatwoot",
                             mirror_to_chatwoot=False)
        mp = _mapping(session, msg, "chatwoot")
        mp.chatwoot_message_id = int(p["id"])
        return {"queued": str(msg.id)}


def classify_chatwoot_event(p: dict) -> str | None:
    """Return the reason to ignore a Chatwoot callback, or None when it is a human reply to send."""
    if p.get("event") != "message_created":
        return "not_message_created"
    if str(p.get("source_id") or "").startswith("oms:"):
        return "echo"
    if p.get("message_type") != "outgoing":
        return "not_outgoing"
    if p.get("private"):
        return "private_note"
    if (p.get("sender") or {}).get("type", "").lower() not in ("user", "agent"):
        return "not_human_agent"
    return None


def handoff(session: Session, conv: Conversation, reason: str) -> None:
    conv.ai_enabled = False
    conv.handoff_reason = reason
    enqueue(session, conv.organization_id, "chatwoot.handoff", f"handoff:{conv.id}:{utcnow():%Y%m%d%H%M%S%f}",
            {"conversation_id": str(conv.id), "reason": reason})


@handler("chatwoot.handoff")
def handoff_job(session: Session, outbox: OutboxMessage) -> None:
    conv = session.get(Conversation, uuid.UUID(outbox.payload["conversation_id"]))
    client = factory.chatwoot_client(_chatwoot_account(session, conv.organization_id))
    if conv.chatwoot_conversation_id is None:
        conv.chatwoot_conversation_id = client.ensure_conversation("whatsapp", str(conv.customer_id), None)
    client.open_for_human(conv.chatwoot_conversation_id, outbox.payload["reason"])


def check_webhook_ownership(fetch_config) -> int:
    """Read-only drift detection (never rewrites Meta config). fetch_config(account) -> callback URL or None."""
    alerts = 0
    with system_session() as session:
        for account in session.scalars(select(PlatformAccount).where(PlatformAccount.provider == "whatsapp")).all():
            expected = account.public_config.get("expected_callback_url")
            actual = fetch_config(account)
            ok = expected is None or actual == expected
            account.webhook_status = "ok" if ok else "drift"
            if not ok:
                alerts += 1
                set_tenant(session, account.organization_id)
                session.add(IntegrationError(organization_id=account.organization_id, platform_account_id=account.id,
                                             kind="webhook_drift", severity="critical",
                                             message="Meta callback differs from OMS gateway",
                                             detail={"expected": expected, "actual": actual}))
    return alerts
