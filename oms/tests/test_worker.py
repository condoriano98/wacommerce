"""Procrastinate executor behind the transactional outbox (plan D11, correction 6; G-PROC).

Runs a real Procrastinate worker (``oms_worker`` role, schema ``procrastinate``) against PostgreSQL. The provider
worker-kill test with uncertain external outcomes (#6) belongs to M2 and builds on ``external_operations``."""

import asyncio

from sqlalchemy import text

from app.core.db import tenant_session
from app.core.outbox import HANDLERS, enqueue
from app.worker import QUEUES, procrastinate_app, recover_stalled, relay
from tests.conftest import owner_session

CALLS: list[str] = []


def _record(session, msg):
    CALLS.append(msg.payload["n"])


HANDLERS["test.worker"] = _record


def _jobs(status: str | None = None) -> list:
    sql = "select id, status, args from procrastinate.procrastinate_jobs where task_name = 'outbox.process'"
    with owner_session() as s:
        rows = s.execute(text(sql + (" and status = :st" if status else "")), {"st": status}).all()
    return rows


def _outbox(org_id) -> list:
    with owner_session() as s:
        return s.execute(text("select dedupe_key, status, attempts from outbox_messages where organization_id = :o "
                              "and topic = 'test.worker' order by dedupe_key"), {"o": org_id}).all()


def _run(coro_fn):
    app = procrastinate_app

    async def go():
        async with app.open_async():
            return await coro_fn(app)

    return asyncio.run(go())


async def _drain(app) -> None:
    await app.run_worker_async(queues=QUEUES, wait=False, install_signal_handlers=False, listen_notify=False)


def _cleanup() -> None:
    with owner_session() as s:
        s.execute(text("delete from procrastinate.procrastinate_jobs"))
    CALLS.clear()


def test_relay_defers_once_and_worker_processes_each_message_once(tenant):
    _cleanup()
    with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
        for n in ("a", "b"):
            enqueue(s, tenant.org_id, "test.worker", f"w-{n}-{tenant.org_id}", {"n": n})

    async def scenario(app):
        first = await relay(app)
        second = await relay(app)  # queueing_lock: no second waiting job per message
        await _drain(app)
        third = await relay(app)  # messages are done: nothing left to relay
        return first, second, third

    first, second, third = _run(scenario)
    assert (first, second, third) == (2, 0, 0)
    assert sorted(CALLS) == ["a", "b"]
    assert [r.status for r in _outbox(tenant.org_id)] == ["done", "done"]
    assert len(_jobs("succeeded")) == 2


def test_stalled_worker_job_is_requeued_and_effect_applied_once(tenant):
    """A worker dies mid-job (no heartbeat, job left in 'doing'); recovery re-queues it and a healthy worker finishes it.
    The outbox row was never committed as done, so the handler runs exactly once overall."""
    _cleanup()
    with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
        enqueue(s, tenant.org_id, "test.worker", f"stall-{tenant.org_id}", {"n": "s"})

    _run(relay)
    with owner_session() as s:  # simulate the crashed worker that had fetched the job
        dead = s.execute(text("insert into procrastinate.procrastinate_workers (last_heartbeat) "
                              "values (now() - interval '10 minutes') returning id")).scalar()
        s.execute(text("update procrastinate.procrastinate_jobs set status = 'doing', worker_id = :w "
                       "where task_name = 'outbox.process' and status = 'todo'"), {"w": dead})
    assert len(_jobs("doing")) == 1

    async def scenario(app):
        healthy_first = await recover_stalled(app, seconds_since_heartbeat=3600)  # heartbeat 10 min old: not stalled
        recovered = await recover_stalled(app, seconds_since_heartbeat=60)
        await _drain(app)
        return healthy_first, recovered

    assert _run(scenario) == (0, 1)
    assert CALLS == ["s"]
    assert [r.status for r in _outbox(tenant.org_id)] == ["done"]
    assert len(_jobs("succeeded")) == 1


def test_handler_failure_is_recorded_on_outbox_not_lost(tenant):
    _cleanup()

    def boom(session, msg):
        raise RuntimeError("provider 500")

    HANDLERS["test.worker.fail"] = boom
    with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
        enqueue(s, tenant.org_id, "test.worker.fail", f"fail-{tenant.org_id}", {})

    async def scenario(app):
        n = await relay(app)
        await _drain(app)
        return n, await relay(app)  # backoff: not due again yet

    assert _run(scenario) == (1, 0)
    with owner_session() as s:
        row = s.execute(text("select status, attempts, last_error, next_attempt_at > now() from outbox_messages "
                             "where organization_id = :o and topic = 'test.worker.fail'"), {"o": tenant.org_id}).one()
    assert row[0] == "pending" and row[1] == 1 and "provider 500" in row[2] and row[3] is True


def test_api_role_has_no_access_to_job_queue(tenant):
    import pytest
    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError):
        with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
            s.execute(text("select count(*) from procrastinate.procrastinate_jobs"))
