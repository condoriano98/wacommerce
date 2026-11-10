"""Transactional outbox (ADR-003).

``enqueue`` writes a message in the caller's transaction; ``dispatch_due`` (worker) claims due messages
with ``FOR UPDATE SKIP LOCKED``, runs the registered handler inside the message's organization and
retries with exponential backoff. Handlers must be idempotent; ``dedupe_key`` prevents duplicates.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.base import utcnow
from app.models.governance import OutboxMessage

log = logging.getLogger(__name__)

Handler = Callable[[Session, OutboxMessage], None]
HANDLERS: dict[str, Handler] = {}


def handler(topic: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[topic] = fn
        return fn

    return register


def enqueue(
    session: Session,
    org_id: uuid.UUID,
    topic: str,
    dedupe_key: str,
    payload: dict,
    *,
    delay: timedelta | None = None,
) -> None:
    import json

    from sqlalchemy import text

    if org_id is None:
        raise ValueError("org_id required")
    session.execute(text("select enqueue_outbox(:t, :k, cast(:p as jsonb), :n)"),
                    {"t": topic, "k": dedupe_key, "p": json.dumps(payload),
                     "n": utcnow() + (delay or timedelta(0))})


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, 15 * (2 ** max(0, attempts - 1))))


def process_one(session: Session, msg: OutboxMessage) -> None:
    """Run one handler attempt inside the caller's transaction (already scoped to the message's org)."""
    fn = HANDLERS.get(msg.topic)
    msg.attempts += 1
    try:
        if fn is None:
            raise RuntimeError(f"no handler for topic {msg.topic}")
        with session.begin_nested():
            fn(session, msg)
        msg.status, msg.processed_at, msg.last_error = "done", utcnow(), None
    except Exception as exc:  # noqa: BLE001 - recorded, retried, dead-lettered
        log.warning("outbox %s failed: %s", msg.topic, exc)
        msg.last_error = str(exc)[:2000]
        if msg.attempts >= get_settings().outbox_max_attempts:
            msg.status = "dead"
            from app.models.governance import DeadLetter

            session.add(DeadLetter(organization_id=msg.organization_id, outbox_message_id=msg.id, topic=msg.topic,
                                   last_error=msg.last_error, attempts=msg.attempts))
        else:
            msg.next_attempt_at = utcnow() + _backoff(msg.attempts)


def dispatch_due(limit: int = 50, now: datetime | None = None) -> int:
    """Fallback/synchronous dispatcher (tests, CLI). Claims with FOR UPDATE SKIP LOCKED through a definer function and
    runs each message as its organization's system principal — no BYPASSRLS (ADR-007). The Procrastinate worker
    (app/worker.py) calls the same ``process_one``."""
    from sqlalchemy import text

    from app.core.db import act_as_system, worker_session

    processed = 0
    for _ in range(limit):
        with worker_session() as session:
            row = session.execute(text("select * from claim_due_outbox(1, :now)"),
                                  {"now": now or utcnow()}).first()
            if row is None:
                break
            act_as_system(session, row.organization_id)
            msg = session.get(OutboxMessage, row.id)
            process_one(session, msg)
            processed += 1
    return processed


def requeue_dead_letter(session: Session, dead_letter_id, reviewer_id) -> None:
    from app.models.governance import DeadLetter

    dl = session.get(DeadLetter, dead_letter_id, with_for_update=True)
    msg = session.get(OutboxMessage, dl.outbox_message_id, with_for_update=True)
    msg.status, msg.attempts, msg.next_attempt_at = "pending", 0, utcnow()
    dl.reviewed_by, dl.resolution, dl.resolved_at = reviewer_id, "requeued", utcnow()
