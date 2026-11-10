"""DOKU and Biteship callbacks (spec §16, §12 pattern): verify what can be verified, persist, ack, then let the worker
re-check the provider before changing anything. Callbacks are *triggers*, never the source of truth:

* DOKU: signature verified (documentation-verified scheme); the worker re-checks status by invoice (G-DOKU) before
  ``record_gateway_notification``. Duplicate notifications dedupe on (provider, notification id); FAILED after SUCCESS
  cannot undo a capture.
* Biteship: webhook authentication is unverified (G-BS2), so the payload is used only to find the shipment; the worker
  re-fetches the order and applies the provider's *current* status. Duplicates and out-of-order callbacks therefore
  converge on the same state (shipment events never move backwards).
"""

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import act_as_system, webhook_session
from app.core.outbox import enqueue, handler
from app.models.base import utcnow
from app.models.customers import InboundWebhook
from app.models.fulfillment import Shipment
from app.models.governance import OutboxMessage
from app.models.orders import Payment

DOKU_STATUS = {"SUCCESS": "paid", "FAILED": "failed", "EXPIRED": "expired"}


def receive_doku(raw: bytes, headers: dict) -> dict:
    from app.integrations.doku import verify_notification

    s = get_settings()
    h = {k.lower(): v for k, v in headers.items()}
    if not verify_notification(h, raw, s.doku_notification_secret, s.doku_notification_path):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")
    try:
        sent_at = datetime.fromisoformat(h.get("request-timestamp", "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid request timestamp") from exc
    if abs((utcnow() - sent_at).total_seconds()) > s.webhook_max_age_seconds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "stale webhook (replay window exceeded)")
    body = json.loads(raw)
    invoice = (body.get("order") or {}).get("invoice_number")
    with webhook_session() as session:
        row = session.execute(text("select * from resolve_payment('doku', :r)"), {"r": invoice}).first()
        if row is None or row.organization_id is None:
            return {"ignored": "unknown_invoice"}
        act_as_system(session, row.organization_id)
        session.add(InboundWebhook(organization_id=row.organization_id, provider="doku", raw_body=raw.decode(),
                                   headers={"request-id": h.get("request-id")}))
        enqueue(session, row.organization_id, "doku.verify", f"doku-n:{h.get('request-id')}",
                {"payment_id": str(row.payment_id), "notification_id": h.get("request-id")})
    return {"accepted": True}


@handler("doku.verify")
def verify_doku_job(session: Session, msg: OutboxMessage) -> None:
    from app.integrations import factory
    from app.modules import payments

    p = session.get(Payment, uuid.UUID(msg.payload["payment_id"]))
    current = factory.payment_provider(None).get_status(p.provider_ref)  # status re-check (read-only)
    if current is None:
        return
    verified = DOKU_STATUS.get(str(current.get("status")).upper(), "pending")
    if verified == "pending":
        return
    payments.record_gateway_notification(session, provider="doku", notification_id=msg.payload["notification_id"],
                                         provider_ref=p.provider_ref, verified_status=verified,
                                         verified_amount=Decimal(str(current.get("amount"))), raw=current)


def receive_biteship(raw: bytes) -> dict:
    body = json.loads(raw)
    order_id = body.get("order_id") or body.get("id")
    if not order_id:
        return {"ignored": "no_order_id"}
    with webhook_session() as session:
        row = session.execute(text("select * from resolve_shipment(:e)"), {"e": str(order_id)}).first()
        if row is None or row.organization_id is None:
            return {"ignored": "unknown_shipment"}
        act_as_system(session, row.organization_id)
        session.add(InboundWebhook(organization_id=row.organization_id, provider="biteship", raw_body=raw.decode()))
        enqueue(session, row.organization_id, "biteship.sync", f"bs-n:{hashlib.sha256(raw).hexdigest()}",
                {"shipment_id": str(row.shipment_id)})
    return {"accepted": True}


@handler("biteship.sync")
def sync_biteship_job(session: Session, msg: OutboxMessage) -> None:
    from app.integrations import factory
    from app.modules import fulfillment

    sh = session.get(Shipment, uuid.UUID(msg.payload["shipment_id"]), with_for_update=True)
    current = factory.carrier(None).get_order(sh.external_id)
    if current is None or current.get("status") in (None, "confirmed", "allocated"):
        return
    occurred = datetime.fromisoformat(current["updated_at"]) if current.get("updated_at") else utcnow()
    fulfillment.apply_shipment_event(session, sh, current["status"], occurred, {"source": "biteship_refetch"})
