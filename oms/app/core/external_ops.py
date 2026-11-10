"""External side effects with provider-specific idempotency (spec §27.6, ADR-015).

Every call that changes something at a provider is recorded in ``external_operations`` and the record is **committed
before the call** (``prepared`` → ``sent``). A crash or timeout after ``sent`` leaves evidence that the effect may
exist; the next attempt then follows the provider policy:

* ``lookup``      — ask the provider whether the effect exists (DOKU status by invoice, Chatwoot by source_id);
                    found → ``confirmed`` without a second call; absent → safe to call again with the same key.
* ``no_lookup``   — the provider cannot tell us (Biteship has no lookup by reference_id, G-BS4; WhatsApp sends; CAPI):
                    the operation becomes ``uncertain`` and an ``external_uncertain`` case is opened. The OMS never
                    re-sends automatically; a human confirms in the provider dashboard.

Exactly one worker executes an operation at a time (remediation A1): a session-level advisory lock tells live
claims from dead ones, the ``prepared → sent`` transition is a single conditional UPDATE that hands out a claim token,
and every later transition is fenced on that token (``external_operation_attempts`` records each attempt).

State changes run in their own short worker transactions (``worker_session`` as the org's system principal), so they
survive the caller's rollback — that is the point of committing before the call.
"""

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.core.db import worker_engine, worker_session
from app.models.base import utcnow
from app.models.governance import ExternalOperation, ExternalOperationAttempt


@dataclass(frozen=True)
class Policy:
    lookup: bool  # provider can confirm whether an effect exists for our key


POLICIES: dict[tuple[str, str], Policy] = {
    ("doku", "create_payment"): Policy(lookup=True),
    ("biteship", "create_order"): Policy(lookup=False),
    ("whatsapp", "send_message"): Policy(lookup=False),
    ("meta_capi", "send_event"): Policy(lookup=False),
    ("chatwoot", "post_message"): Policy(lookup=True),
    # Single-use refresh tokens: a sent-but-unconfirmed rotation can never be retried (the token may be spent).
    **{(p, "token_refresh"): Policy(lookup=False) for p in ("shopee", "tiktokshop", "lazada")},
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
        ).on_conflict_do_nothing(index_elements=["organization_id", "provider", "operation", "idempotency_key"]))
        op = s.scalars(select(ExternalOperation).where(
            ExternalOperation.organization_id == org_id, ExternalOperation.provider == provider,
            ExternalOperation.operation == operation,
            ExternalOperation.idempotency_key == key)).one()
        return _view(op)


class OperationInFlight(Exception):
    """Another live worker holds the claim for this operation; retry later (no provider call was made)."""

    def __init__(self, operation_id: uuid.UUID):
        super().__init__(f"external operation {operation_id} is being executed by another worker")
        self.operation_id = operation_id


def _read(org_id: uuid.UUID, op_id: uuid.UUID) -> OpView:
    with worker_session(org_id) as s:
        return _view(s.get(ExternalOperation, op_id))


@contextmanager
def _liveness_lock(op_id: uuid.UUID) -> Iterator[None]:
    """Session-level advisory lock held for the whole attempt on a dedicated autocommit connection.

    It tells a live claim from a dead one: PostgreSQL releases it when the holder's connection dies (crash, SIGKILL,
    lost container), so a worker that acquires it while the operation is ``sent`` knows the previous claimant is gone.
    A worker that cannot acquire it raises ``OperationInFlight`` and makes no provider call."""
    key = f"extop:{op_id}"
    conn = worker_engine().connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        if not conn.execute(text("select pg_try_advisory_lock(hashtextextended(:k, 0))"), {"k": key}).scalar():
            raise OperationInFlight(op_id)
        try:
            yield
        finally:
            try:
                conn.execute(text("select pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": key})
            except Exception:  # noqa: BLE001 - never return a pooled connection that may still hold the lock
                conn.invalidate()
                raise
    finally:
        conn.close()


