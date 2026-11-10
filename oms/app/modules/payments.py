"""Payments (V3.1; spec §16, §27.3).

Gateway: notification = trigger → adapter re-checks status → ``captured`` (Dr 1010 / Cr 2200) → routing.
Manual transfer: ``reported`` (receipt, no journal) → ``attested`` (finance + bank ref, no journal) →
``bank_matched`` only from a bank statement line (Dr 1000 / Cr 2200, or Cr 1105 if already delivered).
Release before the bank match only under an approved per-portfolio "release on attestation" exposure limit.
"""

import hashlib
import re
import uuid
from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.authz import Principal
from app.core.outbox import handler
from app.models.base import utcnow
from app.models.finance import BankStatementLine, LedgerEntry
from app.models.orders import Order, OrderItem, Payment, PaymentEvent, PaymentNotification
from app.models.org import RiskPolicy
from app.modules import events, exceptions, orders, reconciliation
from app.modules.finance import accounts as A
from app.modules.finance import posting


def _transition(session: Session, p: Payment, to: str, actor_user_id=None, **detail) -> None:
    session.add(PaymentEvent(organization_id=p.organization_id, payment_id=p.id, order_id=p.order_id,
                             from_status=p.status, to_status=to, actor_user_id=actor_user_id, detail=detail))
    p.status = to


def _portfolio(session: Session, order: Order):
    return session.scalars(select(OrderItem.portfolio_id).where(OrderItem.order_id == order.id)).first()


def create_payment_link(session: Session, order: Order, *, provider: str, provider_ref: str, url: str,
                        expires_at) -> Payment:
    if order.payment_method != "doku_link" or order.payment_status != "unpaid":
        raise HTTPException(status.HTTP_409_CONFLICT, f"order payment is {order.payment_status}")
    existing = session.scalars(select(Payment).where(Payment.order_id == order.id, Payment.status == "pending",
                                                     Payment.provider == provider)).first()
    if existing:
        return existing
    p = Payment(organization_id=order.organization_id, order_id=order.id, method="doku_link", provider=provider,
                provider_ref=provider_ref, amount=order.total, payment_url=url, expires_at=expires_at)
    session.add(p)
    return p


def request_doku_link(session: Session, order: Order, *, expiry_minutes: int = 60) -> Payment:
    """Create the pending payment and queue the provider call (ADR-015: invoice_number = idempotency key, reused on
    every retry; the link URL is filled in by the ``doku.create_payment`` job once DOKU confirms)."""
    from app.core.outbox import enqueue

    if order.payment_method != "doku_link" or order.payment_status != "unpaid":
        raise HTTPException(status.HTTP_409_CONFLICT, f"order payment is {order.payment_status}")
    existing = session.scalars(select(Payment).where(Payment.order_id == order.id, Payment.status == "pending",
                                                     Payment.provider == "doku")).first()
    if existing:
        return existing
    pid = uuid.uuid4()
    p = Payment(id=pid, organization_id=order.organization_id, order_id=order.id, method="doku_link",
                provider="doku", provider_ref=f"INV-{pid.hex[:20].upper()}", amount=order.total,
                evidence={"expiry_minutes": expiry_minutes})
    session.add(p)
    session.flush()
    enqueue(session, order.organization_id, "doku.create_payment", f"doku:{p.id}", {"payment_id": str(p.id)})
    return p


def _mismatch(session: Session, order: Order, p: Payment, received: Decimal) -> str:
    exceptions.open_case(session, order.organization_id, "payment_mismatch", subject_type="payment",
                         subject_id=str(p.id), order_id=order.id, portfolio_id=_portfolio(session, order),
                         context={"expected": str(p.amount), "received": str(received)})
    p.evidence = {**p.evidence, "received_amount": str(received)}
    orders.derive_status(session, order)
    return "mismatch"


