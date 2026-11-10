"""Remediation A1/A2 — concurrency regressions with genuinely concurrent transactions on separate connections.

A1: several workers executing the same external operation key must produce exactly one provider-side effect.
A2: concurrent postings of the same business event must produce exactly one journal.
"""

import threading
import time
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

import app.registry  # noqa: F401
from app.core import external_ops
from app.core.db import tenant_session, worker_session
from app.models.finance import LedgerEntry
from app.models.governance import ExternalOperation
from app.modules.finance import accounts as A
from app.modules.finance.ledger import JournalLine, post_journal, reverse_journal
from tests.conftest import owner_session


def _run_parallel(fns):
    barrier = threading.Barrier(len(fns))
    results, errors = [None] * len(fns), [None] * len(fns)

    def run(i, fn):
        barrier.wait()
        try:
            results[i] = fn()
        except Exception as exc:  # noqa: BLE001 - collected and asserted
            errors[i] = exc

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


class SlowProvider:
    """Counts provider-side effects; each call takes long enough for the other workers to race it."""

    def __init__(self, delay: float = 0.4):
        self.calls, self.delay, self.lock = 0, delay, threading.Lock()

    def call(self) -> dict:
        with self.lock:
            self.calls += 1
            n = self.calls
        time.sleep(self.delay)
        return {"ref": f"PROV-{n}"}


def test_A1_concurrent_workers_same_operation_key_cause_exactly_one_provider_effect(tenant):
    provider = SlowProvider()
    key = f"grp-{uuid.uuid4()}"

    def worker():
        return external_ops.execute(tenant.org_id, "biteship", "create_order", key, provider.call)

    results, errors = _run_parallel([worker] * 4)
    assert provider.calls == 1, f"provider called {provider.calls} times"
    confirmed = [r for r in results if r is not None]
    assert confirmed and {r["ref"] for r in confirmed} == {"PROV-1"}
    assert all(e is None or isinstance(e, external_ops.OperationInFlight) for e in errors), errors
    with owner_session() as s:
        op = s.scalars(select(ExternalOperation).where(ExternalOperation.idempotency_key == key)).one()
        assert (op.status, op.provider_ref, op.attempts) == ("confirmed", "PROV-1", 1)
        attempts = s.execute(text("select count(*) from external_operation_attempts where operation_id = :o"),
                             {"o": op.id}).scalar()
        assert attempts == 1
    # A later retry of the same key returns the stored response and never calls again.
    assert external_ops.execute(tenant.org_id, "biteship", "create_order", key, provider.call)["ref"] == "PROV-1"
    assert provider.calls == 1


def _crashed_sent_operation(org_id, provider: str, operation: str) -> tuple[str, uuid.UUID]:
    """An operation whose worker died after claiming (status sent, claim token held by nobody)."""
    key = f"{provider}-{uuid.uuid4()}"
    op = external_ops.prepare(org_id, provider, operation, key)
    with worker_session(org_id) as s:
        s.execute(text("update external_operations set status = 'sent', claim_token = :t, claimed_at = now(), "
                       "attempts = 1 where id = :i"), {"t": uuid.uuid4(), "i": op.id})
    return key, op.id


def test_A1_stale_claim_with_lookup_policy_is_confirmed_by_lookup_without_a_second_call(tenant):
    provider = SlowProvider(delay=0)
    key, _ = _crashed_sent_operation(tenant.org_id, "doku", "create_payment")
    resp = external_ops.execute(tenant.org_id, "doku", "create_payment", key, provider.call,
                                lookup=lambda: {"ref": "FOUND-1"})
    assert resp["ref"] == "FOUND-1" and provider.calls == 0


def test_A1_stale_claim_without_lookup_becomes_uncertain_with_a_case(tenant):
    provider = SlowProvider(delay=0)
    key, op_id = _crashed_sent_operation(tenant.org_id, "biteship", "create_order")
    with pytest.raises(external_ops.OperationUncertain):
        external_ops.execute(tenant.org_id, "biteship", "create_order", key, provider.call)
    assert provider.calls == 0
    with owner_session() as s:
        assert s.get(ExternalOperation, op_id).status == "uncertain"
        assert s.execute(text("select count(*) from exception_cases where subject_id = :i and "
                              "type = 'external_uncertain'"), {"i": str(op_id)}).scalar() == 1


