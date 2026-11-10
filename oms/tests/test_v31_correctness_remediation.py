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
