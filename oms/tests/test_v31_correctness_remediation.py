"""V3.1 correctness remediation — financial regression suite (Phase B and later).

Each test reproduces a code-review finding against the real services and PostgreSQL, and asserts the intended
operational AND accounting effect (a balanced journal alone is not proof of correctness).
"""

import uuid
from datetime import timedelta
from decimal import Decimal as D

from sqlalchemy import func, select, text

import app.registry  # noqa: F401
from app.core.db import tenant_session
from app.core.outbox import dispatch_due
from app.models.base import utcnow
from app.models.fulfillment import FulfillmentGroup, Shipment
from app.models.inventory import InventoryBalance, Warehouse
from app.models.orders import Order, OrderItem
from app.modules import fulfillment, inventory
from app.modules.finance import posting
from tests.conftest import owner_session
from tests.test_accounting_services import _manual_pay, _match, _order, _pay_gateway, _release_policy, assert_balanced
from tests.world import build_world, principal


def _second_warehouse(t, w, code: str, qty: int, unit_cost: D) -> uuid.UUID:
    owner = principal(t, "owner")
    with tenant_session(t.org_id, owner.user_id) as s:
        wh = Warehouse(organization_id=t.org_id, name="Surabaya WH", city="Surabaya", postal_code="60111",
                       address="Jl. Contoh 2", legal_entity_id=t.legal_entity_id)
        s.add(wh)
        s.flush()
        sku = w.sku[code]
        pid = t.portfolios[code[0]]
        inventory.receive(s, t.org_id, portfolio_id=pid, sku_id=sku.id, warehouse_id=wh.id, qty=qty,
                          unit_cost=unit_cost, reference=f"wh2-{code}-{uuid.uuid4().hex[:6]}")
        return wh.id


def _owned_groups(order_id) -> list[tuple[uuid.UUID, uuid.UUID]]:
    with owner_session() as s:
        return [(g.id, g.warehouse_id) for g in s.scalars(select(FulfillmentGroup).where(
            FulfillmentGroup.order_id == order_id, FulfillmentGroup.route == "OWNED_STOCK_BITESHIP")
            .order_by(FulfillmentGroup.created_at))]


def _ship_group(t, gid, events, *, shipping_cost=D("12000"), cod_fee=None) -> uuid.UUID:
    packer = principal(t, "packer")
    with tenant_session(t.org_id, packer.user_id) as s:
        sh = fulfillment.create_shipment(s, s.get(FulfillmentGroup, gid), carrier="biteship",
                                         external_id=f"BS-{uuid.uuid4().hex[:10]}", shipping_cost=shipping_cost)
        sid = sh.id
    for i, ev in enumerate(events):
        with tenant_session(t.org_id, packer.user_id) as s:
            fulfillment.apply_shipment_event(s, s.get(Shipment, sid), ev, utcnow() + timedelta(minutes=i),
                                             {"return_cost": "12000"}, cod_fee=cod_fee)
    return sid


def _gl(account: str, **filters) -> D:
    with owner_session() as s:
        return posting._balance(s, account, **filters)


def _stock_value(org_id) -> D:
    """Inventory subledger: on-hand at moving average + goods in transit at the exact cost picked."""
    with owner_session() as s:
        on_hand = s.execute(text("select coalesce(sum(on_hand * avg_unit_cost), 0) from inventory_balances "
                                 "where organization_id = :o"), {"o": org_id}).scalar()
        transit = s.execute(text("select coalesce(sum(amount), 0) from shipment_cost_allocations "
                                 "where organization_id = :o and status = 'in_transit'"), {"o": org_id}).scalar()
        return D(on_hand) + D(transit)


def _balance_of(warehouse_id, sku_id) -> InventoryBalance:
    with owner_session() as s:
        return s.scalars(select(InventoryBalance).where(InventoryBalance.warehouse_id == warehouse_id,
                                                        InventoryBalance.sku_id == sku_id)).one()


# --- B2 multi-warehouse inventory valuation ---------------------------------------------------------------------


