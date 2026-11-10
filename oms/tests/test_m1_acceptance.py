"""M1 acceptance tests (spec §20): #1, #2 split/partial orders; #7 tenant isolation across roles; #8, #9 concurrency
with separate DB connections; #11a settlement ageing; #13a period close; #17 consolidation; #20 order slices and
reporting-role identity."""

import threading
from datetime import date, timedelta
from decimal import Decimal

import psycopg
import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

import app.registry  # noqa: F401
from app.core.db import tenant_session, webhook_session, worker_session
from app.core.outbox import dispatch_due
from app.models.base import utcnow
from app.models.finance import LedgerEntry, ReconciliationItem
from app.models.fulfillment import FulfillmentGroup, PurchaseTask
from app.models.governance import ExceptionCase
from app.models.inventory import InventoryBalance
from app.models.orders import Order, OrderItem, Payment
from app.models.org import LegalEntity, RoleAssignment, User
from app.modules import approvals, inventory, payments, procurement, reconciliation, settlements
from app.modules.finance import periods, posting
from app.modules.finance.ledger import JournalLine, post_journal
from tests.conftest import TEST_DB, make_tenant, owner_session
from tests.test_accounting_services import _groups, _order, _pay_gateway, _ship_owned
from tests.world import D, build_world, principal

# --- #1 / #2 ------------------------------------------------------------------------------------------------


