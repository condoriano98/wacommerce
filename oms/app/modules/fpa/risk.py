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


RETRY_MINUTES = (1, 5, 15, 60)


@handler("notify.risk_alert")
def _notify(session: Session, msg) -> None:
    """Deliver a risk alert on every configured channel (remediation E2).

    The exception case is the system of record and is never modified or closed by delivery problems. Each
    (case, channel) delivery keeps its status, attempts, last error and the request correlation id. A failed send is
    recorded and retried by a delayed outbox message (the failure is *persisted*, not rolled back with the handler);
    after ``notify_max_attempts`` the delivery is ``dead``: a dead letter is raised for review, an escalation event is
    appended to the case and the case is flagged for a human."""
    import uuid as _uuid
    from datetime import timedelta

    from sqlalchemy.dialects.postgresql import insert

    from app.core.audit import audit
    from app.core.config import get_settings
    from app.core.observability import correlation_id
    from app.integrations.notify import channel_for
    from app.models.base import utcnow
    from app.models.governance import DeadLetter, ExceptionCase, ExceptionEvent, NotificationDelivery

    payload = msg.payload or {}
    case = session.get(ExceptionCase, _uuid.UUID(payload["case_id"]))
    if case is None:
        return
    settings = get_settings()
    attempt = int(payload.get("attempt", 1))
    retry: list[str] = []
    for name in [c.strip() for c in settings.risk_alert_channels.split(",") if c.strip()]:
        session.execute(insert(NotificationDelivery).values(
            id=_uuid.uuid4(), created_at=utcnow(), organization_id=case.organization_id, case_id=case.id,
            channel=name, recipient_role=case.owner_role or "owner", status="pending", attempts=0)
            .on_conflict_do_nothing(index_elements=["case_id", "channel"]))
        d = session.scalars(select(NotificationDelivery).where(
            NotificationDelivery.case_id == case.id, NotificationDelivery.channel == name).with_for_update()).one()
        if d.status == "sent":
            continue
        d.attempts += 1
        d.correlation_id = correlation_id.get() or d.correlation_id
        try:
            resp = channel_for(name).send({"case_id": str(case.id), "type": payload.get("type"),
                                           "scenario": payload.get("scenario"), "exposure": payload.get("exposure"),
                                           "correlation_id": d.correlation_id})
        except Exception as exc:  # noqa: BLE001 - recorded, retried, dead-lettered; never touches the case
            d.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            if d.attempts >= settings.notify_max_attempts:
                d.status = "dead"
                if session.scalars(select(DeadLetter).where(DeadLetter.outbox_message_id == msg.id)).first() is None:
                    session.add(DeadLetter(organization_id=case.organization_id, outbox_message_id=msg.id,
                                           topic=msg.topic, last_error=d.last_error, attempts=d.attempts))
                session.add(ExceptionEvent(organization_id=case.organization_id, case_id=case.id,
                                           portfolio_id=case.portfolio_id, event="notification_failed",
                                           detail={"channel": name, "attempts": d.attempts,
                                                   "error": d.last_error[:300], "escalation": "human_follow_up"}))
                case.context = {**(case.context or {}), "notification_escalated": True}
            else:
                d.status = "failed"
                retry.append(name)
            audit(session, case.organization_id, "notification.failed", "exception_case", case.id,
                  actor_type="system", after={"channel": name, "attempts": d.attempts, "status": d.status})
            continue
        d.status, d.provider_ref, d.sent_at, d.last_error = "sent", str(resp.get("ref")), utcnow(), None
        audit(session, case.organization_id, "notification.sent", "exception_case", case.id,
              actor_type="system", after={"channel": name, "attempts": d.attempts, "provider_ref": d.provider_ref})
    if retry:
        delay = timedelta(minutes=RETRY_MINUTES[min(attempt - 1, len(RETRY_MINUTES) - 1)])
        enqueue(session, case.organization_id, "notify.risk_alert", f"{msg.dedupe_key}:retry{attempt}",
                {**payload, "attempt": attempt + 1}, delay=delay)
