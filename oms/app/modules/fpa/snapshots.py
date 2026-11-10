"""FP&A snapshots (spec §15 close & audit, acceptance #13b). Snapshots are appended, never edited (DB trigger); a
restatement creates version n+1 with a reason. Closing a period snapshots the entity (L3) and every portfolio (L2)."""

import hashlib
import json
import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.authz import Principal
from app.models.fpa import FpaSnapshot
from app.modules.finance import periods
from app.modules.fpa import waterfall


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _next_version(session: Session, legal_entity_id, scope_type: str, portfolio_id, period_start: date) -> int:
    v = session.scalar(select(func.max(FpaSnapshot.version)).where(
        FpaSnapshot.legal_entity_id == legal_entity_id, FpaSnapshot.scope_type == scope_type,
        FpaSnapshot.portfolio_id.is_(None) if portfolio_id is None else FpaSnapshot.portfolio_id == portfolio_id,
        FpaSnapshot.period_start == period_start))
    return (v or 0) + 1


def snapshot(session: Session, principal: Principal, legal_entity_id: uuid.UUID, period_start: date, *,
             reason: str) -> list[FpaSnapshot]:
    start, end = periods.month_bounds(period_start)
    scopes: list[tuple[str, uuid.UUID | None, dict]] = [
        ("legal_entity", None, {"waterfall": waterfall.compute(session, start, end,
                                                               legal_entity_id=legal_entity_id).as_dict(),
                                "tie_out": waterfall.tie_out(session, start, end, legal_entity_id)})]
    for pid, wf in sorted(waterfall.by_portfolio(session, start, end, legal_entity_id).items(), key=lambda x: str(x[0])):
        scopes.append(("portfolio", pid, {"waterfall": wf.as_dict()}))
    out = []
    for scope_type, pid, body in scopes:
        payload = {"period_start": str(start), "period_end": str(end), "legal_entity_id": str(legal_entity_id), **body}
        snap = FpaSnapshot(organization_id=principal.org_id, legal_entity_id=legal_entity_id, scope_type=scope_type,
                           portfolio_id=pid, period_start=start,
                           version=_next_version(session, legal_entity_id, scope_type, pid, start),
                           payload=payload, sha256=hashlib.sha256(_canonical(payload).encode()).hexdigest(),
                           created_by=principal.user_id, reason=reason)
        session.add(snap)
        out.append(snap)
    session.flush()
    audit(session, principal.org_id, "fpa.snapshot", "fpa_snapshot", out[0].id, actor_user_id=principal.user_id,
          after={"period_start": str(start), "versions": [s.version for s in out], "reason": reason})
    return out


def close_and_snapshot(session: Session, principal: Principal, legal_entity_id: uuid.UUID, period_start: date):
    period = periods.close_period(session, principal, legal_entity_id, period_start)
    return period, snapshot(session, principal, legal_entity_id, period_start, reason="period_close")


def latest(session: Session, legal_entity_id, period_start: date, *, portfolio_id=None) -> FpaSnapshot | None:
    scope = "portfolio" if portfolio_id else "legal_entity"
    return session.scalars(select(FpaSnapshot).where(
        FpaSnapshot.legal_entity_id == legal_entity_id, FpaSnapshot.scope_type == scope,
        FpaSnapshot.portfolio_id.is_(None) if portfolio_id is None else FpaSnapshot.portfolio_id == portfolio_id,
        FpaSnapshot.period_start == periods.month_bounds(period_start)[0]).order_by(FpaSnapshot.version.desc())).first()