def record_gateway_notification(session: Session, *, provider: str, notification_id: str, provider_ref: str,
                                verified_status: str, verified_amount: Decimal, raw: dict,
                                fee: Decimal | None = None) -> str:
    """Called by the adapter after signature verification AND an API status re-check (G-DOKU)."""
    p = session.scalars(select(Payment).where(Payment.provider == provider,
                                              Payment.provider_ref == provider_ref)).first()
    if p is None:
        return "unknown_payment"
    res = session.execute(insert(PaymentNotification).values(
        id=uuid.uuid4(), created_at=utcnow(), organization_id=p.organization_id, provider=provider,
        notification_id=notification_id, payment_id=p.id, raw=raw, verified=True)
        .on_conflict_do_nothing(index_elements=["organization_id", "provider", "notification_id"])
        .returning(PaymentNotification.id)).scalar()
    if res is None:  # duplicate notification
        return "duplicate"
    order = session.get(Order, p.order_id, with_for_update=True)
    if verified_status != "paid":
        if verified_status in ("expired",):
            _transition(session, p, "expired")
        return verified_status  # DOKU Checkout: FAILED notifications are ignored (verified 2026-11-10)
    if p.status == "captured":
        return "duplicate"
    if verified_amount != p.amount:
        return _mismatch(session, order, p, verified_amount)
    _transition(session, p, "captured")
    p.paid_at = utcnow()
    order.payment_status = "captured"
    posting.payment_captured(session, order, p, verified_amount)
    if fee:
        posting.gateway_fee(session, order, p, fee)
    reconciliation.expect(session, order.organization_id, "gateway", str(p.id), verified_amount - (fee or 0),
                          platform_account_id=order.platform_account_id, portfolio_id=_portfolio(session, order))
    events.emit(session, "payment_captured", order=order, payment=p)
    events.emit(session, "payment_released", order=order, payment=p, basis="captured")
    orders.derive_status(session, order)
    return "captured"


def report_manual_transfer(session: Session, order: Order, evidence: dict) -> Payment:
    """Customer says they paid (receipt screenshot). Creates no journal and releases nothing."""
    p = Payment(organization_id=order.organization_id, order_id=order.id, method="manual_transfer", provider="manual",
                provider_ref=f"manual:{order.order_number}:{uuid.uuid4().hex[:6]}", amount=order.total,
                evidence=evidence, status="reported")
    session.add(p)
    session.flush()
    session.add(PaymentEvent(organization_id=p.organization_id, payment_id=p.id, order_id=order.id, from_status=None,
                             to_status="reported", detail={"evidence": list(evidence)}))
    order.payment_status = "reported"
    return p


def release_limit(session: Session, portfolio_id) -> Decimal | None:
    pol = session.scalars(select(RiskPolicy).where(RiskPolicy.key == "release_on_attestation_limit",
                                                   RiskPolicy.portfolio_id == portfolio_id,
                                                   RiskPolicy.active.is_(True))).first()
    return pol.threshold if pol else None


def unverified_exposure(session: Session, portfolio_id) -> Decimal:
    """Value released or delivered against attestations not yet bank-matched (1105 + released-but-undelivered)."""
    on_1105 = Decimal(session.scalar(select(func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0)).where(
        LedgerEntry.account_code == A.TRANSFER_RECEIVABLE, LedgerEntry.portfolio_id == portfolio_id)))
    released = Decimal(session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).join(
        Order, Order.id == Payment.order_id).where(Payment.status == "attested", Order.payment_status == "released",
                                                   Order.delivered_at.is_(None))))
    return on_1105 + released


def attest(session: Session, principal: Principal, payment_id: uuid.UUID, *, bank_reference: str,
           amount_seen: Decimal) -> str:
    """Finance confirms they saw the transfer in the bank portal. Still NO journal (spec §27.3)."""
    if principal.is_service:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "service principals cannot attest payments")
    p = session.get(Payment, payment_id, with_for_update=True)
    if p is None or p.provider != "manual":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "manual payment not found")
    order = session.get(Order, p.order_id, with_for_update=True)
    pid = _portfolio(session, order)
    principal.require("finance.manage", pid)
    if p.status != "reported":
        raise HTTPException(status.HTTP_409_CONFLICT, f"payment is {p.status}")
    if amount_seen != p.amount:
        return _mismatch(session, order, p, amount_seen)
    _transition(session, p, "attested", principal.user_id, bank_reference=bank_reference)
    p.verified_by = principal.user_id
    p.evidence = {**p.evidence, "bank_reference": bank_reference}
    order.payment_status = "attested"
    reconciliation.expect(session, order.organization_id, "bank_manual", str(p.id), p.amount, portfolio_id=pid)
    limit = release_limit(session, pid)
    if limit is not None and unverified_exposure(session, pid) + p.amount <= limit:
        order.payment_status = "released"
        events.emit(session, "payment_released", order=order, payment=p, basis="attested_policy")
    audit(session, principal.org_id, "payment.attested", "payment", p.id, actor_user_id=principal.user_id,
          portfolio_id=pid, after={"amount": amount_seen, "released": order.payment_status == "released"})
    orders.derive_status(session, order)
    return order.payment_status


