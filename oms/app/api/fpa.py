"""FP&A and capital intelligence API (spec §15). Visibility by RLS (portfolio users see their portfolios only);
actions by permission. Simulations are advisory: nothing here moves money or changes budgets."""

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.deps import CurrentPrincipal, DbSession
from app.models.fpa import FpaSnapshot
from app.modules import marketing
from app.modules.finance import periods
from app.modules.fpa import cash, risk, simulator, snapshots, waterfall

router = APIRouter(prefix="/fpa", tags=["fpa"])


def _bounds(start: date | None, end: date | None) -> tuple[date, date]:
    if start and end:
        return start, end
    return periods.month_bounds(start or date.today())


@router.get("/waterfall")
def get_waterfall(session: DbSession, principal: CurrentPrincipal, start: date | None = None, end: date | None = None,
                  legal_entity_id: uuid.UUID | None = None, portfolio_id: uuid.UUID | None = None,
                  level: str = "L3") -> dict:
    # No portfolio and no entity = consolidated: org-wide scope required (remediation C1).
    principal.require_financial_scope("fpa.read", portfolio_id=portfolio_id, legal_entity_id=legal_entity_id)
    s, e = _bounds(start, end)
    if level == "L1":
        return {"period": [str(s), str(e)], "rows": waterfall.l1(session, s, e, legal_entity_id)}
    if level == "L2":
        return {"period": [str(s), str(e)], "portfolios": {
            str(k): v.as_dict() for k, v in waterfall.by_portfolio(session, s, e, legal_entity_id).items()}}
    wf = waterfall.compute(session, s, e, legal_entity_id=legal_entity_id, portfolio_id=portfolio_id)
    return {"period": [str(s), str(e)], **wf.as_dict()}


@router.get("/tie-out")
def get_tie_out(session: DbSession, principal: CurrentPrincipal, legal_entity_id: uuid.UUID,
                start: date | None = None, end: date | None = None) -> dict:
    principal.require("finance.manage")
    s, e = _bounds(start, end)
    return waterfall.tie_out(session, s, e, legal_entity_id)


@router.post("/periods/{legal_entity_id}/{period_start}/close")
def close(legal_entity_id: uuid.UUID, period_start: date, session: DbSession, principal: CurrentPrincipal) -> dict:
    period, snaps = snapshots.close_and_snapshot(session, principal, legal_entity_id, period_start)
    return {"period_start": str(period.period_start), "status": period.status,
            "snapshots": [{"id": str(x.id), "scope": x.scope_type, "version": x.version, "sha256": x.sha256}
                          for x in snaps]}


@router.get("/snapshots")
def list_snapshots(session: DbSession, principal: CurrentPrincipal, period_start: date,
                   portfolio_id: uuid.UUID | None = None, legal_entity_id: uuid.UUID | None = None) -> list[dict]:
    principal.require_financial_scope("fpa.read", portfolio_id=portfolio_id, legal_entity_id=legal_entity_id)
    q = select(FpaSnapshot).where(FpaSnapshot.period_start == periods.month_bounds(period_start)[0])
    if portfolio_id is not None:
        q = q.where(FpaSnapshot.portfolio_id == portfolio_id)
    if legal_entity_id is not None:
        q = q.where(FpaSnapshot.legal_entity_id == legal_entity_id)
    rows = session.scalars(q.order_by(FpaSnapshot.version)).all()
    return [{"id": str(x.id), "scope": x.scope_type, "portfolio_id": str(x.portfolio_id) if x.portfolio_id else None,
             "version": x.version, "sha256": x.sha256, "reason": x.reason, "payload": x.payload} for x in rows]


class StressIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenarios: list[str] | None = None
    seed: str = Field(default="rp100m", pattern="^rp100m$")


@router.post("/simulations/stress")
def stress(body: StressIn, session: DbSession, principal: CurrentPrincipal) -> dict:
    known = set(simulator.SCENARIOS) | set(simulator.PRESETS)
    if body.scenarios and not set(body.scenarios) <= known:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown scenarios; known: {sorted(known)}")
    return risk.stress_and_alert(session, principal, simulator.rp100m_seed(), body.scenarios)


@router.get("/simulations/scenarios")
def scenarios(principal: CurrentPrincipal) -> dict:
    return {"scenarios": sorted(simulator.SCENARIOS), "presets": sorted(simulator.PRESETS),
            "monte_carlo_enabled": simulator.MONTE_CARLO_ENABLED, "engine_version": simulator.ENGINE_VERSION}


@router.get("/cash-forecast")
def cash_forecast(session: DbSession, principal: CurrentPrincipal, legal_entity_id: uuid.UUID | None = None) -> dict:
    principal.require_financial_scope("fpa.read", legal_entity_id=legal_entity_id)
    return cash.forecast_13w(session, legal_entity_id)


class BulkBuyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weekly_demand: list[int] = Field(min_length=1, max_length=52)
    qty: int = Field(gt=0)
    dropship_unit_cost: Decimal = Field(gt=0)
    bulk_unit_cost: Decimal = Field(gt=0)
    carrying_cost_per_unit_week: Decimal = Field(ge=0)
    max_holding_weeks: int = Field(gt=0)
    supplier_reliability: Decimal = Field(ge=0, le=1)
    return_rate: Decimal = Field(ge=0, le=1)
    salvage_rate: Decimal = Field(ge=0, le=1)
    post_purchase_min_cash: Decimal
    reserve: Decimal
    risk_limit: Decimal = Field(ge=0)


@router.post("/bulk-buy/evaluate")
def bulk_buy(body: BulkBuyIn, principal: CurrentPrincipal) -> dict:
    principal.require("fpa.read")
    return cash.evaluate_bulk_buy(cash.BulkBuyInputs(**body.model_dump()))


class AllocationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shares: dict[uuid.UUID, Decimal]
    effective_from: date
    legal_entity_id: uuid.UUID


@router.put("/campaigns/{campaign_id}/allocation")
def set_allocation(campaign_id: uuid.UUID, body: AllocationIn, session: DbSession, principal: CurrentPrincipal) -> dict:
    version = marketing.set_allocation(session, principal, campaign_id, body.shares, body.effective_from,
                                       body.legal_entity_id)
    return {"campaign_id": str(campaign_id), "policy_version": version}