def test_1_2_multi_portfolio_split_fulfilment_partial_return_and_refund(tenant):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 2, "0"), ("A2", 1, "0"), ("B1", 1, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    routes = sorted(r for _, r in _groups(tenant, oid))
    assert routes == ["OWNED_STOCK_BITESHIP", "OWNED_STOCK_BITESHIP", "SHOPEE_DROPSHIP"]  # A owned, B owned, A drop
    with owner_session() as s:
        gs = {g.portfolio_id: g for g in s.scalars(select(FulfillmentGroup).where(FulfillmentGroup.order_id == oid))
              if g.route == "OWNED_STOCK_BITESHIP"}
        assert set(gs) == {tenant.portfolios["A"], tenant.portfolios["B"]}
    _ship_owned(tenant, oid)  # both owned groups delivered; dropship still pending
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        assert s.get(Order, oid).status == "partially_delivered"
        items = {i.sku_id: i for i in s.scalars(select(OrderItem).where(OrderItem.order_id == oid))}
        a1, a2, b1 = items[w.sku["A1"].id], items[w.sku["A2"].id], items[w.sku["B1"].id]
        assert (a1.qty_delivered, b1.qty_delivered, a2.qty_delivered) == (2, 1, 0)
        # revenue only for delivered lines; contract liability still holds the undelivered dropship line
        rev = -posting._balance(s, "4000", order_id=oid)
        assert rev == D("280000.00")  # 2 x 100k + 80k
        assert posting._balance(s, "2200", order_id=oid) < 0
    # partial return of one A1 unit + refund
    cs, pm, packer = principal(tenant, "cs"), principal(tenant, "pm_a"), principal(tenant, "packer")
    with tenant_session(tenant.org_id, cs.user_id) as s:
        r = settlements.request_refund(s, cs, oid, a1.id, D("100000"), "one unit returned")
    with tenant_session(tenant.org_id, pm.user_id) as s:
        approvals.decide(s, pm, r.approval_id, True, "ok")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        settlements.approve_refund_obligation(s, fin, r.id)
    with tenant_session(tenant.org_id, packer.user_id) as s:
        settlements.receive_return(s, packer, oid, a1.id, 1, resellable=True, warehouse_id=w.warehouse,
                                   return_shipping_cost=D("0"))
    with tenant_session(tenant.org_id, fin.user_id) as s:
        o = s.get(Order, oid)
        assert o.status == "partially_returned"
        bal = s.scalars(select(InventoryBalance).where(InventoryBalance.sku_id == w.sku["A1"].id)).one()
        assert (bal.on_hand, bal.reserved, bal.in_transit) == (4, 0, 0)  # 5 - 2 shipped + 1 returned
        net_rev = -(posting._balance(s, "4000", order_id=oid) + posting._balance(s, "4900", order_id=oid))
        assert net_rev == D("180000.00")
        # COGS: A1 2 x 60k + B1 40k delivered = 160k, minus one A1 unit restocked (60k) = 100k
        assert posting._balance(s, "5000", order_id=oid) == D("100000.00")
    _invariants(tenant)


def _invariants(t):
    with owner_session() as s:
        assert s.execute(text("select count(*) from (select journal_id from ledger_entries where organization_id=:o "
                              "group by journal_id having sum(debit)<>sum(credit)) x"), {"o": t.org_id}).scalar() == 0
        # stock subledger value == GL inventory + goods in transit (per org)
        stock = s.execute(text("select coalesce(sum((on_hand + in_transit) * avg_unit_cost),0) from inventory_balances "
                               "where organization_id=:o"), {"o": t.org_id}).scalar()
        gl = s.execute(text("select coalesce(sum(debit-credit),0) from ledger_entries where organization_id=:o "
                            "and account_code in ('1200','1250')"), {"o": t.org_id}).scalar()
        assert abs(Decimal(stock) - Decimal(gl)) < D("1.00"), (stock, gl)
        bad = s.execute(text("select count(*) from order_items where organization_id=:o and (qty_procured > "
                             "qty_allocated_dropship or qty_delivered > qty_shipped)"), {"o": t.org_id}).scalar()
        assert bad == 0


# --- #7 ------------------------------------------------------------------------------------------------------


def test_7_isolation_across_app_worker_webhook_roles_and_sql(tenant):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")])
    other = make_tenant()
    # app role, portfolio B user: raw SQL sees no portfolio-A lines, ledger, customers
    with tenant_session(tenant.org_id, tenant.users["pm_b"]) as s:
        for q in ("select count(*) from order_items where order_id = :o",
                  "select count(*) from customers where id = (select customer_id from orders where id = :o)"):
            assert s.execute(text(q), {"o": oid}).scalar() == 0
    # worker role scoped to another org sees nothing of this org
    with worker_session(other.org_id) as s:
        assert s.execute(text("select count(*) from orders where id = :o"), {"o": oid}).scalar() == 0
        assert s.execute(text("select count(*) from portfolios where organization_id = :o"),
                         {"o": tenant.org_id}).scalar() == 0
    # webhook role before tenant resolution: no identity -> nothing visible
    with webhook_session() as s:
        assert s.execute(text("select count(*) from orders")).scalar() == 0
        assert s.execute(text("select count(*) from customers")).scalar() == 0
    # no runtime role can read credentials directly; app role cannot call read_credential
    for sess in (lambda: tenant_session(tenant.org_id, tenant.users["owner"]), lambda: worker_session(tenant.org_id)):
        with pytest.raises(ProgrammingError):
            with sess() as s:
                s.execute(text("select * from account_credentials"))
    with pytest.raises(ProgrammingError):
        with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
            s.execute(text("select read_credential(gen_random_uuid(), 'x')"))
    # tenant-scoped composite FK: a row in org X cannot reference a parent in org Y
    with pytest.raises((IntegrityError, ProgrammingError)):
        with owner_session() as s:
            s.execute(text("insert into order_items (id, organization_id, created_at, order_id, portfolio_id, sku_id, qty, "
                           "unit_price, discount, line_total) select gen_random_uuid(), :other, now(), :o, :p, :sku, 1, "
                           "1, 0, 1"), {"other": other.org_id, "o": oid, "p": other.portfolios["A"],
                                        "sku": w.sku["A1"].id})
    # no runtime role has BYPASSRLS
    with owner_session() as s:
        assert s.execute(text("select count(*) from pg_roles where rolbypassrls and rolname in "
                              "('oms_app','oms_worker','oms_webhook','oms_reporting','oms_reporting_owner')")).scalar() == 0


# --- #20 -----------------------------------------------------------------------------------------------------


def test_20_partial_visibility_uses_order_slices_without_header_totals(tenant):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0"), ("B1", 1, "0")])
    _pay_gateway(tenant, oid)
    with tenant_session(tenant.org_id, tenant.users["pm_a"]) as s:
        assert s.get(Order, oid) is None  # header hidden: user cannot see every line
        assert s.scalars(select(Payment).where(Payment.order_id == oid)).all() == []
        rows = s.execute(text("select * from order_slices where order_id = :o"), {"o": oid}).mappings().all()
        assert {r["portfolio_id"] for r in rows} == {tenant.portfolios["A"]}
        assert "total" not in rows[0] and "shipping_address" not in rows[0]
        assert s.scalars(select(OrderItem).where(OrderItem.order_id == oid)).all() != []  # own line visible
    with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
        assert s.get(Order, oid) is not None


def test_20_reporting_role_identity_cannot_be_spoofed(tenant):
    w = build_world(tenant)
    _order(tenant, w, [("A1", 1, "0")])
    other = make_tenant()
    _order(other, build_world(other), [("A1", 1, "0")])
    role = f"rpt_{tenant.org_id.hex[:10]}"
    with owner_session() as s:
        u = User(organization_id=tenant.org_id, email=f"{role}@oms.internal", name="reporting", is_service=True)
        s.add(u)
        s.flush()
        s.add(RoleAssignment(organization_id=tenant.org_id, user_id=u.id, role="auditor", scope_type="organization",
                             scope_id=None))
        s.flush()
        s.execute(text("select provision_reporting_principal(:r, 'rpt-pw', :o, :u)"),
                  {"r": role, "o": tenant.org_id, "u": u.id})
    conninfo = f"host=localhost port=5432 dbname={TEST_DB} user={role} password=rpt-pw"
    with psycopg.connect(conninfo, autocommit=True) as c:
        orgs = {r[0] for r in c.execute("select organization_id from reporting.portfolio_orders").fetchall()}
        assert orgs == {tenant.org_id}
        c.execute("select set_config('app.org_id', %s, false), set_config('app.user_id', %s, false)",
                  (str(other.org_id), str(other.users["owner"])))
        orgs = {r[0] for r in c.execute("select organization_id from reporting.portfolio_orders").fetchall()}
        assert orgs == {tenant.org_id}  # GUC spoofing ignored for reporting logins (session_user identity)
        for stmt in ("select * from ledger_entries limit 1", "select * from orders limit 1",
                     "select * from reporting_principals", "set role oms_app"):
            with pytest.raises(psycopg.Error):
                c.execute(stmt)


# --- #8 / #9 concurrency (separate connections) ----------------------------------------------------------------


def _parallel(fns):
    barrier, results = threading.Barrier(len(fns)), [None] * len(fns)

    def run(i, fn):
        try:
            barrier.wait()
            results[i] = ("ok", fn())
        except Exception as exc:  # noqa: BLE001
            results[i] = ("err", exc)

    threads = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(fns)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    return results


def test_9_concurrent_reservations_never_oversell(tenant):
    w = build_world(tenant, stock_a1=1)

    def reserve():
        with tenant_session(tenant.org_id, tenant.users["owner"]) as s:
            inventory.reserve(s, tenant.org_id, portfolio_id=tenant.portfolios["A"], sku_id=w.sku["A1"].id, qty=1,
                              kind="order")
        return True

    res = _parallel([reserve, reserve, reserve])
    assert sum(1 for r in res if r[0] == "ok") == 1
    with owner_session() as s:
        bal = s.scalars(select(InventoryBalance).where(InventoryBalance.sku_id == w.sku["A1"].id)).one()
        assert bal.reserved == 1 and bal.on_hand == 1


def test_8_concurrent_purchases_cannot_over_procure(tenant):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A2", 2, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    proc, pm = principal(tenant, "procurement_a"), principal(tenant, "pm_a")
    with owner_session() as s:
        tid = s.scalars(select(PurchaseTask.id).where(PurchaseTask.order_id == oid)).one()
        item_id = s.scalars(select(OrderItem.id).where(OrderItem.order_id == oid)).one()
    with tenant_session(tenant.org_id, proc.user_id) as s:
        aid = procurement.observe(s, proc, tid, price=D("90000"), shipping_cost=D("10000"), in_stock=True,
                                  variant_matches=True).approval_id
    with tenant_session(tenant.org_id, pm.user_id) as s:
        approvals.decide(s, pm, aid, True, "ok")

    def buy(ext, qty, amount):
        def f():
            with tenant_session(tenant.org_id, proc.user_id) as s:
                procurement.record_purchase(s, proc, tid, external_order_id=ext, amount_paid=D(amount),
                                            lines=[(item_id, qty, D("90000"))], evidence_ref="m://x")
            return True
        return f

    res = _parallel([buy("EXT-1", 2, "95000"), buy("EXT-2", 2, "95000")])
    assert sorted(r[0] for r in res) == ["err", "ok"], res
    with owner_session() as s:
        item = s.get(OrderItem, item_id)
        assert item.qty_procured == 2
    # duplicate recording of the same external order is rejected
    with pytest.raises(HTTPException):
        with tenant_session(tenant.org_id, proc.user_id) as s:
            procurement.record_purchase(s, proc, tid, external_order_id="EXT-1", amount_paid=D("1"),
                                        lines=[(item_id, 1, D("1"))], evidence_ref="m://y")


# --- #11a ----------------------------------------------------------------------------------------------------


def test_11a_unmatched_attested_payment_ages_into_exception_with_exposure(tenant):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")], method="manual_transfer")
    ai, fin = principal(tenant, "ai"), principal(tenant, "finance")
    with tenant_session(tenant.org_id, ai.user_id) as s:
        pid = payments.report_manual_transfer(s, s.get(Order, oid), {"receipt": "img"}).id
    with tenant_session(tenant.org_id, fin.user_id) as s:
        payments.attest(s, fin, pid, bank_reference="BCA-9", amount_seen=D("115000"))
        item = s.scalars(select(ReconciliationItem).where(ReconciliationItem.expected_ref == str(pid))).one()
        assert item.status == "open" and reconciliation.bucket(item) == "0-2"
        aged = reconciliation.age(s, today=utcnow().date() + timedelta(days=5))
        assert [a.id for a in aged] == [item.id]
        case = s.get(ExceptionCase, item.exception_case_id)
        assert case.exposure == D("115000") and item.classification == "missing"
        assert reconciliation.open_exposure(s) >= D("115000")


# --- #13a ----------------------------------------------------------------------------------------------------


def test_13a_closed_period_is_immutable_and_corrections_are_adjustments(tenant):
    fin, owner2 = principal(tenant, "finance"), principal(tenant, "owner2")
    last_month = (utcnow().date().replace(day=1) - timedelta(days=1))
    with tenant_session(tenant.org_id, fin.user_id) as s:
        post_journal(s, tenant.org_id, source_type="opex", source_id="rent-oct", legal_entity_id=tenant.legal_entity_id,
                     accounting_date=last_month, lines=[JournalLine("6900", debit=D("1000000")),
                                                        JournalLine("1000", credit=D("1000000"))])
        periods.close_period(s, fin, tenant.legal_entity_id, last_month)
    with pytest.raises((IntegrityError, DBAPIError)):
        with tenant_session(tenant.org_id, fin.user_id) as s:
            post_journal(s, tenant.org_id, source_type="opex", source_id="late", legal_entity_id=tenant.legal_entity_id,
                         accounting_date=last_month, lines=[JournalLine("6900", debit=D("1")),
                                                            JournalLine("1000", credit=D("1"))])
    with tenant_session(tenant.org_id, fin.user_id) as s:
        a = approvals.request_approval(s, fin, kind="ledger_adjustment", subject_type="period",
                                       subject_id=str(last_month.replace(day=1)), amount=D("50000"), payload={},
                                       portfolio_id=None, idempotency_key=f"adj:{tenant.org_id}")
    with tenant_session(tenant.org_id, owner2.user_id) as s:
        approvals.decide(s, owner2, a.id, True, "rent overstated")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        periods.post_adjustment(s, fin, legal_entity_id=tenant.legal_entity_id,
                                closed_period_start=last_month.replace(day=1), approval_id=a.id, reason="rent",
                                lines=[JournalLine("1000", debit=D("50000")), JournalLine("6900", credit=D("50000"))])
        adj = s.scalars(select(LedgerEntry).where(LedgerEntry.source_type == "adjustment")).first()
        assert adj.accounting_date == utcnow().date()


# --- #17 ------------------------------------------------------------------------------------------------------


def test_17_consolidation_eliminates_intercompany_and_inter_portfolio(tenant):
    w = build_world(tenant)
    with owner_session() as s:
        le2 = LegalEntity(organization_id=tenant.org_id, name="PT Second")
        s.add(le2)
    owner = principal(tenant, "owner")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        before = posting.consolidated_balances(s)
        posting.intercompany_transfer(s, tenant.org_id, from_entity=tenant.legal_entity_id, to_entity=le2.id,
                                      amount=D("5000000"), source_id="IC-1")
        inventory.transfer_between_portfolios(s, tenant.org_id, from_portfolio=tenant.portfolios["A"],
                                              to_portfolio=tenant.portfolios["B"], sku_id=w.sku["A1"].id,
                                              warehouse_id=w.warehouse, qty=1, reference="T-17")
        after = posting.consolidated_balances(s)
        assert after.get("1000") == before.get("1000")  # consolidated cash unchanged by internal transfers
        assert after.get("1200") == before.get("1200")
        per_entity = dict(s.execute(text("select legal_entity_id, sum(debit-credit) from ledger_entries "
                                         "where account_code='1000' and source_type like 'intercompany%' "
                                         "group by 1")).all())
        assert per_entity[le2.id] == D("5000000") and per_entity[tenant.legal_entity_id] == D("-5000000")


def test_dates_helper_month_bounds():
    assert periods.month_bounds(date(2026, 12, 15)) == (date(2026, 12, 1), date(2026, 12, 31))