def import_bank_statement(session: Session, principal: Principal, bank_account_id: uuid.UUID, rows: list[dict],
                          batch: str) -> int:
    """rows: {booked_at: date, amount: Decimal, reference: str, counterparty: str}. Duplicate lines are ignored."""
    principal.require("finance.manage")
    n = 0
    for seq, r in enumerate(rows):
        fp = hashlib.sha256(f"{bank_account_id}|{r['booked_at']}|{r['amount']}|{r.get('reference')}|{seq}".encode()
                            ).hexdigest()
        res = session.execute(insert(BankStatementLine).values(
            id=uuid.uuid4(), created_at=utcnow(), organization_id=principal.org_id, bank_account_id=bank_account_id,
            booked_at=r["booked_at"], amount=Decimal(str(r["amount"])), reference=r.get("reference"),
            counterparty=r.get("counterparty"), import_batch=batch, fingerprint=fp)
            .on_conflict_do_nothing(index_elements=["bank_account_id", "fingerprint"])
            .returning(BankStatementLine.id)).scalar()
        n += res is not None
    return n


_SEP = r"[-_/.:]"


def contains_ref(reference: str | None, key: str | None) -> bool:
    """Whole-reference match after normalisation (round 2): ``key`` must appear in ``reference`` as a complete token,
    so ``PO-1`` never matches ``PO-10`` or ``PO-1-2`` and ``BCA-77`` never matches ``BCA-777``."""
    if not reference or not key or not key.strip():
        return False
    k = re.escape(key.strip().upper())
    pattern = rf"(?<![A-Z0-9])(?<![A-Z0-9]{_SEP}){k}(?![A-Z0-9])(?!{_SEP}[A-Z0-9])"
    return re.search(pattern, reference.upper()) is not None


def match_bank_lines(session: Session, principal: Principal) -> int:
    """Match unmatched bank credits to reported/attested manual transfers (round 2, P0-2).

    A line can settle a payment only if: same organization; the bank account belongs to the **order's legal entity**
    (``posting.entity_for_order``); exact amount; the payment reference, order number or attested bank reference
    appears as a whole token in the line's reference. Exactly one candidate is required — several open a
    ``payment_mismatch`` case and nothing is matched. Lines are locked (``FOR UPDATE SKIP LOCKED``) and payments are
    re-checked under lock, so concurrent runs recognise the cash once."""
    from app.models.org import BankAccount

    principal.require("finance.manage")
    matched = 0
    lines = session.scalars(select(BankStatementLine).where(
        BankStatementLine.organization_id == principal.org_id, BankStatementLine.matched_id.is_(None),
        BankStatementLine.amount > 0).order_by(BankStatementLine.booked_at, BankStatementLine.id)
        .with_for_update(skip_locked=True)).all()
    for line in lines:
        ba = session.get(BankAccount, line.bank_account_id)
        if ba is None or ba.organization_id != line.organization_id or ba.legal_entity_id is None:
            continue
        cands = session.scalars(select(Payment).where(
            Payment.organization_id == line.organization_id, Payment.provider == "manual",
            Payment.status.in_(("reported", "attested")), Payment.amount == line.amount)
            .order_by(Payment.id)).all()
        fits = []
        for p in cands:
            order = session.get(Order, p.order_id)
            if posting.entity_for_order(session, order) != ba.legal_entity_id:
                continue  # bank evidence of another legal entity can never settle this order
            if contains_ref(line.reference, p.provider_ref) or contains_ref(line.reference, order.order_number) \
                    or contains_ref(line.reference, (p.evidence or {}).get("bank_reference")):
                fits.append(p)
        if len(fits) > 1:
            exceptions.open_case(session, line.organization_id, "payment_mismatch", subject_type="bank_statement_line",
                                 subject_id=str(line.id), context={"reason": "several manual payments fit this line",
                                                                   "candidates": [str(p.id) for p in fits]})
            continue
        if not fits:
            continue
        hit = session.get(Payment, fits[0].id, with_for_update=True)
        if hit.status not in ("reported", "attested"):  # matched meanwhile by another run
            continue
        order = session.get(Order, hit.order_id, with_for_update=True)
        _transition(session, hit, "bank_matched", principal.user_id, bank_line=str(line.id))
        line.matched_type, line.matched_id = "payment", str(hit.id)
        was_released = order.payment_status == "released"
        for item in orders.items_of(session, order.id):  # F13b: release any allowance before clearing 1105
            allowance = -posting._balance(session, A.CREDIT_LOSS_ALLOWANCE, order_item_id=item.id)
            if allowance > 0:
                posting.credit_loss(session, order, item, allowance, "release", f"bankmatch:{hit.id}:{item.id}")
        posting.payment_bank_matched(session, order, hit, line.amount)
        reconciliation.match(session, order.organization_id, "bank_manual", str(hit.id), actual_ref=str(line.id),
                             actual_amount=line.amount,
                             evidence={"bank_line": str(line.id), "reference": line.reference})
        order.payment_status = "bank_matched"
        events.emit(session, "payment_bank_matched", order=order, payment=hit)
        if not was_released:
            events.emit(session, "payment_released", order=order, payment=hit, basis="bank_matched")
        orders.derive_status(session, order)
        matched += 1
    matched += _match_payout_receipts(session, principal, [ln for ln in lines if ln.matched_id is None])
    return matched


