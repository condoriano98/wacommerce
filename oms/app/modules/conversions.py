"""Conversion event outbox (docs/05-meta-attribution.md).

Events are written in the same transaction as the business change, keyed by a unique business key
(subject, event, destination, occurrence) so retries and replays can never create a second Purchase.
Eligibility (supported event, consent, identifiers, age) is re-evaluated by the sender at send time.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.outbox import enqueue, handler
from app.models.base import utcnow
from app.models.marketing import ConversionOutbox
from app.models.orders import Order
from app.modules import events

META_BM = "meta_business_messaging"
BM_SUPPORTED = {
    "Purchase", "LeadSubmitted", "InitiateCheckout", "AddToCart", "ViewContent", "OrderCreated", "OrderShipped",
    "OrderDelivered", "OrderCanceled", "OrderReturned", "CartAbandoned", "QualifiedLead", "RatingProvided",
    "ReviewProvided",
}


def enqueue_event(session: Session, *, org_id: uuid.UUID, subject_type: str, subject_id: uuid.UUID,
                  customer_id: uuid.UUID, event_name: str, event_time: datetime, destination: str = META_BM,
                  order_id: uuid.UUID | None = None, value: Decimal | None = None, occurrence_key: str = "",
                  dataset_account_id: uuid.UUID | None = None) -> bool:
    """Returns True if a new event was queued, False if this business event was already recorded."""
    cid = uuid.uuid4()
    res = session.execute(insert(ConversionOutbox).values(
        id=cid, created_at=utcnow(), organization_id=org_id, event_id=uuid.uuid4(),
        subject_type=subject_type, subject_id=subject_id, customer_id=customer_id, order_id=order_id,
        event_name=event_name, destination=destination, dataset_account_id=dataset_account_id,
        occurrence_key=occurrence_key, event_time=event_time, value=value, currency="IDR", status="pending",
        attempts=0, next_attempt_at=utcnow(),
    ).on_conflict_do_nothing(
        index_elements=["subject_type", "subject_id", "event_name", "destination", "occurrence_key"])
        .returning(ConversionOutbox.id)).scalar()
    if res is None:  # this business event was already recorded
        return False
    if destination == META_BM:
        enqueue(session, org_id, "capi.send", f"capi:{cid}", {"conversion_id": str(cid)})
    return True


def _order_event(session: Session, order: Order, event_name: str, *, value: bool = False, occurrence: str = "") -> None:
    if order.sales_channel != "whatsapp" or order.customer_id is None:
        return  # marketplace-native orders are measured by the platform, not Meta BM CAPI
    enqueue_event(session, org_id=order.organization_id, subject_type="order", subject_id=order.id,
                  customer_id=order.customer_id, order_id=order.id, event_name=event_name, event_time=utcnow(),
                  value=order.total if value else None, occurrence_key=occurrence,
                  dataset_account_id=order.platform_account_id)


@events.subscribe("order_confirmed")
def _order_created(session, data):
    _order_event(session, data["order"], "OrderCreated")


@events.subscribe("payment_captured")
def _prepaid_purchase_gateway(session, data):
    _order_event(session, data["order"], "Purchase", value=True)


@events.subscribe("payment_bank_matched")
def _prepaid_purchase_bank(session, data):
    # Prepaid purchases count only once money is verified (captured or bank-matched), never on a receipt.
    _order_event(session, data["order"], "Purchase", value=True)


@events.subscribe("shipment_picked")
def _shipped(session, data):
    _order_event(session, data["order"], "OrderShipped", occurrence=str(data["shipment"].id))


@events.subscribe("shipment_delivered")
def _delivered(session, data):
    order = data["order"]
    _order_event(session, order, "OrderDelivered", occurrence=str(data["shipment"].id))
    if order.payment_method == "cod":  # COD: delivered = collected by the courier; revenue recognised now too
        _order_event(session, order, "Purchase", value=True)


@events.subscribe("items_canceled")
def _canceled(session, data):
    if data["order"].status == "canceled":
        _order_event(session, data["order"], "OrderCanceled")


@events.subscribe("order_returned")
def _returned(session, data):
    _order_event(session, data["order"], "OrderReturned")


def suppress_pending_for_customer(session: Session, customer_id: uuid.UUID, reason: str) -> int:
    rows = session.scalars(select(ConversionOutbox).where(ConversionOutbox.customer_id == customer_id,
                                                          ConversionOutbox.status.in_(("pending", "failed")))).all()
    for r in rows:
        r.status, r.status_reason = "suppressed", reason
    return len(rows)


MAX_EVENT_AGE = 7  # days (Business Messaging CAPI)


def _identifiers(session: Session, c: ConversionOutbox) -> dict:
    """Hashed identifiers only; ctwa_clid as provided by Meta. Never fabricated."""
    from app.core.security import sha256_normalized
    from app.models.customers import CustomerAttribution, CustomerIdentity

    ids: dict = {}
    clid = session.scalars(select(CustomerAttribution.ctwa_clid).where(
        CustomerAttribution.customer_id == c.customer_id, CustomerAttribution.ctwa_clid.is_not(None))
        .order_by(CustomerAttribution.touched_at.desc())).first()
    if clid:
        ids["ctwa_clid"] = clid
    phone = session.scalars(select(CustomerIdentity.identity_value).where(
        CustomerIdentity.customer_id == c.customer_id, CustomerIdentity.identity_type == "phone_e164")).first()
    if phone:
        ids["ph"] = [sha256_normalized(phone.lstrip("+"))]
    return ids


def eligibility(session: Session, c: ConversionOutbox, now=None) -> tuple[str, str | None, dict]:
    """Send-time eligibility: supported event ∧ consent ∧ identifiers ∧ age ≤ 7 days."""
    from datetime import timedelta

    from app.modules import customers

    if c.event_name not in BM_SUPPORTED:
        return "suppressed", "unsupported_event", {}
    if (now or utcnow()) - c.event_time > timedelta(days=MAX_EVENT_AGE):
        return "expired", "older_than_7_days", {}
    consent = customers.consent_status(session, c.customer_id, "meta_ad_measurement")
    if consent != "granted":
        return "suppressed", f"consent_{consent}", {}
    ids = _identifiers(session, c)
    if not ids:
        return "suppressed", "no_identifiers", {}
    return "eligible", None, ids


@handler("capi.send")
def send_conversion(session: Session, msg) -> None:
    from app.core import external_ops
    from app.integrations.meta_capi import MockMetaCapi
    from app.models.marketing import ConversionDeliveryLog
    from app.models.platforms import PlatformAccount

    c = session.get(ConversionOutbox, uuid.UUID(msg.payload["conversion_id"]), with_for_update=True)
    if c is None or c.status != "pending":
        return
    state, reason, ids = eligibility(session, c)
    if state != "eligible":
        c.status, c.status_reason = state, reason
        return
    account = session.get(PlatformAccount, c.dataset_account_id) if c.dataset_account_id else None
    dataset = (account.public_config.get("capi_dataset_id") if account else None) or "unconfigured"
    event = {"event_name": c.event_name, "event_time": int(c.event_time.timestamp()), "event_id": str(c.event_id),
             "action_source": "business_messaging", "messaging_channel": "whatsapp", "user_data": ids}
    if c.value is not None:
        event["custom_data"] = {"value": str(c.value), "currency": c.currency}
    c.attempts += 1
    try:
        resp = external_ops.execute(c.organization_id, "meta_capi", "send_event", str(c.event_id),
                                    lambda: MockMetaCapi().send_event(dataset, event),
                                    subject_type="conversion", subject_id=str(c.id),
                                    summary={"event_name": c.event_name, "event_id": str(c.event_id)})
    except external_ops.OperationUncertain:
        c.status, c.status_reason = "failed", "uncertain_not_resent"  # Meta does not dedupe BM events
        return
    except external_ops.Rejected as exc:
        c.status, c.status_reason, c.last_error = "failed", "rejected", str(exc)[:500]
        return
    c.status, c.sent_at, c.external_ack = "sent", utcnow(), resp
    session.add(ConversionDeliveryLog(organization_id=c.organization_id, conversion_id=c.id,
                                      customer_id=c.customer_id, attempt=c.attempts,
                                      request={"event_name": c.event_name, "user_data_keys": sorted(ids)},
                                      response=resp, http_status=200))
