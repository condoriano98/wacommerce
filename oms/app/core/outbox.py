"""Transactional outbox (ADR-003).

``enqueue`` writes a message in the caller's transaction; ``dispatch_due`` (worker) claims due messages
with ``FOR UPDATE SKIP LOCKED``, runs the registered handler inside the message's organization and
retries with exponential backoff. Handlers must be idempotent; ``dedupe_key`` prevents duplicates.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import set_tenant, system_session
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
    stmt = (
        insert(OutboxMessage)
        .values(
            id=uuid.uuid4(),
            created_at=utcnow(),
            organization_id=org_id,
            topic=topic,
            dedupe_key=dedupe_key,
            payload=payload,
            status="pending",
            attempts=0,
            next_attempt_at=utcnow() + (delay or timedelta(0)),
        )
        .on_conflict_do_nothing(index_elements=["organization_id", "dedupe_key"])
    )
    session.execute(stmt)


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, 15 * (2 ** max(0, attempts - 1))))


def dispatch_due(limit: int = 50, now: datetime | None = None) -> int:
    """Process due messages; returns the number handled. Each message runs in its own transaction."""
    processed = 0
    max_attempts = get_settings().outbox_max_attempts
    for _ in range(limit):
        with system_session() as session:
            msg = session.scalars(
                select(OutboxMessage)
                .where(OutboxMessage.status == "pending", OutboxMessage.next_attempt_at <= (now or utcnow()))
                .order_by(OutboxMessage.next_attempt_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if msg is None:
                break
            set_tenant(session, msg.organization_id)
            fn = HANDLERS.get(msg.topic)
            msg.attempts += 1
            try:
                if fn is None:
                    raise RuntimeError(f"no handler for topic {msg.topic}")
                with session.begin_nested():
                    fn(session, msg)
                msg.status = "done"
                msg.processed_at = utcnow()
                msg.last_error = None
            except Exception as exc:  # noqa: BLE001 - recorded and retried
                log.warning("outbox %s failed: %s", msg.topic, exc)
                msg.last_error = str(exc)[:2000]
                if msg.attempts >= max_attempts:
                    msg.status = "dead"
                else:
                    msg.next_attempt_at = utcnow() + _backoff(msg.attempts)
            processed += 1
    return processed
