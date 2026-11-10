"""Human approvals for irreversible or financially material actions.

Separation of duties is enforced by a CHECK constraint (approver ≠ requester). Who may approve is
decided by kind and amount tier below.
"""

import uuid
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.authz import Principal
from app.models.base import utcnow
from app.models.governance import HumanApproval

# kind -> (permission required, amount above which only an org owner may approve)
APPROVAL_RULES: dict[str, tuple[str, Decimal | None]] = {
    "procurement_purchase": ("procurement.approve", Decimal("5000000")),
    "refund": ("refund.approve", Decimal("2000000")),
    "capital_reallocation": ("capital.approve", Decimal("0")),
    "price_change": ("pricing.manage", None),
    "discount_override": ("pricing.manage", None),
    "ad_budget_change": ("marketing.manage", Decimal("5000000")),
    "credential_change": ("credentials.manage", None),
    "ledger_adjustment": ("finance.manage", Decimal("0")),
    "inventory_write_off": ("inventory.manage", Decimal("500000")),
    "variance_acceptance": ("finance.manage", Decimal("1000000")),
    "paid_shipment_booking": ("fulfillment.operate", None),
    "risk_threshold_override": ("finance.manage", Decimal("0")),
}


def request_approval(
    session: Session,
    principal: Principal,
    *,
    kind: str,
    subject_type: str,
    subject_id: str,
    amount: Decimal | None,
    payload: dict,
    portfolio_id: uuid.UUID | None,
    idempotency_key: str,
) -> HumanApproval:
    if kind not in APPROVAL_RULES:
        raise ValueError(f"unknown approval kind {kind}")
    existing = session.scalars(
        select(HumanApproval).where(HumanApproval.idempotency_key == idempotency_key)
    ).first()
    if existing:
        return existing
    approval = HumanApproval(
        organization_id=principal.org_id,
        portfolio_id=portfolio_id,
        kind=kind,
        subject_type=subject_type,
        subject_id=subject_id,
        amount=amount,
        payload=payload,
        requested_by=principal.user_id,
        idempotency_key=idempotency_key,
    )
    session.add(approval)
    audit(session, principal.org_id, "approval.requested", "human_approval", approval.id,
          actor_user_id=principal.user_id, portfolio_id=portfolio_id,
          after={"kind": kind, "amount": amount, "subject": f"{subject_type}:{subject_id}"})
    return approval


def decide(session: Session, principal: Principal, approval_id: uuid.UUID, approve: bool, reason: str) -> HumanApproval:
    approval = session.get(HumanApproval, approval_id, with_for_update=True)
    if approval is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    if approval.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"approval already {approval.status}")
    if approval.requested_by == principal.user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "requester cannot approve their own request")
    permission, owner_above = APPROVAL_RULES[approval.kind]
    principal.require(permission, approval.portfolio_id)
    above_tier = owner_above is not None and approval.amount is not None and approval.amount > owner_above
    if above_tier and not principal.is_org_owner():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "amount above tier requires an organization owner")
    approval.status = "approved" if approve else "rejected"
    approval.decided_by = principal.user_id
    approval.decided_at = utcnow()
    approval.reason = reason
    audit(session, principal.org_id, f"approval.{approval.status}", "human_approval", approval.id,
          actor_user_id=principal.user_id, portfolio_id=approval.portfolio_id,
          after={"kind": approval.kind, "amount": approval.amount, "reason": reason})
    return approval


def require_approved(session: Session, approval_id: uuid.UUID | None, kind: str, subject_id: str) -> HumanApproval:
    """Guard used immediately before executing an irreversible action."""
    if approval_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{kind} requires an approval")
    approval = session.get(HumanApproval, approval_id, with_for_update=True)
    if approval is None or approval.kind != kind or approval.subject_id != subject_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "approval does not match this action")
    if approval.status != "approved":
        raise HTTPException(status.HTTP_409_CONFLICT, f"approval is {approval.status}")
    approval.status = "executed"
    return approval