def _new_attempt(s, org_id, op_id, attempt_no: int, token: uuid.UUID, kind: str) -> None:
    s.add(ExternalOperationAttempt(id=uuid.uuid4(), organization_id=org_id, operation_id=op_id,
                                   attempt_no=attempt_no, claim_token=token, kind=kind))


def _close_attempt(s, token: uuid.UUID, outcome: str, error: str | None = None) -> None:
    s.execute(update(ExternalOperationAttempt).where(ExternalOperationAttempt.claim_token == token).values(
        outcome=outcome, finished_at=utcnow(), error=(error or None) and error[:2000]))


def claim(org_id: uuid.UUID, op_id: uuid.UUID) -> uuid.UUID | None:
    """Atomically move ``prepared → sent`` and take ownership (committed before any provider call). Returns the claim
    token, or ``None`` if the operation was not ``prepared`` (another worker owns or finished it)."""
    token = uuid.uuid4()
    with worker_session(org_id) as s:
        n = s.execute(update(ExternalOperation).where(
            ExternalOperation.id == op_id, ExternalOperation.status == "prepared").values(
            status="sent", claim_token=token, claimed_at=utcnow(), sent_at=utcnow(),
            attempts=ExternalOperation.attempts + 1).returning(ExternalOperation.attempts)).scalar()
        if n is None:
            return None
        _new_attempt(s, org_id, op_id, n, token, "call")
    return token


def takeover(org_id: uuid.UUID, op_id: uuid.UUID) -> uuid.UUID | None:
    """Re-claim a ``sent`` operation whose claimant is dead (caller holds the liveness lock). No provider call is
    implied: the new owner may only look the effect up or declare it uncertain."""
    token = uuid.uuid4()
    with worker_session(org_id) as s:
        n = s.execute(update(ExternalOperation).where(
            ExternalOperation.id == op_id, ExternalOperation.status == "sent").values(
            claim_token=token, claimed_at=utcnow()).returning(ExternalOperation.attempts)).scalar()
        if n is None:
            return None
        _new_attempt(s, org_id, op_id, n, token, "takeover")
    return token


def finish(org_id: uuid.UUID, op_id: uuid.UUID, token: uuid.UUID, *, status: str, outcome: str | None = None,
           **fields) -> bool:
    """Fenced transition out of ``sent``: applied only while ``token`` still owns the claim. A superseded worker gets
    ``False`` and its attempt is recorded as ``fenced``; it never overwrites its successor's outcome."""
    with worker_session(org_id) as s:
        done = s.execute(update(ExternalOperation).where(
            ExternalOperation.id == op_id, ExternalOperation.claim_token == token,
            ExternalOperation.status == "sent").values(status=status, **fields)
            .returning(ExternalOperation.id)).scalar()
        _close_attempt(s, token, (outcome or status) if done else "fenced", fields.get("last_error"))
        return done is not None


def _uncertain(org_id: uuid.UUID, op_id: uuid.UUID, reason: str, *, token: uuid.UUID, order_id=None,
               portfolio_id=None, exposure=None) -> bool:
    """Fenced ``sent → uncertain`` plus an ``external_uncertain`` case (never clobbers a confirmed outcome)."""
    from app.modules import exceptions

    with worker_session(org_id) as s:
        op = s.execute(update(ExternalOperation).where(
            ExternalOperation.id == op_id, ExternalOperation.claim_token == token,
            ExternalOperation.status == "sent").values(status="uncertain", last_error=reason[:2000])
            .returning(ExternalOperation)).scalar()
        _close_attempt(s, token, "uncertain" if op else "fenced", reason)
        if op is None:
            return False
        case = exceptions.open_case(s, org_id, "external_uncertain", subject_type="external_operation",
                                    subject_id=str(op.id), order_id=order_id, portfolio_id=portfolio_id,
                                    context={"provider": op.provider, "operation": op.operation,
                                             "idempotency_key": op.idempotency_key, "reason": reason[:500]})
        if exposure is not None:
            case.exposure = exposure
        return True


