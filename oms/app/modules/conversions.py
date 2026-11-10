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
    res = session.execute(insert(ConversionOutbox).values(
        id=uuid.uuid4(), created_at=utcnow(), organization_id=org_id, event_id=uuid.uuid4(),
        subject_type=subject_type, subject_id=subject_id, customer_id=customer_id, order_id=order_id,
        event_name=event_name, destination=destination, dataset_account_id=dataset_account_id,
        occurrence_key=occurrence_key, event_time=event_time, value=value, currency="IDR", status="pending",
        attempts=0, next_attempt_at=utcnow(),
    ).on_conflict_do_nothing(
        index_elements=["subject_type", "subject_id", "event_name", "destination", "occurrence_key"]))
    return res.rowcount == 1


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
