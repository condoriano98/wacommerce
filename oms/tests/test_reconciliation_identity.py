"""Round 2 / P0-1 — reconciliation and external references are tenant- and account-scoped.

The same provider reference (a payout id like ``PO-001``, a notification id, an idempotency key) may legitimately exist
in two organizations or on two provider accounts of one organization. Neither may suppress, satisfy or settle the other.
"""

import uuid
from decimal import Decimal as D

from sqlalchemy import select, text

import app.registry  # noqa: F401
from app.core import external_ops
from app.core.db import act_as_system, tenant_session, webhook_session
from app.core.outbox import dispatch_due
from app.models.orders import Payment
from app.modules import payments, platforms, settlements
from tests.conftest import make_tenant, owner_session
from tests.test_accounting_services import _order, _pay_gateway
from tests.test_v31_correctness_remediation import _bank, _gl, _import_and_match
from tests.world import build_world, principal


def _paid_order(t, w) -> tuple[uuid.UUID, D]:
    oid = _order(t, w, [("A1", 1, "0")])
    _pay_gateway(t, oid)
    dispatch_due()
    with owner_session() as s:
        return s.execute(select(Payment.id, Payment.amount).where(Payment.order_id == oid)).one()


def _disburse(t, account_id, payout_id: str, pid, amount) -> None:
    fin = principal(t, "finance")
    with tenant_session(t.org_id, fin.user_id) as s:
        settlements.gateway_disbursement(s, fin, account_id, payout_id, [(pid, amount)])


def _payout_items(t) -> list:
    with owner_session() as s:
        return s.execute(text("select expected_ref, status, platform_account_id from reconciliation_items "
                              "where organization_id = :o and kind = 'bank_payout' order by created_at"),
                         {"o": t.org_id}).all()


def test_two_organizations_with_the_same_payout_reference_reconcile_independently():
    tenants = [make_tenant(), make_tenant()]
    worlds = [build_world(t) for t in tenants]
    paid = [_paid_order(t, w) for t, w in zip(tenants, worlds, strict=True)]
    amount = paid[0][1]
    assert paid[1][1] == amount  # same reference AND same amount in both organizations
    for t, w, (pid, _) in zip(tenants, worlds, paid, strict=True):
        _disburse(t, w.wa_account, "PO-001", pid, amount)
    for t in tenants:  # neither expectation was suppressed by the other organization's
        assert [(r.expected_ref, r.status) for r in _payout_items(t)] == [("gateway:PO-001", "open")]
    for t in tenants:
        assert _import_and_match(t, _bank(t, t.legal_entity_id), amount, "DOKU PO-001", f"b-{t.org_id}") == (1, 1)
        assert [r.status for r in _payout_items(t)] == ["matched"]
        assert _gl("1050", organization_id=t.org_id) == 0
        assert _gl("1000", organization_id=t.org_id) > 0


def test_same_reference_on_two_provider_accounts_of_one_organization_is_never_merged(tenant):
    w = build_world(tenant)
    owner = principal(tenant, "owner")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        second = platforms.register_account(s, owner, provider="doku", provider_account_id="MCH-2",
                                            display_name="DOKU merchant 2", role="payments",
                                            portfolio_ids=[tenant.portfolios["A"]], brand_id=tenant.brand_id)
        second.legal_entity_id = tenant.legal_entity_id
        second_id = second.id
    (p1, a1), (p2, a2) = _paid_order(tenant, w), _paid_order(tenant, w)
    assert a1 == a2
    _disburse(tenant, w.wa_account, "PO-001", p1, a1)
    _disburse(tenant, second_id, "PO-001", p2, a2)
    items = _payout_items(tenant)
    assert len(items) == 2 and {r.platform_account_id for r in items} == {w.wa_account, second_id}
    # One bank line fits both: it is not matched to an arbitrary one (ambiguity → case, no cash).
    assert _import_and_match(tenant, _bank(tenant, tenant.legal_entity_id), a1, "DOKU PO-001", "amb") == (1, 0)
    assert _gl("1050", organization_id=tenant.org_id) == a1 + a2
    with owner_session() as s:
        assert s.execute(text("select count(*) from exception_cases where organization_id = :o and "
                              "type = 'bank_unmatched'"), {"o": tenant.org_id}).scalar() == 1