def test_B2_multi_warehouse_pickup_delivery_and_failed_delivery_use_exact_warehouse_costs(tenant):
    w = build_world(tenant, stock_a1=2)  # Jakarta: 2 @ 60,000
    wh2 = _second_warehouse(tenant, w, "A1", 1, D("80000"))  # Surabaya: 1 @ 80,000
    oid = _order(tenant, w, [("A1", 3, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    groups = dict((wh, gid) for gid, wh in _owned_groups(oid))
    assert set(groups) == {w.warehouse, wh2}
    _ship_group(tenant, groups[w.warehouse], ("picked",))
    sh2 = _ship_group(tenant, groups[wh2], ("picked",))
    # Goods in transit carry the true acquisition cost: 2 x 60,000 + 1 x 80,000.
    assert _gl("1250", order_id=oid) == D("200000")
    assert _balance_of(w.warehouse, w.sku["A1"].id).on_hand == 0
    assert _balance_of(wh2, w.sku["A1"].id).on_hand == 0
    # Failed delivery of the Surabaya shipment returns exactly its 80,000 to Surabaya stock.
    packer = principal(tenant, "packer")
    with tenant_session(tenant.org_id, packer.user_id) as s:
        fulfillment.apply_shipment_event(s, s.get(Shipment, sh2), "returned", utcnow() + timedelta(hours=1),
                                         {"return_cost": "12000"})
    b2 = _balance_of(wh2, w.sku["A1"].id)
    assert (b2.on_hand, b2.in_transit, b2.avg_unit_cost) == (1, 0, D("80000"))
    assert _gl("1250", order_id=oid) == D("120000")
    # Deliver the Jakarta shipment: COGS is exactly the Jakarta cost.
    with owner_session() as s:
        sh1 = s.scalars(select(Shipment.id).where(Shipment.group_id == groups[w.warehouse])).one()
    with tenant_session(tenant.org_id, packer.user_id) as s:
        fulfillment.apply_shipment_event(s, s.get(Shipment, sh1), "delivered", utcnow() + timedelta(hours=2), {})
    assert _gl("5000", order_id=oid) == D("120000")
    assert _gl("1250", order_id=oid) == 0
    assert _stock_value(tenant.org_id) == _gl("1200", organization_id=tenant.org_id) + \
        _gl("1250", organization_id=tenant.org_id)
    assert_balanced(tenant)


def test_B2_pickup_never_consumes_another_warehouses_reservation(tenant):
    w = build_world(tenant, stock_a1=2)
    wh2 = _second_warehouse(tenant, w, "A1", 1, D("80000"))
    oid = _order(tenant, w, [("A1", 3, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    groups = dict((wh, gid) for gid, wh in _owned_groups(oid))
    _ship_group(tenant, groups[wh2], ("picked",))  # ship the 1-unit Surabaya group first
    assert _balance_of(wh2, w.sku["A1"].id).in_transit == 1
    j = _balance_of(w.warehouse, w.sku["A1"].id)
    assert (j.on_hand, j.reserved, j.in_transit) == (2, 2, 0)  # Jakarta untouched
    assert _gl("1250", order_id=oid) == D("80000")


# --- B3 split-shipment COD -------------------------------------------------------------------------------------


def test_B3_cod_split_across_shipments_collects_shipping_once_and_deducts_own_fees(tenant):
    w = build_world(tenant, stock_a1=2)
    wh2 = _second_warehouse(tenant, w, "A1", 1, D("80000"))
    oid = _order(tenant, w, [("A1", 3, "0")], method="cod")
    dispatch_due()
    with owner_session() as s:
        o = s.get(Order, oid)
        total, shipping = o.total, o.shipping_fee
    assert shipping > 0
    groups = dict((wh, gid) for gid, wh in _owned_groups(oid))
    s1 = _ship_group(tenant, groups[w.warehouse], ("picked", "delivered"), shipping_cost=D("12000"),
                     cod_fee=D("4000"))
    s2 = _ship_group(tenant, groups[wh2], ("picked", "delivered"), shipping_cost=D("9000"), cod_fee=D("2000"))
    with owner_session() as s:
        cods = {sid: s.get(Shipment, sid).cod_amount for sid in (s1, s2)}
        expected = dict(s.execute(text("select expected_ref, expected_amount from reconciliation_items "
                                       "where organization_id = :o and kind = 'cod'"), {"o": tenant.org_id}).all())
    assert sum(cods.values()) == total  # customer pays goods + shipping exactly once
    assert _gl("4100", order_id=oid) == -shipping  # shipping income recognised once
    assert _gl("1100", order_id=oid) == total - D("12000") - D("9000") - D("4000") - D("2000")
    assert D(expected[str(s1)]) == cods[s1] - D("12000") - D("4000")
    assert D(expected[str(s2)]) == cods[s2] - D("9000") - D("2000")
    assert_balanced(tenant)


def test_B3_failed_cod_delivery_never_leaves_cod_receivable_negative(tenant):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")], method="cod")
    dispatch_due()
    (gid, _), = _owned_groups(oid)
    _ship_group(tenant, gid, ("picked", "returned"), shipping_cost=D("12000"))
    assert _gl("1100", order_id=oid) == 0  # nothing collected: carrier cost is a payable, not a COD deduction
    assert _gl("2010", order_id=oid) == D("-24000")  # outbound + return shipping owed to the carrier
    assert_balanced(tenant)


# --- B1 partial delivery, then full bank match ------------------------------------------------------------------


def test_B1_bank_match_after_partial_delivery_clears_only_the_recognised_receivable(tenant):
    w = build_world(tenant, stock_a1=1)
    _second_warehouse(tenant, w, "A1", 1, D("60000"))
    _release_policy(tenant)
    oid = _order(tenant, w, [("A1", 2, "0")], method="manual_transfer")  # Rp200,000 line + shipping
    _manual_pay(tenant, oid, match=False)
    dispatch_due()
    groups = [gid for gid, _ in _owned_groups(oid)]
    assert len(groups) == 2
    _ship_group(tenant, groups[0], ("picked", "delivered"))  # 50% delivered under the approved exception
    with owner_session() as s:
        o = s.get(Order, oid)
        total = o.total
        item = s.scalars(select(OrderItem).where(OrderItem.order_id == oid)).one()
        ship_alloc = posting.shipping_allocation(s, item)
    receivable = D("100000") + posting.q(ship_alloc / 2)
    assert _gl("1105", order_id=oid) == receivable
    _match(tenant, oid)  # full payment proven by the bank
    assert _gl("1105", order_id=oid) == 0  # cleared exactly, never negative
    assert _gl("2200", order_id=oid) == -(total - receivable)  # undelivered half is a customer advance
    assert _gl("1000", order_id=oid) == total  # verified bank cash
    _ship_group(tenant, groups[1], ("picked", "delivered"))
    assert (_gl("1105", order_id=oid), _gl("2200", order_id=oid)) == (0, 0)
    assert _gl("4000", order_id=oid) + _gl("4100", order_id=oid) == -total
    with owner_session() as s:
        assert s.scalar(select(func.count()).select_from(OrderItem).where(
            OrderItem.order_id == oid, OrderItem.qty_delivered == 2)) == 1
    assert_balanced(tenant)


# --- B4 multi-portfolio marketplace settlement ------------------------------------------------------------------


def _multi_portfolio_marketplace_order(t, w, ext: str) -> uuid.UUID:
    from app.core.db import worker_session
    from app.models.catalog import PlatformListing
    from app.models.platforms import PlatformAccount
    from app.modules import catalog, orders

    owner = principal(t, "owner")
    with tenant_session(t.org_id, owner.user_id) as s:
        if not s.scalars(select(PlatformListing).where(PlatformListing.external_sku_ref == "SHP-B1",
                                                       PlatformListing.platform_account_id == w.shopee_seller)).first():
            lst = catalog.upsert_listing(s, owner, portfolio_id=t.portfolios["B"], sku_id=w.sku["B1"].id,
                                         platform_account_id=w.shopee_seller, price=D("80000"),
                                         max_discount_rate=D("0.10"), commission_rate=D("0.08"))
            lst.external_sku_ref = "SHP-B1"
    payload = {"external_order_id": ext, "status": "ready_to_ship", "total": 200000,
               "items": [{"external_sku_ref": "SHP-C1", "qty": 1, "unit_price": 120000},
                         {"external_sku_ref": "SHP-B1", "qty": 1, "unit_price": 80000}]}
    with worker_session(t.org_id) as s:
        acc = s.get(PlatformAccount, w.shopee_seller)
        oid = orders.ingest_marketplace_order(s, acc, payload)[0].id
        orders.ingest_marketplace_order(s, acc, {**payload, "status": "delivered"})
    return oid


def _cash_like(**f) -> D:
    return _gl("1000", **f) + _gl("1050", **f)


def test_B4_marketplace_payout_is_allocated_to_each_portfolio_and_clears_its_receivable(tenant):
    from app.modules import settlements

    w = build_world(tenant)
    oid = _multi_portfolio_marketplace_order(tenant, w, "SHP-MP-1")
    pb, pc = tenant.portfolios["B"], tenant.portfolios["C"]
    assert (_gl("1110", portfolio_id=pc), _gl("1110", portfolio_id=pb)) == (D("110400"), D("73600"))
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        settlements.marketplace_payout(s, fin, w.shopee_seller, "PAYOUT-MP-1", [
            {"external_order_id": "SHP-MP-1", "gross": "200000", "fee": "17000", "tax_withheld": "1000",
             "net": "182000"}])
    # Each portfolio's receivable clears; gross, fees, tax and cash follow item ownership (120:80).
    assert (_gl("1110", portfolio_id=pc), _gl("1110", portfolio_id=pb)) == (0, 0)
    assert (_gl("5300", portfolio_id=pc), _gl("5300", portfolio_id=pb)) == (D("10200"), D("6800"))
    assert (_gl("1400", portfolio_id=pc), _gl("1400", portfolio_id=pb)) == (D("600"), D("400"))
    assert (_cash_like(order_id=oid, portfolio_id=pc), _cash_like(order_id=oid, portfolio_id=pb)) == \
        (D("109200"), D("72800"))
    assert _cash_like(order_id=oid) == D("182000")
    with owner_session() as s:
        item = s.execute(text("select status, difference, portfolio_id from reconciliation_items "
                              "where kind = 'marketplace' and expected_ref = :o"), {"o": str(oid)}).one()
    # The 1,000 fee above the accrual is booked (true-up) and surfaces for review; the item is org-level.
    assert (item.status, D(item.difference), item.portfolio_id) == ("variance", D("-1000"), None)
    assert_balanced(tenant)


def test_B4_two_lines_for_one_order_in_one_payout_are_both_settled(tenant):
    from app.modules import settlements

    w = build_world(tenant)
    _multi_portfolio_marketplace_order(tenant, w, "SHP-MP-2")
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        settlements.marketplace_payout(s, fin, w.shopee_seller, "PAYOUT-MP-2", [
            {"external_order_id": "SHP-MP-2", "gross": "200000", "fee": "17000", "tax_withheld": "1000",
             "net": "182000"},
            {"external_order_id": "SHP-MP-2", "gross": "0", "fee": "500", "tax_withheld": "0", "net": "-500"}])
    assert _gl("5300", platform_account_id=w.shopee_seller) == D("17500")
    assert _gl("1000", platform_account_id=w.shopee_seller) + _gl("1050", platform_account_id=w.shopee_seller) \
        == D("181500")
    assert _gl("1110", platform_account_id=w.shopee_seller) == 0
    assert_balanced(tenant)


# --- B5 verified bank cash -------------------------------------------------------------------------------------


def _bank(t, legal_entity_id) -> uuid.UUID:
    from app.models.org import BankAccount

    with owner_session() as s:
        ba = BankAccount(organization_id=t.org_id, legal_entity_id=legal_entity_id, provider="bank",
                         institution="BCA", account_ref=f"BCA-{uuid.uuid4().hex[:8]}")
        s.add(ba)
        s.flush()
        return ba.id


def _import_and_match(t, bank_account_id, amount: D, reference: str, batch: str) -> tuple[int, int]:
    from app.modules import payments

    fin = principal(t, "finance")
    with tenant_session(t.org_id, fin.user_id) as s:
        n = payments.import_bank_statement(s, fin, bank_account_id, [
            {"booked_at": utcnow().date(), "amount": amount, "reference": reference}], batch=batch)
        return n, payments.match_bank_lines(s, fin)


def test_B5_provider_payout_is_not_verified_cash_until_a_bank_line_of_the_same_entity_proves_it(tenant):
    from app.models.orders import Payment
    from app.models.org import LegalEntity
    from app.modules import settlements
    from app.modules.fpa import cash

    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")])
    _pay_gateway(tenant, oid)
    dispatch_due()
    with owner_session() as s:
        pid, amount = s.execute(select(Payment.id, Payment.amount).where(Payment.order_id == oid)).one()
        other = LegalEntity(organization_id=tenant.org_id, name="Second PT")
        s.add(other)
        s.flush()
        other_le = other.id
    fin = principal(tenant, "finance")
    cash_before = _gl("1000", organization_id=tenant.org_id)
    with tenant_session(tenant.org_id, fin.user_id) as s:
        opening_before = D(cash.forecast_13w(s, tenant.legal_entity_id)["opening_cash"])
        settlements.gateway_disbursement(s, fin, w.wa_account, "DOKU-PAYOUT-B5", [(pid, amount)])
    # Reported, not received: verified cash and the forecast's opening cash are unchanged.
    assert _gl("1000", organization_id=tenant.org_id) == cash_before
    assert _gl("1050", organization_id=tenant.org_id) == amount
    with tenant_session(tenant.org_id, fin.user_id) as s:
        f = cash.forecast_13w(s, tenant.legal_entity_id)
        assert D(f["opening_cash"]) == opening_before and D(f["funds_in_transit_unverified"]) == amount
    # A bank line of ANOTHER legal entity with the same reference and amount proves nothing.
    assert _import_and_match(tenant, _bank(tenant, other_le), amount, "DOKU DOKU-PAYOUT-B5", "x1") == (1, 0)
    assert _gl("1000", organization_id=tenant.org_id) == cash_before
    # The entity's own bank line proves receipt: 1050 → 1000.
    own = _bank(tenant, tenant.legal_entity_id)
    assert _import_and_match(tenant, own, amount, "DOKU DOKU-PAYOUT-B5", "b1") == (1, 1)
    assert _gl("1000", organization_id=tenant.org_id) == cash_before + amount
    assert _gl("1050", organization_id=tenant.org_id) == 0
    # Re-importing the same statement is idempotent and matches nothing new.
    assert _import_and_match(tenant, own, amount, "DOKU DOKU-PAYOUT-B5", "b1") == (0, 0)
    assert _gl("1000", organization_id=tenant.org_id) == cash_before + amount
    assert_balanced(tenant)


def test_B5_unmatched_payout_ages_into_a_bank_unmatched_case(tenant):
    from app.models.orders import Payment
    from app.modules import reconciliation, settlements

    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")])
    _pay_gateway(tenant, oid)
    with owner_session() as s:
        pid, amount = s.execute(select(Payment.id, Payment.amount).where(Payment.order_id == oid)).one()
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        settlements.gateway_disbursement(s, fin, w.wa_account, "DOKU-PAYOUT-LATE", [(pid, amount)])
    with tenant_session(tenant.org_id, fin.user_id) as s:
        aged = reconciliation.age(s, today=utcnow().date() + timedelta(days=5))
        assert any(i.kind == "bank_payout" for i in aged)
    with owner_session() as s:
        case = s.execute(text("select type, exposure from exception_cases where organization_id = :o "
                              "and type = 'bank_unmatched'"), {"o": tenant.org_id}).one()
    assert D(case.exposure) == amount
