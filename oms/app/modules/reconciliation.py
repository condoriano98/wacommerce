"""Reconciliation (spec §11, ADR-012; acceptance #11a/#11b, #17).

Every money movement the OMS expects becomes a reconciliation item (expected ref/amount/date). Evidence (bank line,
provider settlement, remittance report) matches it. Items that stay open past their SLA age into exception cases that
carry the financial exposure; variances are never silently netted away.
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.finance import ReconciliationItem
from app.models.governance import ExceptionCase

# kind -> (SLA business days approximated as calendar days, exception case type)
SLA = {
    "bank_manual": (2, "payment_mismatch"),
    "gateway": (3, "payment_mismatch"),
    "cod": (7, "cod_not_remitted"),
    "marketplace": (14, "marketplace_settlement_mismatch"),
    "supplier_refund": (14, "supplier_canceled"),
}
TOLERANCE = Decimal("0.00")


def expect(session: Session, org_id, kind: str, ref: str, amount: Decimal, *, portfolio_id=None,
           expected_at: date | None = None) -> None:
    import uuid

    session.execute(insert(ReconciliationItem).values(
        id=uuid.uuid4(), created_at=utcnow(), organization_id=org_id, portfolio_id=portfolio_id, kind=kind,
        expected_ref=ref, expected_amount=amount, expected_at=expected_at or utcnow().date(), status="open",
        evidence={}).on_conflict_do_nothing(index_elements=["kind", "expected_ref"]))


def match(session: Session, kind: str, ref: str, *, actual_ref: str, actual_amount: Decimal, evidence: dict,
          operator_user_id=None) -> ReconciliationItem | None:
    item = session.scalars(select(ReconciliationItem).where(ReconciliationItem.kind == kind,
                                                            ReconciliationItem.expected_ref == ref)
                           .with_for_update()).first()
    if item is None:
        return None
    item.actual_ref, item.actual_amount = actual_ref, actual_amount
    item.difference = actual_amount - item.expected_amount
    item.evidence = {**item.evidence, **evidence}
    item.operator_user_id = operator_user_id
    if abs(item.difference) <= TOLERANCE:
        item.status, item.resolved_at = "matched", utcnow()
        if item.exception_case_id is not None:  # aged item settled late: close its case with the evidence
            from app.models.governance import ExceptionCase
            from app.modules import exceptions

            case = session.get(ExceptionCase, item.exception_case_id)
            if case is not None and case.status not in ("resolved", "written_off"):
                exceptions.resolve(session, case, actor_user_id=operator_user_id,
                                   note=f"settled late: {actual_ref}")
    else:
        item.status, item.classification = "variance", "fee_variance" if item.difference < 0 else "unknown"
        _case(session, item, exposure=abs(item.difference))
    return item


def _case(session: Session, item: ReconciliationItem, exposure: Decimal) -> ExceptionCase:
    from app.modules import exceptions

    _, case_type = SLA.get(item.kind, (7, "manual_review"))
    case = exceptions.open_case(session, item.organization_id, case_type, subject_type="reconciliation_item",
                                subject_id=str(item.id), portfolio_id=item.portfolio_id,
                                context={"kind": item.kind, "expected": str(item.expected_amount),
                                         "actual": str(item.actual_amount), "ref": item.expected_ref})
    case.exposure = exposure
    item.exception_case_id = case.id
    return case


def age(session: Session, today: date | None = None) -> list[ReconciliationItem]:
    """Open items beyond SLA → exception with full exposure (acceptance #11a/#11b)."""
    today = today or utcnow().date()
    aged = []
    for item in session.scalars(select(ReconciliationItem).where(ReconciliationItem.status == "open")).all():
        days, _ = SLA.get(item.kind, (7, "manual_review"))
        if item.expected_at + timedelta(days=days) < today and item.exception_case_id is None:
            item.classification = "missing"
            _case(session, item, exposure=item.expected_amount)
            aged.append(item)
    return aged


def bucket(item: ReconciliationItem, today: date | None = None) -> str:
    d = ((today or utcnow().date()) - item.expected_at).days
    return "0-2" if d <= 2 else "3-7" if d <= 7 else "8-14" if d <= 14 else ">14"


def open_exposure(session: Session, portfolio_id=None) -> Decimal:
    stmt = select(ReconciliationItem).where(ReconciliationItem.status.in_(("open", "variance")))
    if portfolio_id:
        stmt = stmt.where(ReconciliationItem.portfolio_id == portfolio_id)
    total = Decimal("0")
    for item in session.scalars(stmt):
        total += abs(item.difference) if item.status == "variance" else item.expected_amount
    return total
