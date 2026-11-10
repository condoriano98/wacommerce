"""Background worker: transactional outbox executed by Procrastinate (ADR-003 revised, plan D11).

* Business code writes ``outbox_messages`` in its own transaction (atomic with the state change).
* ``outbox.relay`` (periodic, every 5 s) defers one ``outbox.process`` job per due message. ``queueing_lock`` keeps at
  most one waiting job per message and ``lock`` serialises execution per message; the task body re-checks the
  message state under ``FOR UPDATE SKIP LOCKED``, so duplicate jobs are harmless.
* Handler failures are recorded on the outbox row (attempts, exponential backoff, dead letter after
  ``outbox_max_attempts``). Procrastinate retries cover infrastructure failures (DB unavailable) only; a job that
  exhausts them leaves the message pending and the next relay tick re-defers it.
* ``jobs.retry_stalled`` (periodic, every minute) returns jobs of workers whose heartbeat is older than
  ``STALLED_AFTER_SECONDS`` to the queue (worker crash / kill). Handlers must therefore be idempotent, and handlers
  with external side effects go through ``external_operations`` (ADR-015).

The queue lives in schema ``procrastinate`` (migration 0008), reachable only by ``oms_worker``.
Run: ``python -m app.worker`` (or ``procrastinate --app=app.worker.procrastinate_app worker``).
"""

import asyncio
import logging
import uuid

from procrastinate import App, PsycopgConnector, RetryStrategy
from procrastinate.exceptions import AlreadyEnqueued
from sqlalchemy.engine import make_url

from app.core import outbox
from app.core.config import get_settings

log = logging.getLogger(__name__)

QUEUES = ["outbox", "maintenance"]
STALLED_AFTER_SECONDS = 60  # 6 missed heartbeats at the default 10 s interval
RELAY_BATCH = 200


def _conninfo() -> str:
    s = get_settings()
    url = s.system_database_url if s.db_role_mode == "legacy" else s.worker_database_url
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def build_app() -> App:
    connector = PsycopgConnector(conninfo=_conninfo(), kwargs={"options": "-c search_path=procrastinate"})
    return App(connector=connector, import_paths=["app.worker"])


procrastinate_app = build_app()


@procrastinate_app.task(name="outbox.process", queue="outbox",
                        retry=RetryStrategy(max_attempts=5, exponential_wait=2))
def process_outbox(message_id: str, org_id: str) -> bool:
    import app.registry  # noqa: F401  (registers outbox handlers)

    return outbox.process_message(uuid.UUID(message_id), uuid.UUID(org_id))


async def relay(app: App = procrastinate_app, limit: int = RELAY_BATCH) -> int:
    """Defer one job per due outbox message; returns the number of newly deferred jobs."""
    due = await asyncio.to_thread(outbox.due_messages, limit)
    deferred = 0
    for message_id, org_id in due:
        key = f"outbox:{message_id}"
        try:
            await app.tasks["outbox.process"].configure(queueing_lock=key, lock=key).defer_async(
                message_id=str(message_id), org_id=str(org_id))
            deferred += 1
        except AlreadyEnqueued:
            pass
    return deferred


@procrastinate_app.periodic(cron="*/5 * * * * *", periodic_id="outbox-relay")
@procrastinate_app.task(name="outbox.relay", queue="maintenance", pass_context=True)
async def relay_outbox(context, timestamp: int) -> int:
    return await relay(context.app)


async def recover_stalled(app: App = procrastinate_app, seconds_since_heartbeat: float = STALLED_AFTER_SECONDS) -> int:
    """Re-queue jobs held by workers that stopped heart-beating (crash, OOM kill, lost container)."""
    stalled = list(await app.job_manager.get_stalled_jobs(seconds_since_heartbeat=seconds_since_heartbeat))
    for job in stalled:
        log.warning("re-queueing stalled job %s (%s)", job.id, job.task_name)
        await app.job_manager.retry_job(job)
    return len(stalled)


@procrastinate_app.periodic(cron="* * * * *", periodic_id="retry-stalled")
@procrastinate_app.task(name="jobs.retry_stalled", queue="maintenance", pass_context=True)
async def retry_stalled_jobs(context, timestamp: int) -> int:
    return await recover_stalled(context.app)


def _channels(kind: str) -> dict:
    import app.registry  # noqa: F401
    from app.modules import channel_sync

    return channel_sync.run_scheduled(kind)


@procrastinate_app.periodic(cron="*/15 * * * *", periodic_id="channels-orders")
@procrastinate_app.task(name="channels.orders", queue="maintenance", queueing_lock="channels.orders")
def sync_channel_orders(timestamp: int) -> dict:
    return _channels("orders")


@procrastinate_app.periodic(cron="7 */6 * * *", periodic_id="channels-payouts")
@procrastinate_app.task(name="channels.payouts", queue="maintenance", queueing_lock="channels.payouts")
def sync_channel_payouts(timestamp: int) -> dict:
    return _channels("payouts")


@procrastinate_app.periodic(cron="17 3 * * *", periodic_id="channels-insights")
@procrastinate_app.task(name="channels.insights", queue="maintenance", queueing_lock="channels.insights")
def sync_ads_insights(timestamp: int) -> dict:
    return _channels("insights")


@procrastinate_app.periodic(cron="23 * * * *", periodic_id="invariants")
@procrastinate_app.task(name="ops.invariants", queue="maintenance", queueing_lock="ops.invariants")
def check_invariants(timestamp: int) -> dict:
    import app.registry  # noqa: F401
    from app.modules import invariants

    return invariants.run_all()


def main() -> None:
    from app.core.observability import configure_logging

    configure_logging()
    procrastinate_app.run_worker(queues=QUEUES, update_heartbeat_interval=10.0,
                                 stalled_worker_timeout=STALLED_AFTER_SECONDS)


if __name__ == "__main__":
    main()
