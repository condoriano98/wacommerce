"""Operations endpoints: dead-letter review, uncertain external operations, procurement copilot (human dropship).
Row visibility is enforced by PostgreSQL RLS; action permissions by ``Principal.require``."""

import uuid
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core import external_ops
from app.core.deps import CurrentPrincipal, DbSession
from app.core.outbox import requeue_dead_letter
from app.models.fulfillment import PurchaseTask
from app.models.governance import DeadLetter, ExternalOperation
from app.modules import procurement

router = APIRouter(tags=["operations"])


def _org_ops(principal) -> None:
    if not (principal.is_org_owner() or principal.can("finance.manage") or principal.can("exceptions.manage")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "operations review requires an org-scope role")


@router.get("/dead-letters")
def list_dead_letters(session: DbSession, principal: CurrentPrincipal) -> list[dict]:
    _org_ops(principal)
    rows = session.scalars(select(DeadLetter).where(DeadLetter.resolved_at.is_(None))
                           .order_by(DeadLetter.created_at)).all()
    return [{"id": str(d.id), "topic": d.topic, "attempts": d.attempts, "last_error": d.last_error} for d in rows]


@router.post("/dead-letters/{dead_letter_id}/requeue")
def requeue(dead_letter_id: uuid.UUID, session: DbSession, principal: CurrentPrincipal) -> dict:
    _org_ops(principal)
    if session.get(DeadLetter, dead_letter_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dead letter not found")
    requeue_dead_letter(session, dead_letter_id, principal.user_id)
    return {"requeued": str(dead_letter_id)}


@router.get("/external-operations")
def list_external_operations(session: DbSession, principal: CurrentPrincipal, status_: str = "uncertain") -> list:
    _org_ops(principal)
    rows = session.scalars(select(ExternalOperation).where(ExternalOperation.status == status_)).all()
    return [{"id": str(o.id), "provider": o.provider, "operation": o.operation, "idempotency_key": o.idempotency_key,
             "subject": f"{o.subject_type}:{o.subject_id}", "last_error": o.last_error} for o in rows]


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effect_exists: bool
    provider_ref: str | None = None


@router.post("/external-operations/{op_id}/resolve")
def resolve(op_id: uuid.UUID, body: Resolution, session: DbSession, principal: CurrentPrincipal) -> dict:
    try:
        op = external_ops.resolve_uncertain(session, principal, op_id, effect_exists=body.effect_exists,
                                            provider_ref=body.provider_ref)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"id": str(op.id), "status": op.status}


@router.get("/procurement/tasks")
def procurement_tasks(session: DbSession, principal: CurrentPrincipal, include_closed: bool = False) -> list[dict]:
    q = select(PurchaseTask)
    if not include_closed:
        q = q.where(PurchaseTask.status.not_in(("delivered", "canceled", "supplier_canceled")))
    return [{"id": str(t.id), "order_id": str(t.order_id), "portfolio_id": str(t.portfolio_id),
             "marketplace": t.marketplace, "status": t.status,
             "landed_cost": str(t.landed_cost) if t.landed_cost is not None else None,
             "expected_contribution": str(t.expected_contribution) if t.expected_contribution is not None else None}
            for t in session.scalars(q.order_by(PurchaseTask.created_at))]


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price: Decimal = Field(gt=0)
    shipping_cost: Decimal = Field(ge=0)
    in_stock: bool
    variant_matches: bool
    note: str | None = Field(default=None, max_length=500)


@router.post("/procurement/tasks/{task_id}/observations")
def observe(task_id: uuid.UUID, body: Observation, session: DbSession, principal: CurrentPrincipal) -> dict:
    t = procurement.observe(session, principal, task_id, **body.model_dump())
    return {"id": str(t.id), "status": t.status, "approval_id": str(t.approval_id) if t.approval_id else None}


class PurchaseLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_item_id: uuid.UUID
    qty: int = Field(ge=1)
    unit_price: Decimal = Field(gt=0)


class PurchaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_order_id: str = Field(max_length=64)
    amount_paid: Decimal = Field(gt=0)
    lines: list[PurchaseLineIn] = Field(min_length=1)
    evidence_ref: str = Field(max_length=300)


@router.post("/procurement/tasks/{task_id}/purchases", status_code=201)
def record_purchase(task_id: uuid.UUID, body: PurchaseIn, session: DbSession, principal: CurrentPrincipal) -> dict:
    p = procurement.record_purchase(session, principal, task_id, external_order_id=body.external_order_id,
                                    amount_paid=body.amount_paid, evidence_ref=body.evidence_ref,
                                    lines=[(ln.order_item_id, ln.qty, ln.unit_price) for ln in body.lines])
    return {"id": str(p.id)}


@router.post("/ops/invariants/check")
def check_invariants(session: DbSession, principal: CurrentPrincipal) -> dict:
    """Run the invariant checks for the caller's organization (finance). Violations open cases; nothing is fixed."""
    from app.modules import invariants

    principal.require("finance.manage")
    found = invariants.run(session, principal.org_id)
    return {"ok": not found, "violations": found}
