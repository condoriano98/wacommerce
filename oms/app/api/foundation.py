import uuid
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, text

from app.core.db import tenant_session
from app.core.deps import CurrentPrincipal, DbSession
from app.core.security import issue_token, verify_password
from app.models.governance import AuditLog, HumanApproval
from app.models.org import Portfolio, RiskPolicy
from app.models.platforms import PlatformAccount
from app.modules import approvals, capital, org, platforms

router = APIRouter()


class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/auth/login", tags=["auth"])
def login(body: LoginIn) -> dict:
    with tenant_session(None, None) as session:
        row = session.execute(text("select * from auth_lookup_user(:e)"), {"e": body.email}).mappings().first()
    if not row or not row["is_active"] or not row["password_hash"] or not verify_password(
        row["password_hash"], body.password
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return {"access_token": issue_token(row["id"], row["organization_id"]), "token_type": "bearer"}


class PortfolioIn(BaseModel):
    brand_id: uuid.UUID
    code: str
    name: str
    category: str | None = None
    risk_class: str = "medium"
    target_contribution_margin: Decimal | None = None
    target_roic: Decimal | None = None
    max_cpa: Decimal | None = None
    max_inventory_days: int | None = None
    approved_channels: list[str] = []
    approved_procurement_sources: list[str] = []


@router.get("/portfolios", tags=["portfolios"])
def list_portfolios(session: DbSession, principal: CurrentPrincipal) -> list[dict]:
    rows = session.scalars(select(Portfolio).order_by(Portfolio.code)).all()  # RLS filters
    return [{"id": str(p.id), "code": p.code, "name": p.name, "status": p.status, "risk_class": p.risk_class,
             "category": p.category} for p in rows]


@router.post("/portfolios", tags=["portfolios"], status_code=201)
def create_portfolio(body: PortfolioIn, session: DbSession, principal: CurrentPrincipal) -> dict:
    p = org.create_portfolio(session, principal, **body.model_dump())
    return {"id": str(p.id)}


class BudgetIn(BaseModel):
    period_start: date
    period_end: date
    category: str
    amount: Decimal
    provider: str | None = None


@router.put("/portfolios/{portfolio_id}/budgets", tags=["portfolios"])
def set_budget(portfolio_id: uuid.UUID, body: BudgetIn, session: DbSession, principal: CurrentPrincipal) -> dict:
    b = org.set_budget(session, principal, portfolio_id, **body.model_dump())
    return {"id": str(b.id), "amount": str(b.amount)}


@router.get("/risk-policies", tags=["risk"])
def list_risk_policies(session: DbSession, principal: CurrentPrincipal) -> list[dict]:
    return [{"id": str(r.id), "key": r.key, "threshold": str(r.threshold), "comparator": r.comparator,
             "portfolio_id": str(r.portfolio_id) if r.portfolio_id else None, "severity": r.severity}
            for r in session.scalars(select(RiskPolicy)).all()]


class ThresholdIn(BaseModel):
    threshold: Decimal
    reason: str


@router.patch("/risk-policies/{policy_id}", tags=["risk"])
def override_threshold(policy_id: uuid.UUID, body: ThresholdIn, session: DbSession,
                       principal: CurrentPrincipal) -> dict:
    r = org.override_risk_threshold(session, principal, policy_id, body.threshold, body.reason)
    return {"id": str(r.id), "threshold": str(r.threshold)}


class CapitalIn(BaseModel):
    from_portfolio_id: uuid.UUID | None = None
    to_portfolio_id: uuid.UUID
    category: str = "advertising"
    amount: Decimal
    reason: str


@router.post("/capital/reallocations", tags=["capital"], status_code=201)
def request_reallocation(body: CapitalIn, session: DbSession, principal: CurrentPrincipal) -> dict:
    a = capital.request_reallocation(session, principal, **body.model_dump())
    return {"approval_id": str(a.id), "status": a.status}


@router.post("/capital/reallocations/{approval_id}/execute", tags=["capital"])
def execute_reallocation(approval_id: uuid.UUID, session: DbSession, principal: CurrentPrincipal) -> dict:
    """Changes capital mandates only — never creates bank/cash journals (spec §10, acceptance #16)."""
    evs = capital.execute_reallocation(session, principal, approval_id)
    return {"events": [str(e.id) for e in evs]}


class MandateIn(BaseModel):
    category: str
    amount: Decimal
    effective_from: date
    reason: str


@router.put("/portfolios/{portfolio_id}/mandates", tags=["capital"])
def set_mandate(portfolio_id: uuid.UUID, body: MandateIn, session: DbSession, principal: CurrentPrincipal) -> dict:
    ev = capital.set_mandate(session, principal, portfolio_id, **body.model_dump())
    return {"event_id": str(ev.id), "available": str(capital.available(session, portfolio_id, body.category))}


class DecisionIn(BaseModel):
    approve: bool
    reason: str


@router.get("/approvals", tags=["approvals"])
def list_approvals(session: DbSession, principal: CurrentPrincipal, status_filter: str = "pending") -> list[dict]:
    rows = session.scalars(select(HumanApproval).where(HumanApproval.status == status_filter)
                           .order_by(HumanApproval.created_at)).all()
    return [{"id": str(a.id), "kind": a.kind, "subject": f"{a.subject_type}:{a.subject_id}",
             "amount": str(a.amount) if a.amount is not None else None, "payload": a.payload,
             "requested_by": str(a.requested_by), "portfolio_id": str(a.portfolio_id) if a.portfolio_id else None}
            for a in rows]


@router.post("/approvals/{approval_id}/decision", tags=["approvals"])
def decide_approval(approval_id: uuid.UUID, body: DecisionIn, session: DbSession, principal: CurrentPrincipal) -> dict:
    a = approvals.decide(session, principal, approval_id, body.approve, body.reason)
    return {"id": str(a.id), "status": a.status}


class AccountIn(BaseModel):
    provider: str
    provider_account_id: str
    display_name: str
    role: str
    portfolio_ids: list[uuid.UUID] = []
    public_config: dict = {}
    api_version: str | None = None
    brand_id: uuid.UUID | None = None


@router.get("/platform-accounts", tags=["platforms"])
def list_accounts(session: DbSession, principal: CurrentPrincipal) -> list[dict]:
    return [platforms.serialize_account(a) for a in session.scalars(select(PlatformAccount)).all()]


@router.post("/platform-accounts", tags=["platforms"], status_code=201)
def register_account(body: AccountIn, session: DbSession, principal: CurrentPrincipal) -> dict:
    a = platforms.register_account(session, principal, **body.model_dump())
    return {"id": str(a.id)}


class CredentialIn(BaseModel):
    name: str
    secret: str
    expires_at: datetime | None = None


@router.put("/platform-accounts/{account_id}/credentials", tags=["platforms"], status_code=204)
def put_credential(account_id: uuid.UUID, body: CredentialIn, session: DbSession, principal: CurrentPrincipal) -> None:
    platforms.store_credential(session, principal, account_id, body.name, body.secret, body.expires_at)


@router.get("/platform-accounts/{account_id}/credentials", tags=["platforms"])
def get_credentials(account_id: uuid.UUID, session: DbSession, principal: CurrentPrincipal) -> list[dict]:
    return platforms.list_credentials(session, account_id)  # names and metadata only, never secrets


@router.get("/audit-logs", tags=["governance"])
def audit_logs(session: DbSession, principal: CurrentPrincipal, limit: int = 100) -> list[dict]:
    principal.require("audit.read")
    rows = session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)).all()
    return [{"at": r.created_at, "action": r.action, "entity": r.entity, "entity_id": r.entity_id,
             "actor": str(r.actor_user_id) if r.actor_user_id else r.actor_type, "after": r.after} for r in rows]