def test_provider_notification_ids_are_scoped_to_the_organization():
    tenants = [make_tenant(), make_tenant()]
    results = []
    for t in tenants:
        w = build_world(t)
        oid = _order(t, w, [("A1", 1, "0")])
        owner = principal(t, "owner")
        from datetime import timedelta

        from app.models.base import utcnow
        from app.models.orders import Order

        with tenant_session(t.org_id, owner.user_id) as s:
            o = s.get(Order, oid)
            p = payments.create_payment_link(s, o, provider="doku", provider_ref=f"INV-{o.order_number}",
                                             url="https://sandbox/pay", expires_at=utcnow() + timedelta(hours=1))
            ref, amount = p.provider_ref, p.amount
        with webhook_session() as s:
            org = s.execute(text("select organization_id from resolve_payment('doku', :r)"), {"r": ref}).scalar()
            act_as_system(s, org)
            results.append(payments.record_gateway_notification(
                s, provider="doku", notification_id="REQ-SAME-1", provider_ref=ref, verified_status="paid",
                verified_amount=amount, raw={}))
    assert results == ["captured", "captured"]


def test_the_same_idempotency_key_in_two_organizations_executes_in_each():
    a, b = make_tenant(), make_tenant()
    calls = []

    def call(tag):
        def _c():
            calls.append(tag)
            return {"ref": f"REF-{tag}"}
        return _c

    assert external_ops.execute(a.org_id, "biteship", "create_order", "grp-shared-key", call("a"))["ref"] == "REF-a"
    assert external_ops.execute(b.org_id, "biteship", "create_order", "grp-shared-key", call("b"))["ref"] == "REF-b"
    assert calls == ["a", "b"]


# --- Round 2 / P1-4: deterministic provider payout matching ------------------------------------------------------


def test_payout_token_must_match_as_a_whole_reference(tenant):
    w = build_world(tenant)
    pid, amount = _paid_order(tenant, w)
    _disburse(tenant, w.wa_account, "PO-1", pid, amount)
    assert _import_and_match(tenant, _bank(tenant, tenant.legal_entity_id), amount, "DOKU PO-10", "tok") == (1, 0)
    assert [r.status for r in _payout_items(tenant)] == ["open"]


def test_payout_in_another_currency_than_the_bank_entity_is_never_matched(tenant):
    w = build_world(tenant)
    pid, amount = _paid_order(tenant, w)
    _disburse(tenant, w.wa_account, "PO-CUR", pid, amount)
    with owner_session() as s:
        s.execute(text("update legal_entities set base_currency = 'USD' where id = :l"), {"l": tenant.legal_entity_id})
    assert _import_and_match(tenant, _bank(tenant, tenant.legal_entity_id), amount, "DOKU PO-CUR", "cur") == (1, 0)


def test_ambiguous_payout_is_resolved_by_an_operator_with_approval_and_audit(tenant):
    from app.models.finance import BankStatementLine
    from app.modules import approvals

    w = build_world(tenant)
    owner = principal(tenant, "owner")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        second = platforms.register_account(s, owner, provider="doku", provider_account_id="MCH-9",
                                            display_name="DOKU merchant 9", role="payments",
                                            portfolio_ids=[tenant.portfolios["A"]], brand_id=tenant.brand_id)
        second.legal_entity_id = tenant.legal_entity_id
        second_id = second.id
    (p1, a1), (p2, a2) = _paid_order(tenant, w), _paid_order(tenant, w)
    _disburse(tenant, w.wa_account, "PO-AMB", p1, a1)
    _disburse(tenant, second_id, "PO-AMB", p2, a2)
    bank = _bank(tenant, tenant.legal_entity_id)
    assert _import_and_match(tenant, bank, a1, "DOKU PO-AMB", "amb1") == (1, 0)
    with owner_session() as s:
        line_id = s.scalars(select(BankStatementLine.id).where(BankStatementLine.bank_account_id == bank)).one()
        item_id = s.execute(text("select id from reconciliation_items where organization_id = :o and kind = "
                                 "'bank_payout' and platform_account_id = :a"),
                            {"o": tenant.org_id, "a": second_id}).scalar()
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        aid = approvals.request_approval(s, fin, kind="variance_acceptance", subject_type="bank_statement_line",
                                         subject_id=str(line_id), amount=a1, payload={"item": str(item_id)},
                                         portfolio_id=None, idempotency_key=f"res-{line_id}").id
    with tenant_session(tenant.org_id, owner.user_id) as s:
        approvals.decide(s, owner, aid, True, "merchant 9 statement confirms it")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        payments.resolve_bank_match(s, fin, line_id, item_id, aid)
    rows = {r.platform_account_id: r.status for r in _payout_items(tenant)}
    assert rows == {second_id: "matched", w.wa_account: "open"}
    assert _gl("1050", organization_id=tenant.org_id) == a2  # only the resolved payout became verified cash
    with owner_session() as s:
        assert s.execute(text("select count(*) from audit_logs where action = 'bank_match.resolved' and "
                              "entity_id = :l"), {"l": str(line_id)}).scalar() == 1