def _terminal(op: OpView, provider: str, operation: str, key: str) -> dict | None:
    if op.status == "confirmed":
        return op.response
    if op.status == "failed":
        raise Rejected(f"{provider}.{operation} {key} was rejected earlier")
    if op.status == "uncertain":
        raise OperationUncertain(op.id, "awaiting human confirmation")
    return None


def execute(org_id: uuid.UUID, provider: str, operation: str, key: str, call: Callable[[], dict],
            *, lookup: Callable[[], dict | None] | None = None, summary: dict | None = None,
            platform_account_id: uuid.UUID | None = None, subject_type: str | None = None,
            subject_id: str | None = None, order_id=None, portfolio_id=None, exposure=None) -> dict:
    """Perform ``call`` at most once per (provider, operation, key) as far as the provider policy allows.

    Concurrency: exactly one worker can hold an operation (liveness lock + atomic ``prepared → sent`` claim);
    others get ``OperationInFlight`` without calling the provider. A ``sent`` operation found by a lock holder
    belongs to a dead worker: it is taken over and resolved by lookup, or declared uncertain (no automatic resend).

    ``call`` and ``lookup`` return a response dict with at least ``ref`` (the provider's id). Returns the confirmed
    response (possibly stored from an earlier attempt). Raises ``OperationUncertain``, ``OperationInFlight`` or
    ``Rejected``."""
    policy = POLICIES[(provider, operation)]
    op = prepare(org_id, provider, operation, key, summary=summary, platform_account_id=platform_account_id,
                 subject_type=subject_type, subject_id=subject_id)
    if (done := _terminal(op, provider, operation, key)) is not None:
        return done
    with _liveness_lock(op.id):
        op = _read(org_id, op.id)  # authoritative state now that no live worker can change it underneath us
        if (done := _terminal(op, provider, operation, key)) is not None:
            return done
        if op.status == "sent":  # the previous claimant died after (possibly) reaching the provider
            token = takeover(org_id, op.id)
            if token is None:
                raise OperationInFlight(op.id)
            if not (policy.lookup and lookup):
                _uncertain(org_id, op.id, "previous attempt sent without confirmation; provider has no lookup",
                           token=token, order_id=order_id, portfolio_id=portfolio_id, exposure=exposure)
                raise OperationUncertain(op.id, "no lookup")
            found = lookup()
            if found:
                if not finish(org_id, op.id, token, status="confirmed", outcome="lookup_confirmed",
                              provider_ref=str(found.get("ref")), response=found, confirmed_at=utcnow()):
                    raise OperationInFlight(op.id)
                return found
            # Provider confirms absence: calling again with the same key is safe.
            if not finish(org_id, op.id, token, status="prepared", outcome="lookup_absent"):
                raise OperationInFlight(op.id)
        token = claim(org_id, op.id)
        if token is None:
            raise OperationInFlight(op.id)
        try:
            resp: dict[str, Any] = call()
        except NotSent as exc:
            finish(org_id, op.id, token, status="prepared", outcome="not_sent", last_error=str(exc)[:2000])
            raise
        except Rejected as exc:
            finish(org_id, op.id, token, status="failed", last_error=str(exc)[:2000])
            raise
        except Exception as exc:
            # Timeout, 5xx, connection reset after send: the effect may exist. The row stays ``sent``; the next
            # attempt takes the lookup / uncertain branch above.
            with worker_session(org_id) as s:
                _close_attempt(s, token, "error", f"{type(exc).__name__}: {exc}")
            raise
        if not finish(org_id, op.id, token, status="confirmed", provider_ref=str(resp.get("ref")), response=resp,
                      confirmed_at=utcnow()):
            # Our claim was superseded while we were calling (lost lock connection): the successor decides.
            raise OperationUncertain(op.id, "claim superseded while the provider call was in progress")
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
    op.claim_token = None
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
