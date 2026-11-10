"""External side effects with provider-specific idempotency (spec §27.6, ADR-015).

Every call that changes something at a provider is recorded in ``external_operations`` and the record is **committed
before the call** (``prepared`` → ``sent``). A crash or timeout after ``sent`` leaves evidence that the effect may
exist; the next attempt then follows the provider policy:

* ``lookup``      — ask the provider whether the effect exists (DOKU status by invoice, Chatwoot by source_id);
                    found → ``confirmed`` without a second call; absent → safe to call again with the same key.
* ``no_lookup``   — the provider cannot tell us (Biteship has no lookup by reference_id, G-BS4; WhatsApp sends; CAPI):
                    the operation becomes ``uncertain`` and an ``external_uncertain`` case is opened. The OMS never
                    re-sends automatically; a human confirms in the provider dashboard.

State changes run in their own short worker transactions (``worker_session`` as the org's system principal), so they
survive the caller's rollback — that is the point of committing before the call.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.db import worker_session
from app.models.base import utcnow
from app.models.governance import ExternalOperation


@dataclass(frozen=True)
class Policy:
    lookup: bool  # provider can confirm whether an effect exists for our key


POLICIES: dict[tuple[str, str], Policy] = {
    ("doku", "create_payment"): Policy(lookup=True),
    ("biteship", "create_order"): Policy(lookup=False),
    ("whatsapp", "send_message"): Policy(lookup=False),
    ("meta_capi", "send_event"): Policy(lookup=False),
    ("chatwoot", "post_message"): Policy(lookup=True),
}


class NotSent(Exception):
    """Raised by adapters when the request provably never reached the provider (DNS/connect failure, local
    validation). The operation returns to ``prepared`` and may be retried."""


class Rejected(Exception):
    """Definitive provider rejection (4xx with a business error): no effect exists; operation ``failed``."""


class OperationUncertain(Exception):
    def __init__(self, operation_id: uuid.UUID, reason: str):
        super().__init__(f"external operation {operation_id} uncertain: {reason}")
        self.operation_id = operation_id
        self.reason = reason


@dataclass
class OpView:
    id: uuid.UUID
    status: str
    provider_ref: str | None
    response: dict
    attempts: int


def _view(op: ExternalOperation) -> OpView:
    return OpView(op.id, op.status, op.provider_ref, dict(op.response or {}), op.attempts)


def prepare(org_id: uuid.UUID, provider: str, operation: str, key: str, *, summary: dict | None = None,
            platform_account_id: uuid.UUID | None = None, subject_type: str | None = None,
            subject_id: str | None = None) -> OpView:
    with worker_session(org_id) as s:
        s.execute(insert(ExternalOperation).values(
            id=uuid.uuid4(), created_at=utcnow(), organization_id=org_id, platform_account_id=platform_account_id,
            provider=provider, operation=operation, idempotency_key=key, status="prepared",
            request_summary=summary or {}, attempts=0, response={}, subject_type=subject_type, subject_id=subject_id,
        ).on_conflict_do_nothing(index_elements=["provider", "operation", "idempotency_key"]))
        op = s.scalars(select(ExternalOperation).where(
            ExternalOperation.provider == provider, ExternalOperation.operation == operation,
            ExternalOperation.idempotency_key == key)).one()
        return _view(op)


def _update(org_id: uuid.UUID, op_id: uuid.UUID, **fields) -> OpView:
    with worker_session(org_id) as s:
        op = s.get(ExternalOperation, op_id, with_for_update=True)
        for k, v in fields.items():
            setattr(op, k, v)
        return _view(op)


def _uncertain(org_id: uuid.UUID, op_id: uuid.UUID, reason: str, *, order_id=None, portfolio_id=None,
               exposure=None) -> None:
    from app.modules import exceptions

    with worker_session(org_id) as s:
        op = s.get(ExternalOperation, op_id, with_for_update=True)
        op.status, op.last_error = "uncertain", reason[:2000]
        case = exceptions.open_case(s, org_id, "external_uncertain", subject_type="external_operation",
                                    subject_id=str(op.id), order_id=order_id, portfolio_id=portfolio_id,
                                    context={"provider": op.provider, "operation": op.operation,
                                             "idempotency_key": op.idempotency_key, "reason": reason[:500]})
        if exposure is not None:
            case.exposure = exposure


def execute(org_id: uuid.UUID, provider: str, operation: str, key: str, call: Callable[[], dict],
            *, lookup: Callable[[], dict | None] | None = None, summary: dict | None = None,
            platform_account_id: uuid.UUID | None = None, subject_type: str | None = None,
            subject_id: str | None = None, order_id=None, portfolio_id=None, exposure=None) -> dict:
    """Perform ``call`` at most once per (provider, operation, key) as far as the provider policy allows.

    ``call`` and ``lookup`` return a response dict with at least ``ref`` (the provider's id). Returns the confirmed
    response (possibly stored from an earlier attempt). Raises ``OperationUncertain`` or ``Rejected``."""
    policy = POLICIES[(provider, operation)]
    op = prepare(org_id, provider, operation, key, summary=summary, platform_account_id=platform_account_id,
                 subject_type=subject_type, subject_id=subject_id)
    if op.status == "confirmed":
        return op.response
    if op.status == "failed":
        raise Rejected(f"{provider}.{operation} {key} was rejected earlier")
    if op.status == "uncertain":
        raise OperationUncertain(op.id, "awaiting human confirmation")
    if op.status == "sent":  # a previous attempt may have produced the effect
        if not (policy.lookup and lookup):
            _uncertain(org_id, op.id, "previous attempt sent without confirmation; provider has no lookup",
                       order_id=order_id, portfolio_id=portfolio_id, exposure=exposure)
            raise OperationUncertain(op.id, "no lookup")
        found = lookup()
        if found:
            _update(org_id, op.id, status="confirmed", provider_ref=str(found.get("ref")), response=found,
                    confirmed_at=utcnow())
            return found
        # Provider confirms absence: calling again with the same key is safe.
    _update(org_id, op.id, status="sent", sent_at=utcnow(), attempts=op.attempts + 1)
    try:
        resp: dict[str, Any] = call()
    except NotSent as exc:
        _update(org_id, op.id, status="prepared", last_error=str(exc)[:2000])
        raise
    except Rejected as exc:
        _update(org_id, op.id, status="failed", last_error=str(exc)[:2000])
        raise
    # Any other exception (timeout, 5xx, connection reset after send) propagates with the row left at "sent";
    # the retry takes the lookup / uncertain branch above.
    _update(org_id, op.id, status="confirmed", provider_ref=str(resp.get("ref")), response=resp,
            confirmed_at=utcnow())
    return resp


def resolve_uncertain(session, principal, op_id: uuid.UUID, *, effect_exists: bool, provider_ref: str | None,
                      response: dict | None = None) -> ExternalOperation:
    """Human resolution after checking the provider dashboard (audited). ``effect_exists=False`` returns the
    operation to ``prepared`` so the job may run once more with the same key."""
    from app.core.audit import audit

    principal.require("exceptions.manage")
    op = session.get(ExternalOperation, op_id, with_for_update=True)
    if op is None or op.status != "uncertain":
        raise ValueError("operation is not uncertain")
    if effect_exists:
        op.status, op.provider_ref, op.response, op.confirmed_at = "confirmed", provider_ref, response or {
            "ref": provider_ref}, utcnow()
    else:
        op.status = "prepared"
    audit(session, principal.org_id, "external_operation.resolved", "external_operation", op.id,
          actor_user_id=principal.user_id, after={"effect_exists": effect_exists, "provider_ref": provider_ref})
    resume = RESUME.get((op.provider, op.operation))
    if resume and op.subject_id:  # re-run the waiting job: it now finds a confirmed op (or may call again)
        from app.core.outbox import enqueue

        topic, payload = resume(op.subject_id)
        enqueue(session, op.organization_id, topic, f"resume:{op.id}:{utcnow():%Y%m%d%H%M%S%f}", payload)
    return op


# (provider, operation) -> job that consumes the operation's result, given the operation's subject id
RESUME: dict[tuple[str, str], Callable[[str], tuple[str, dict]]] = {
    ("biteship", "create_order"): lambda sid: ("biteship.create_order",
                                               {"group_id": sid, "courier_company": "", "courier_type": ""}),
    ("doku", "create_payment"): lambda sid: ("doku.create_payment", {"payment_id": sid}),
}