def _match_payout_receipts(session: Session, principal: Principal, lines: list[BankStatementLine]) -> int:
    """Provider payouts (gateway, carrier withdrawal, marketplace, supplier refund) become verified cash only here:
    a bank credit of the same legal entity, with the payout token in its reference and the exact amount
    (remediation B5). Each line matches at most one payout; each payout is matched once."""
    from app.models.finance import ReconciliationItem
    from app.models.org import BankAccount

    matched = 0
    for line in lines:
        ba = session.get(BankAccount, line.bank_account_id)
        ref = (line.reference or "").upper()
        cands = session.scalars(select(ReconciliationItem).where(
            ReconciliationItem.organization_id == line.organization_id, ReconciliationItem.kind == "bank_payout",
            ReconciliationItem.status == "open", ReconciliationItem.identity_quarantined.is_(False),
            ReconciliationItem.expected_amount == line.amount).order_by(ReconciliationItem.id).with_for_update()).all()
        fits = [c for c in cands if (c.evidence.get("token") or "~").upper() in ref
                and c.evidence.get("legal_entity_id") == (str(ba.legal_entity_id) if ba.legal_entity_id else None)]
        if len(fits) > 1:  # e.g. the same payout id on two provider accounts: never pick one (round 2, P0-1)
            exceptions.open_case(session, line.organization_id, "bank_unmatched", subject_type="bank_statement_line",
                                 subject_id=str(line.id), context={"reason": "ambiguous payout reference",
                                                                   "candidates": [str(c.id) for c in fits]})
            continue
        if not fits:
            continue
        hit = fits[0]
        posting.bank_receipt(session, principal.org_id, ba.legal_entity_id, str(line.id),
                             [tuple(x) for x in hit.evidence.get("sources", [])])
        line.matched_type, line.matched_id = "payout", str(hit.id)
        reconciliation.match_item(session, hit, actual_ref=str(line.id), actual_amount=line.amount,
                                  evidence={"bank_line": str(line.id)}, operator_user_id=principal.user_id)
        matched += 1
    return matched


def today() -> date:
    return utcnow().date()


@handler("doku.create_payment")
def _create_doku_payment(session: Session, msg) -> None:
    from app.core import external_ops
    from app.integrations import factory
    from app.integrations.doku import PaymentRequest

    p = session.get(Payment, uuid.UUID(msg.payload["payment_id"]), with_for_update=True)
    if p is None or p.payment_url or p.status != "pending":
        return
    provider = factory.payment_provider(None)
    req = PaymentRequest(p.provider_ref, p.amount, int(p.evidence.get("expiry_minutes", 60)))
    try:
        resp = external_ops.execute(p.organization_id, "doku", "create_payment", p.provider_ref,
                                    lambda: provider.create_payment(req),
                                    lookup=lambda: provider.get_status(p.provider_ref),
                                    summary={"invoice_number": p.provider_ref, "amount": str(p.amount)},
                                    subject_type="payment", subject_id=str(p.id), order_id=p.order_id)
    except external_ops.OperationUncertain as exc:  # never re-created automatically; a human resolves the case
        p.evidence = {**p.evidence, "link_status": "uncertain", "external_operation_id": str(exc.operation_id)}
        return
    p.payment_url = resp.get("url")
    if resp.get("expires_at"):
        from datetime import datetime

        p.expires_at = datetime.fromisoformat(resp["expires_at"])
    p.evidence = {**p.evidence, "link_status": "created"}
