"""Simulation persistence and risk alerts (acceptance #18). A run that breaches an active risk policy opens an exception
case carrying the exposure and enqueues a notification through the outbox. Advisory only: nothing is executed."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.authz import Principal
from app.core.outbox import enqueue, handler
from app.models.fpa import SimulationRun
from app.models.org import RiskPolicy
from app.modules import exceptions
from app.modules.fpa import simulator


def _policy(session: Session, key: str) -> RiskPolicy | None:
    return session.scalars(select(RiskPolicy).where(RiskPolicy.key == key, RiskPolicy.portfolio_id.is_(None),
                                                    RiskPolicy.active.is_(True))).first()


def record(session: Session, principal: Principal, inputs: simulator.SimInputs, outputs: dict, *, kind: str,
           scenario: str) -> SimulationRun:
    run = SimulationRun(organization_id=principal.org_id, kind=kind, scenario=scenario,
                        engine_version=simulator.ENGINE_VERSION,
                        inputs={"hash": simulator.inputs_hash(inputs),
                                "portfolios": [p.code for p in inputs.portfolios],
                                "opening_cash": str(inputs.opening_cash), "reserve": str(inputs.reserve)},
                        outputs=outputs, created_by=principal.user_id)
    session.add(run)
    session.flush()
    return run


def evaluate(session: Session, principal: Principal, run: SimulationRun) -> list[dict]:
    """Check the run against org-level risk policies; returns the breaches (cases opened, notifications queued)."""
    c = run.outputs["consolidated"]
    breaches = []
    reserve = _policy(session, "min_cash_reserve")
    threshold = reserve.threshold if reserve else Decimal(c["reserve"])
    if Decimal(c["min_cash"]) < threshold:
        breaches.append({"type": "liquidity_breach", "exposure": threshold - Decimal(c["min_cash"]),
                         "week": c["min_cash_week"], "detail": f"minimum cash {c['min_cash']} below {threshold}"})
    margin = _policy(session, "op_margin_min")
    revenue = Decimal(c["revenue"])
    if margin and revenue > 0 and Decimal(c["operating_profit"]) / revenue < margin.threshold:
        breaches.append({"type": "margin_breach",
                         "exposure": margin.threshold * revenue - Decimal(c["operating_profit"]),
                         "week": None, "detail": f"operating margin below {margin.threshold}"})
    for b in breaches:
        case = exceptions.open_case(session, principal.org_id, b["type"], subject_type="simulation_run",
                                    subject_id=str(run.id), context={"scenario": run.scenario, "week": b["week"],
                                                                     "detail": b["detail"]})
        case.exposure = b["exposure"]
        enqueue(session, principal.org_id, "notify.risk_alert", f"risk:{run.id}:{b['type']}",
                {"run_id": str(run.id), "scenario": run.scenario, "type": b["type"],
                 "exposure": str(b["exposure"]), "case_id": str(case.id)})
    return breaches


def stress_and_alert(session: Session, principal: Principal, inputs: simulator.SimInputs,
                     names: list[str] | None = None) -> dict[str, dict]:
    principal.require("fpa.read")
    out = {}
    for name, outputs in simulator.stress(inputs, names).items():
        run = record(session, principal, inputs, outputs, kind="stress", scenario=name)
        out[name] = {"run_id": str(run.id), "breaches": [{**b, "exposure": str(b["exposure"])}
                                                         for b in evaluate(session, principal, run)],
                     "consolidated": outputs["consolidated"]}
    return out


@handler("notify.risk_alert")
def _notify(session: Session, msg) -> None:
    """Delivery channel (n8n/Slack/WhatsApp to the owner) is configured in M5; the case is the system of record."""
    return None

