"""Exception cases and playbooks (docs/17-exception-workflows.md)."""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.models.base import utcnow
from app.models.governance import ExceptionCase, ExceptionEvent


@dataclass(frozen=True)
class Playbook:
    severity: str
    owner_role: str
    sla_hours: int
    decisions: tuple[str, ...]
    approval_kind: dict[str, str]  # decision -> approval kind required before execution
    customer_template: str | None


PLAYBOOKS: dict[str, Playbook] = {
    "delivery_failed": Playbook("high", "cs_admin", 24, ("reattempt", "return_to_sender", "cancel_and_refund"),
                                {"cancel_and_refund": "refund"}, "delivery_failed"),
    "returned_to_sender": Playbook("medium", "packing_admin", 72, ("restock", "write_off"),
                                   {"write_off": "inventory_write_off"}, None),
    "cod_refused": Playbook("medium", "cs_admin", 48, ("block_cod_for_customer", "allow"), {}, None),
    "lost_or_damaged": Playbook("high", "cs_admin", 48, ("courier_claim", "replace", "refund"),
                                {"refund": "refund", "replace": "inventory_write_off"}, "lost_or_damaged"),
    "refund_requested": Playbook("high", "finance_manager", 48, ("full_refund", "partial_refund", "reject"),
                                 {"full_refund": "refund", "partial_refund": "refund"}, "refund_update"),
    "cod_settlement_mismatch": Playbook("high", "finance_manager", 72, ("dispute", "accept_variance"),
                                        {"accept_variance": "variance_acceptance"}, None),
    "cod_not_remitted": Playbook("high", "finance_manager", 168, ("dispute",), {}, None),
    "supplier_out_of_stock": Playbook("high", "procurement_admin", 4,
                                      ("alternative_supplier", "wait", "cancel_and_refund"),
                                      {"alternative_supplier": "procurement_purchase", "cancel_and_refund": "refund"},
                                      "delay_notice"),
    "supplier_price_drift": Playbook("high", "portfolio_manager", 4, ("accept_price", "alternative_supplier",
                                                                      "cancel_and_refund"),
                                     {"accept_price": "procurement_purchase", "cancel_and_refund": "refund"}, None),
    "supplier_canceled": Playbook("high", "procurement_admin", 8, ("rebuy", "cancel_and_refund"),
                                  {"rebuy": "procurement_purchase", "cancel_and_refund": "refund"}, "delay_notice"),
    "dropship_late": Playbook("medium", "procurement_admin", 48, ("chase_supplier", "cancel_and_refund"),
                              {"cancel_and_refund": "refund"}, "delay_notice"),
    "wrong_item": Playbook("high", "cs_admin", 48, ("replace", "partial_refund", "return"),
                           {"partial_refund": "refund", "replace": "inventory_write_off"}, None),
    "payment_mismatch": Playbook("high", "finance_manager", 24, ("request_balance", "refund_excess", "accept"),
                                 {"refund_excess": "refund", "accept": "variance_acceptance"}, "payment_mismatch"),
    "marketplace_settlement_mismatch": Playbook("medium", "finance_manager", 168, ("dispute", "accept_variance"),
                                                {"accept_variance": "variance_acceptance"}, None),
    "duplicate_order": Playbook("medium", "cs_admin", 8, ("merge", "keep_both", "cancel_duplicate"), {}, None),
    "oversold_marketplace": Playbook("critical", "portfolio_manager", 2, ("source_from_supplier", "cancel_on_platform"),
                                     {"source_from_supplier": "procurement_purchase"}, None),
    # ADR-015: an external side effect whose outcome is unknown and cannot be looked up (e.g. Biteship booking after a
    # timeout). A human checks the provider dashboard; the OMS never re-sends automatically.
    "external_uncertain": Playbook("high", "finance_manager", 4, ("confirm_effect", "confirm_absent_and_retry"), {}, None),
    # M3 risk alerts (advisory; acceptance #18)
    "liquidity_breach": Playbook("critical", "finance_manager", 24, ("reduce_ad_budget", "delay_inventory_buy",
                                                                    "inject_capital", "accept_risk"),
                                 {"inject_capital": "capital_reallocation"}, None),
    "invariant_violation": Playbook("critical", "finance_manager", 4, ("investigate", "post_adjustment"),
                                    {"post_adjustment": "ledger_adjustment"}, None),
    "margin_breach": Playbook("high", "finance_manager", 72, ("reprice", "cut_channel", "accept_risk"), {}, None),
    "manual_review": Playbook("medium", "portfolio_manager", 8, ("route_manually", "cancel_and_refund"),
                              {"cancel_and_refund": "refund"}, None),
}


