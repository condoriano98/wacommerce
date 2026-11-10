"""OMS WhatsApp gateway — the single owner of the Meta WhatsApp connection (R1).

Inbound: verify signature on raw bytes -> resolve account by phone_number_id -> store raw -> resolve customer
(BSUID-first) -> dedupe message by wamid -> capture attribution (CTWA referral / landing ref code) -> consent
keywords -> LeadSubmitted -> mirror to Chatwoot (outbox).
Outbound: 24-hour service window enforced; free text outside the window is refused (templates only).
"""

import json
import logging
import re
import uuid
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import set_tenant, system_session
from app.core.outbox import enqueue, handler
from app.core.security import normalize_id_phone, verify_meta_signature
from app.integrations import factory
from app.integrations.whatsapp import InboundMessage, parse_webhook
from app.models.base import utcnow
from app.models.customers import Conversation, CustomerAttribution, InboundWebhook, LandingSession, Message
from app.models.governance import IntegrationError, OutboxMessage
from app.models.marketing import AdCampaign, AdGroup, Advertisement
from app.models.platforms import AccountPortfolioLink, PlatformAccount
from app.modules import conversions, customers

log = logging.getLogger(__name__)
REF_CODE = re.compile(r"\bkode[:\s]+([A-Z0-9]{6})\b", re.IGNORECASE)
CONSENT_YES = {"consent_yes", "setuju data"}
CONSENT_NO = {"consent_no", "tolak data", "stop data"}
NOTICE_VERSION = "pdp-2026-11-v1"
SERVICE_WINDOW = timedelta(hours=24)


def handle_whatsapp_webhook(raw_body: bytes, signature: str | None) -> dict:
    if not verify_meta_signature(get_settings().meta_app_secret, raw_body, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")
    payload = json.loads(raw_body)
    messages, statuses = parse_webhook(payload)
    result = {"messages": 0, "duplicates": 0, "statuses": 0, "unknown_numbers": 0}
    pnids = {m.phone_number_id for m in messages} | {s.phone_number_id for s in statuses}
    for pnid in pnids:
        with system_session() as session:
            account = session.scalars(select(PlatformAccount).where(
                PlatformAccount.provider == "whatsapp",
                PlatformAccount.public_config["phone_number_id"].astext == pnid)).first()
            if account is None:
                result["unknown_numbers"] += 1
                log.warning("webhook for unknown phone_number_id %s", pnid)
                continue
            set_tenant(session, account.organization_id)
            session.add(InboundWebhook(organization_id=account.organization_id, platform_account_id=account.id,
                                       provider="whatsapp", raw_body=raw_body.decode(), processed_at=utcnow()))
            for m in (m for m in messages if m.phone_number_id == pnid):
                if process_inbound(session, account, m):
                    result["messages"] += 1
                else:
                    result["duplicates"] += 1
            for st in (s for s in statuses if s.phone_number_id == pnid):
                msg = session.scalars(select(Message).where(Message.platform_account_id == account.id,
                                                            Message.external_id == st.wamid)).first()
                if msg:
                    msg.status = st.status
                    result["statuses"] += 1
    return result


def process_inbound(session: Session, account: PlatformAccount, m: InboundMessage) -> bool:
    org = account.organization_id
    portfolio_ids = list(session.scalars(select(AccountPortfolioLink.portfolio_id).where(
        AccountPortfolioLink.platform_account_id == account.id)))
    identities = {"bsuid": m.bsuid, "phone_e164": normalize_id_phone(m.wa_id) if m.wa_id else None}
    customer = customers.resolve_customer(session, org, account.brand_id, identities, portfolio_ids, m.profile_name)
    conv = session.scalars(select(Conversation).where(Conversation.platform_account_id == account.id,
                                                      Conversation.customer_id == customer.id)).first()
    if conv is None:
        conv = Conversation(organization_id=org, platform_account_id=account.id, customer_id=customer.id)
        session.add(conv)
        session.flush()
    inserted = session.execute(insert(Message).values(
        id=uuid.uuid4(), created_at=utcnow(), organization_id=org, platform_account_id=account.id,
        conversation_id=conv.id, customer_id=customer.id, direction="inbound", author="customer",
        external_id=m.wamid, body=m.text, payload=m.raw, status="received", sent_at=m.timestamp,
    ).on_conflict_do_nothing(index_elements=["platform_account_id", "external_id"]).returning(Message.id)).first()
    if inserted is None:
        return False  # duplicate delivery by Meta
    message_id = inserted[0]
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


def queue_outbound(session: Session, conv: Conversation, body: str, *, author: str,
                   mirror_to_chatwoot: bool = True) -> Message:
    """Queue a free-text reply. Refused outside the 24h customer service window (templates required)."""
    if conv.last_inbound_at is None or utcnow() - conv.last_inbound_at > SERVICE_WINDOW:
        raise HTTPException(status.HTTP_409_CONFLICT, "outside 24h service window: use an approved template")
    msg = Message(organization_id=conv.organization_id, platform_account_id=conv.platform_account_id,
                  conversation_id=conv.id, customer_id=conv.customer_id, direction="outbound", author=author,
                  body=body, status="queued")
    session.add(msg)
    session.flush()
    enqueue(session, conv.organization_id, "whatsapp.send", f"send:{msg.id}",
            {"message_id": str(msg.id), "mirror": mirror_to_chatwoot})
    return msg


@handler("whatsapp.send")
def send_job(session: Session, outbox: OutboxMessage) -> None:
    from app.models.customers import CustomerIdentity

    msg = session.get(Message, uuid.UUID(outbox.payload["message_id"]))
    if msg.external_id:  # already sent (idempotent retry)
        return
    account = session.get(PlatformAccount, msg.platform_account_id)
    idents = dict(session.execute(select(CustomerIdentity.identity_type, CustomerIdentity.identity_value).where(
        CustomerIdentity.customer_id == msg.customer_id)).all())
    to = {"phone": idents.get("phone_e164"), "bsuid": idents.get("bsuid")}
    msg.external_id = factory.whatsapp_sender(account).send_text(account.public_config["phone_number_id"], to,
                                                                 msg.body or "")
    msg.status, msg.sent_at = "sent", utcnow()
    if outbox.payload.get("mirror"):
        enqueue(session, msg.organization_id, "chatwoot.mirror", f"cw:{msg.id}", {"message_id": str(msg.id)})


def _chatwoot_account(session: Session, org_id) -> PlatformAccount | None:
    return session.scalars(select(PlatformAccount).where(PlatformAccount.provider == "chatwoot",
                                                         PlatformAccount.organization_id == org_id)).first()


@handler("chatwoot.mirror")
def mirror_job(session: Session, outbox: OutboxMessage) -> None:
    msg = session.get(Message, uuid.UUID(outbox.payload["message_id"]))
    conv = session.get(Conversation, msg.conversation_id)
    client = factory.chatwoot_client(_chatwoot_account(session, msg.organization_id))
    if conv.chatwoot_conversation_id is None:
        conv.chatwoot_conversation_id = client.ensure_conversation("whatsapp", str(conv.customer_id), None)
    client.post_message(conv.chatwoot_conversation_id, msg.body or f"[{msg.payload.get('type', 'media')}]",
                        "incoming" if msg.direction == "inbound" else "outgoing")


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