def test_A1_superseded_worker_is_fenced_and_cannot_overwrite_the_operation(tenant):
    key = f"grp-{uuid.uuid4()}"
    op = external_ops.prepare(tenant.org_id, "biteship", "create_order", key)
    first = external_ops.claim(tenant.org_id, op.id)
    assert first is not None
    # The first worker is presumed dead; a second takes over with a new claim token.
    with worker_session(tenant.org_id) as s:
        s.execute(text("update external_operations set claim_token = :t where id = :i"),
                  {"t": uuid.uuid4(), "i": op.id})
    assert external_ops.finish(tenant.org_id, op.id, first, status="confirmed", provider_ref="LATE") is False
    with owner_session() as s:
        row = s.get(ExternalOperation, op.id)
        assert (row.status, row.provider_ref) == ("sent", None)
        assert s.execute(text("select outcome from external_operation_attempts where claim_token = :t"),
                         {"t": first}).scalar() == "fenced"


# --- A2 ---------------------------------------------------------------------------------------------------------


def _journal_count(org_id, source_type, source_id) -> int:
    with owner_session() as s:
        return s.scalar(select(func.count(func.distinct(LedgerEntry.journal_id))).where(
            LedgerEntry.organization_id == org_id, LedgerEntry.source_type == source_type,
            LedgerEntry.source_id == source_id))


def _post_once(t, source_type, source_id, amount=Decimal("1000")):
    from app.modules.finance import posting

    with tenant_session(t.org_id, t.users["owner"]) as s:
        s.execute(text("select pg_sleep(0.2)"))  # widen the check-then-insert window
        return posting._post(s, t.org_id, t.legal_entity_id, source_type, source_id, [
            JournalLine(A.CASH, debit=amount), JournalLine(A.OWNER_CAPITAL, credit=amount)])


def test_A2_concurrent_posting_of_the_same_event_produces_exactly_one_journal(tenant):
    sid = f"evt-{uuid.uuid4()}"
    results, errors = _run_parallel([lambda: _post_once(tenant, "owner_capital", sid)] * 4)
    assert errors == [None] * 4, errors
    assert _journal_count(tenant.org_id, "owner_capital", sid) == 1
    assert sum(1 for r in results if r is not None) == 1


def test_A2_concurrent_direct_post_journal_same_key_is_deduplicated_by_the_database(tenant):
    sid = f"delta-{uuid.uuid4()}:r1"

    def post():
        with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
            s.execute(text("select pg_sleep(0.2)"))
            return post_journal(s, tenant.org_id, source_type="ad_spend_accrued", source_id=sid,
                                legal_entity_id=tenant.legal_entity_id,
                                lines=[JournalLine(A.ADVERTISING, debit=Decimal("10")),
                                       JournalLine(A.ADS_PAYABLES, credit=Decimal("10"))])

    _, errors = _run_parallel([post] * 3)
    assert errors == [None] * 3, errors
    assert _journal_count(tenant.org_id, "ad_spend_accrued", sid) == 1


def test_A2_distinct_events_remain_independently_postable_and_reversal_happens_once(tenant):
    a, b = f"evt-{uuid.uuid4()}", f"evt-{uuid.uuid4()}"
    assert _post_once(tenant, "owner_capital", a) is not None
    assert _post_once(tenant, "owner_capital", b) is not None
    with owner_session() as s:
        jid = s.scalars(select(LedgerEntry.journal_id).where(LedgerEntry.source_id == a)).first()
    with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
        assert reverse_journal(s, tenant.org_id, jid, "first") is not None
    with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
        assert reverse_journal(s, tenant.org_id, jid, "second") is None
    assert _journal_count(tenant.org_id, "reversal", str(jid)) == 1
