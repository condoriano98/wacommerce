"""Round 2 / P0-2 — a manual transfer becomes cash only against bank evidence of the order's own legal entity.

Every test runs the real services against PostgreSQL; the concurrency test uses separate connections released by a
barrier. A bank line is matched at most once, ambiguous candidates are never auto-matched.
"""

import uuid
from decimal import Decimal as D

from sqlalchemy import func, select, text

import app.registry  # noqa: F401
from app.core.db import tenant_session
from app.models.finance import BankStatementLine, LedgerEntry
from app.models.orders import Order, Payment, PaymentEvent
from app.models.org import LegalEntity
from app.modules import payments
from tests.conftest import make_tenant, owner_session
from tests.test_accounting_services import _order
from tests.test_remediation_concurrency import _run_parallel
from tests.test_v31_correctness_remediation import _bank
from tests.world import build_world, principal


def _attested(t, w, *, bank_reference="BCA-777") -> tuple[uuid.UUID, str, D]:
    oid = _order(t, w, [("A1", 1, "0")], method="manual_transfer")
    ai, fin = principal(t, "ai"), principal(t, "finance")
    with tenant_session(t.org_id, ai.user_id) as s:
        pid = payments.report_manual_transfer(s, s.get(Order, oid), {"receipt": "img://r"}).id
    with tenant_session(t.org_id, fin.user_id) as s:
        payments.attest(s, fin, pid, bank_reference=bank_reference, amount_seen=s.get(Payment, pid).amount)
        o = s.get(Order, oid)
        return pid, o.order_number, o.total


def _import(t, bank_id, amount, reference, batch=None) -> int:
    fin = principal(t, "finance")
    with tenant_session(t.org_id, fin.user_id) as s:
        return payments.import_bank_statement(s, fin, bank_id, [{"booked_at": "2026-11-10", "amount": amount,
                                                                 "reference": reference}],
                                              batch=batch or uuid.uuid4().hex[:8])


def _match(t) -> int:
    fin = principal(t, "finance")
    with tenant_session(t.org_id, fin.user_id) as s:
        return payments.match_bank_lines(s, fin)


def _cash_journals(t, pid) -> int:
    with owner_session() as s:
        return s.scalar(select(func.count(func.distinct(LedgerEntry.journal_id))).where(
            LedgerEntry.organization_id == t.org_id, LedgerEntry.source_type == "payment_bank_matched",
            LedgerEntry.source_id == str(pid)))


def _status(pid) -> str:
    with owner_session() as s:
        return s.get(Payment, pid).status


def _second_entity(t) -> uuid.UUID:
    with owner_session() as s:
        le = LegalEntity(organization_id=t.org_id, name="PT Second")
        s.add(le)
        s.flush()
        return le.id


def test_bank_line_of_another_legal_entity_in_the_same_organization_never_matches(tenant):
    w = build_world(tenant)
    pid, number, total = _attested(tenant, w)
    other_bank = _bank(tenant, _second_entity(tenant))
    assert _import(tenant, other_bank, total, f"TRF {number}") == 1
    assert _match(tenant) == 0
    assert _status(pid) == "attested" and _cash_journals(tenant, pid) == 0


def test_bank_line_of_another_organization_never_matches(tenant):
    w = build_world(tenant)
    pid, number, total = _attested(tenant, w)
    other = make_tenant()
    assert _import(other, _bank(other, other.legal_entity_id), total, f"TRF {number}") == 1
    assert _match(other) == 0
    assert _status(pid) == "attested" and _cash_journals(tenant, pid) == 0


def test_bank_line_of_the_orders_entity_with_reference_and_amount_matches_once(tenant):
    w = build_world(tenant)
    pid, number, total = _attested(tenant, w)
    own = _bank(tenant, tenant.legal_entity_id)
    assert _import(tenant, own, total, f"TRF {number}", batch="b1") == 1
    assert _match(tenant) == 1
    assert _status(pid) == "bank_matched" and _cash_journals(tenant, pid) == 1
    # Re-importing the same statement adds no line and no cash journal.
    assert _import(tenant, own, total, f"TRF {number}", batch="b1") == 0
    assert _match(tenant) == 0 and _cash_journals(tenant, pid) == 1


def test_several_candidate_payments_open_a_case_and_match_nothing(tenant):
    w = build_world(tenant)
    p1, _, total = _attested(tenant, w, bank_reference="BCA-SAME")
    p2, _, total2 = _attested(tenant, w, bank_reference="BCA-SAME")
    assert total == total2
    assert _import(tenant, _bank(tenant, tenant.legal_entity_id), total, "TRF BCA-SAME") == 1
    assert _match(tenant) == 0
    assert (_status(p1), _status(p2)) == ("attested", "attested")
    with owner_session() as s:
        assert s.execute(text("select count(*) from exception_cases where organization_id = :o and type = "
                              "'payment_mismatch' and subject_type = 'bank_statement_line'"),
                         {"o": tenant.org_id}).scalar() == 1


def test_short_reference_never_matches_a_longer_one(tenant):
    w = build_world(tenant)
    pid, number, total = _attested(tenant, w, bank_reference="BCA-77")
    # "BCA-77" must not be found inside "BCA-777" (and the order number is absent).
    assert _import(tenant, _bank(tenant, tenant.legal_entity_id), total, "TRF BCA-777") == 1
    assert _match(tenant) == 0 and _status(pid) == "attested"


def test_concurrent_matching_recognises_cash_once(tenant):
    w = build_world(tenant)
    pid, number, total = _attested(tenant, w)
    assert _import(tenant, _bank(tenant, tenant.legal_entity_id), total, f"TRF {number}") == 1
    results, errors = _run_parallel([lambda: _match(tenant)] * 3)
    assert [e for e in errors if e is not None] == []
    assert sum(results) == 1
    assert _cash_journals(tenant, pid) == 1
    with owner_session() as s:
        assert s.scalar(select(func.count()).select_from(PaymentEvent).where(
            PaymentEvent.payment_id == pid, PaymentEvent.to_status == "bank_matched")) == 1
        assert s.scalar(select(func.count()).select_from(BankStatementLine).where(
            BankStatementLine.organization_id == tenant.org_id, BankStatementLine.matched_id.is_not(None))) == 1