def open_case(session: Session, org_id: uuid.UUID, case_type: str, *, subject_type: str, subject_id: str,
              order_id: uuid.UUID | None = None, portfolio_id: uuid.UUID | None = None,
              context: dict | None = None) -> ExceptionCase:
    """Idempotent: a repeated trigger for an open case appends an event instead of creating a new case."""
    pb = PLAYBOOKS[case_type]
    case = session.scalars(select(ExceptionCase).where(
        ExceptionCase.type == case_type, ExceptionCase.subject_type == subject_type,
        ExceptionCase.subject_id == subject_id,
        ExceptionCase.status.not_in(("resolved", "written_off")))).first()
    if case is None:
        case = ExceptionCase(organization_id=org_id, portfolio_id=portfolio_id, type=case_type, severity=pb.severity,
                             subject_type=subject_type, subject_id=subject_id, order_id=order_id,
                             owner_role=pb.owner_role, sla_due_at=utcnow() + timedelta(hours=pb.sla_hours),
                             context=context or {})
        session.add(case)
        session.flush()
        event = "opened"
    else:
        event = "retriggered"
    session.add(ExceptionEvent(organization_id=org_id, case_id=case.id, portfolio_id=portfolio_id, event=event,
                               detail=context or {}))
    return case


def decide(session: Session, case: ExceptionCase, decision: str, *, actor_user_id: uuid.UUID, note: str,
           approval_id: uuid.UUID | None = None) -> ExceptionCase:
    pb = PLAYBOOKS[case.type]
    if decision not in pb.decisions:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"decision must be one of {pb.decisions}")
    if case.status in ("resolved", "written_off"):
        raise HTTPException(status.HTTP_409_CONFLICT, "case already closed")
    case.decision = decision
    needed = pb.approval_kind.get(decision)
    case.status = "awaiting_approval" if needed and approval_id is None else "in_progress"
    case.approval_id = approval_id or case.approval_id
    session.add(ExceptionEvent(organization_id=case.organization_id, case_id=case.id, portfolio_id=case.portfolio_id,
                               actor_user_id=actor_user_id, event="decided",
                               detail={"decision": decision, "note": note, "approval_kind": needed}))
    audit(session, case.organization_id, "exception.decided", "exception_case", case.id,
          actor_user_id=actor_user_id, portfolio_id=case.portfolio_id, after={"decision": decision})
    return case


def resolve(session: Session, case: ExceptionCase, *, actor_user_id: uuid.UUID | None, note: str,
            written_off: bool = False) -> ExceptionCase:
    case.status = "written_off" if written_off else "resolved"
    case.resolved_at = utcnow()
    case.resolution_note = note
    session.add(ExceptionEvent(organization_id=case.organization_id, case_id=case.id, portfolio_id=case.portfolio_id,
                               actor_user_id=actor_user_id, event=case.status, detail={"note": note}))
    return case


def overdue(session: Session) -> list[ExceptionCase]:
    return list(session.scalars(select(ExceptionCase).where(
        ExceptionCase.status.not_in(("resolved", "written_off")), ExceptionCase.sla_due_at < utcnow())))
