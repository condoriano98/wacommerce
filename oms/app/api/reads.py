"""Read endpoints for the operations dashboard. Rows are limited by PostgreSQL RLS; portfolio-limited users get order
*slices* (their lines only, no header totals/address/payment data — spec §27.4)."""

from decimal import Decimal

from fastapi import APIRouter
from sqlalchemy import select, text

from app.core.deps import CurrentPrincipal, DbSession
from app.models.governance import ExceptionCase
from app.models.org import Portfolio
from app.models.platforms import IntegrationCapability, PlatformAccount
from app.modules import capital

router = APIRouter(tags=["dashboard"])
CATEGORIES = ("advertising", "inventory", "fulfillment", "opex", "reserve")


@router.get("/orders")
def orders(session: DbSession, principal: CurrentPrincipal, limit: int = 100) -> list[dict]:
    rows = session.execute(text(
        "select order_id, order_number, sales_channel, placed_at, order_status, count(*) as lines, "
        "sum(qty) as units, sum(line_total) as visible_line_total, sum(qty_delivered) as delivered "
        "from order_slices group by 1, 2, 3, 4, 5 order by placed_at desc limit :n"), {"n": min(limit, 500)}).all()
    return [{"order_id": str(r.order_id), "order_number": r.order_number, "channel": r.sales_channel,
             "placed_at": r.placed_at.isoformat() if r.placed_at else None, "status": r.order_status,
             "lines": r.lines, "units": int(r.units or 0), "visible_line_total": str(r.visible_line_total),
             "units_delivered": int(r.delivered or 0)} for r in rows]


@router.get("/exceptions")
def exceptions(session: DbSession, principal: CurrentPrincipal, include_closed: bool = False) -> list[dict]:
    q = select(ExceptionCase)
    if not include_closed:
        q = q.where(ExceptionCase.status.not_in(("resolved", "written_off")))
    return [{"id": str(c.id), "type": c.type, "severity": c.severity, "status": c.status, "owner_role": c.owner_role,
             "exposure": str(c.exposure) if c.exposure is not None else None,
             "sla_due_at": c.sla_due_at.isoformat() if c.sla_due_at else None,
             "subject": f"{c.subject_type}:{c.subject_id}"} for c in session.scalars(q.order_by(ExceptionCase.created_at))]


@router.get("/integrations/capabilities")
def capabilities(session: DbSession, principal: CurrentPrincipal) -> list[dict]:
    rows = session.execute(select(IntegrationCapability, PlatformAccount.provider, PlatformAccount.display_name)
                           .join(PlatformAccount, PlatformAccount.id == IntegrationCapability.platform_account_id)).all()
    accounts = session.scalars(select(PlatformAccount)).all()
    out = [{"account": name, "provider": prov, "capability": c.capability,
            "implementation_status": c.implementation_status, "access_state": c.access_state,
            "operationally_ready": c.implementation_status == "production_verified" and c.access_state == "available"}
           for c, prov, name in rows]
    listed = {(o["account"], o["provider"]) for o in out}
    for a in accounts:  # accounts without registry rows are shown as mocked/planned, never as available
        if (a.display_name, a.provider) not in listed:
            out.append({"account": a.display_name, "provider": a.provider, "capability": "*",
                        "implementation_status": "mock_tested" if not a.live_enabled else "planned",
                        "access_state": "mocked", "operationally_ready": False})
    return out


@router.get("/capital/summary")
def capital_summary(session: DbSession, principal: CurrentPrincipal) -> list[dict]:
    principal.require_any_scope("fpa.read")  # capital data is financial (remediation C1); rows are RLS-limited
    out = []
    for p in session.scalars(select(Portfolio).order_by(Portfolio.code)):
        cats = {}
        for cat in CATEGORIES:
            m = capital.mandate(session, p.id, cat)
            if m:
                committed, consumed = capital.committed(session, p.id, cat)
                cats[cat] = {"mandate": str(m), "committed": str(committed), "consumed": str(consumed),
                             "available": str(capital.available(session, p.id, cat))}
        out.append({"portfolio_id": str(p.id), "code": p.code, "name": p.name, "categories": cats,
                    "total_mandate": str(sum((Decimal(c["mandate"]) for c in cats.values()), Decimal("0")))})
    return out
