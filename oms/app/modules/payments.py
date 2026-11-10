"""Payment links (DOKU), manual bank transfers and COD.

* Gateway notifications are triggers only: the adapter re-checks status with the provider before money facts
  are recorded. Notifications are idempotent (UNIQUE provider + notification_id).
* Manual transfers are confirmed by a human with finance/cs authority — never by the AI.
* Amount mismatches open a ``payment_mismatch`` case and do not release the order.
"""

import uuid
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.authz import Principal
from app.models.base import utcnow
from app.models.orders import Order, OrderItem, Payment, PaymentNotification
from app.modules import exceptions, orders
from app.modules.finance import posting


def create_payment_link(session: Session, order: Order, *, provider: str, provider_ref: str, url: str,
                        expires_at) -> Payment:
    if order.status != "payment_pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"order is {order.status}")
    existing = session.scalars(select(Payment).where(Payment.order_id == order.id, Payment.status == "pending",
                                                     Payment.provider == provider)).first()
    if existing:
        return existing
    p = Payment(organization_id=order.organization_id, order_id=order.id, method="doku_link", provider=provider,
                provider_ref=provider_ref, amount=order.total, payment_url=url, expires_at=expires_at)
    session.add(p)
    return p


def _portfolio(session: Session, order: Order) -> uuid.UUID | None:
    return session.scalars(select(OrderItem.portfolio_id).where(OrderItem.order_id == order.id)).first()


def apply_confirmed_payment(session: Session, payment: Payment, amount: Decimal, *, via_gateway: bool,
                            actor_type: str, actor_user_id: uuid.UUID | None = None) -> str:
    """Shared path once the money is confirmed. Returns 'paid' | 'mismatch' | 'duplicate'."""
    order = session.get(Order, payment.order_id, with_for_update=True)
    if payment.status == "paid":
        return "duplicate"
    if amount != payment.amount:
        exceptions.open_case(session, order.organization_id, "payment_mismatch", subject_type="payment",
                             subject_id=str(payment.id), order_id=order.id, portfolio_id=_portfolio(session, order),
                             context={"expected": str(payment.amount), "received": str(amount)})
        payment.evidence = {**payment.evidence, "received_amount": str(amount)}
        if order.status != "exception" and "exception" in orders.TRANSITIONS.get(order.status, set()):
            orders.transition(session, order, "exception", actor_type=actor_type, reason="payment amount mismatch")
        return "mismatch"
    payment.status, payment.paid_at = "paid", utcnow()
    order.payment_status = "paid"
    posting.payment_received(session, order, amount, via_gateway=via_gateway, source_id=f"payment:{payment.id}")
    if order.status == "payment_pending":
        orders.transition(session, order, "payment_verified", actor_type=actor_type, actor_user_id=actor_user_id,
                          reason=f"{payment.provider} payment confirmed")
    return "paid"


def record_gateway_notification(session: Session, *, provider: str, notification_id: str, provider_ref: str,
                                verified_status: str, verified_amount: Decimal, raw: dict) -> str:
    """Called after the adapter verified the signature AND re-checked the status via the provider API."""
    payment = session.scalars(select(Payment).where(Payment.provider == provider,
                                                    Payment.provider_ref == provider_ref)).first()
    stmt = insert(PaymentNotification).values(
        id=uuid.uuid4(), created_at=utcnow(), organization_id=payment.organization_id if payment else None,
        provider=provider, notification_id=notification_id, payment_id=payment.id if payment else None, raw=raw,
        verified=True).on_conflict_do_nothing(index_elements=["provider", "notification_id"])
    if payment is None:
        return "unknown_payment"
    if session.execute(stmt).rowcount == 0:
        return "duplicate"
    if verified_status != "paid":
        if verified_status in ("failed", "expired"):
            payment.status = verified_status
        return verified_status
    return apply_confirmed_payment(session, payment, verified_amount, via_gateway=True, actor_type="system")


def submit_manual_transfer(session: Session, order: Order, evidence: dict) -> Payment:
    """Customer sent a transfer receipt. Creates a pending payment for human verification."""
    p = Payment(organization_id=order.organization_id, order_id=order.id, method="manual_transfer",
                provider="manual", provider_ref=f"manual:{order.id}:{uuid.uuid4().hex[:8]}", amount=order.total,
                evidence=evidence)
    session.add(p)
    return p


def verify_manual_transfer(session: Session, principal: Principal, payment_id: uuid.UUID,
                           amount_received: Decimal, bank_reference: str) -> str:
    if principal.is_service:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "service principals cannot confirm payments")
    payment = session.get(Payment, payment_id, with_for_update=True)
    if payment is None or payment.provider != "manual":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "manual payment not found")
    portfolio_id = _portfolio(session, session.get(Order, payment.order_id))
    if not (principal.can("finance.manage", portfolio_id) or principal.can("refund.request", portfolio_id)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "finance or customer-service authority required")
    payment.verified_by = principal.user_id
    payment.evidence = {**payment.evidence, "bank_reference": bank_reference}
    result = apply_confirmed_payment(session, payment, amount_received, via_gateway=False, actor_type="user",
                                     actor_user_id=principal.user_id)
    audit(session, principal.org_id, "payment.manual_verified", "payment", payment.id, actor_user_id=principal.user_id,
          portfolio_id=portfolio_id, after={"amount": amount_received, "result": result})
    return result
