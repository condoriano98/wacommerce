"""Synthetic test world built through the real services."""

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select

from app.core.authz import Assignment, Principal
from app.core.db import tenant_session
from app.models.inventory import Warehouse
from app.models.org import RoleAssignment, User
from app.modules import catalog, customers, inventory, platforms
from app.modules.finance import accounts as A
from app.modules.finance.ledger import JournalLine, post_journal

D = Decimal


def principal(t, name: str) -> Principal:
    from tests.conftest import owner_session

    with owner_session() as s:
        user = s.get(User, t.users[name])
        rows = s.scalars(select(RoleAssignment).where(RoleAssignment.user_id == user.id)).all()
        return Principal(user.id, t.org_id, user.is_service, [Assignment(r.role, r.scope_type, r.scope_id) for r in rows])


@dataclass
class World:
    t: object
    warehouse: uuid.UUID = None
    wa_account: uuid.UUID = None
    shopee_seller: uuid.UUID = None
    sku: dict = field(default_factory=dict)
    offer: dict = field(default_factory=dict)
    customer: uuid.UUID = None
    customer_b: uuid.UUID = None


def build_world(t, *, stock_a1: int = 5, offer_fresh: bool = True) -> World:
    w = World(t)
    owner = principal(t, "owner")
    pa, pb, pc = t.portfolios["A"], t.portfolios["B"], t.portfolios["C"]
    with tenant_session(t.org_id, owner.user_id) as s:
        wh = Warehouse(organization_id=t.org_id, name="Jakarta WH", city="Jakarta", postal_code="12345",
                       address="Jl. Contoh 1", legal_entity_id=t.legal_entity_id)
        s.add(wh)
        s.flush()
        w.warehouse = wh.id
        wa = platforms.register_account(s, owner, provider="whatsapp", provider_account_id="1098765",
                                        display_name="WA Sales", role="messaging", portfolio_ids=[pa, pb],
                                        public_config={"phone_number_id": f"1098765{t.org_id.hex[:6]}",
                                                       "waba_id": "55501"},
                                        brand_id=t.brand_id)
        wa.legal_entity_id = t.legal_entity_id
        w.wa_account = wa.id
        shp = platforms.register_account(s, owner, provider="shopee", provider_account_id="SHOP-777",
                                         display_name="Shopee store C", role="sales", portfolio_ids=[pc],
                                         brand_id=t.brand_id)
        shp.legal_entity_id = t.legal_entity_id
        w.shopee_seller = shp.id
        specs = [
            ("A1", pa, "owned", D("100000"), D("60000"), stock_a1),
            ("A2", pa, "dropship_only", D("150000"), None, 0),
            ("B1", pb, "owned", D("80000"), D("40000"), 10),
            ("C1", pc, "owned", D("120000"), D("70000"), 2),
            ("F2", pa, "owned", D("150000"), D("90000"), 5),
            ("F6", pa, "owned", D("110000"), D("50000"), 5),
        ]
        for code, pid, policy, price, cost, qty in specs:
            _, (sku,) = catalog.create_product_with_skus(
                s, owner, name=f"Product {code}", category="fmcg", portfolio_ids=[pid],
                skus=[{"code": f"{code}-{t.org_id.hex[:6]}", "name": f"Item {code}", "inventory_policy": policy}])
            w.sku[code] = sku
            account = shp.id if code == "C1" else wa.id
            catalog.upsert_listing(s, owner, portfolio_id=pid, sku_id=sku.id, platform_account_id=account, price=price,
                                   max_discount_rate=D("0.10"), commission_rate=D("0.08") if code == "C1" else D("0"))
            if code == "C1":
                from app.models.catalog import PlatformListing

                lst = s.scalars(select(PlatformListing).where(PlatformListing.sku_id == sku.id)).one()
                lst.external_sku_ref = f"SHP-{code}"
            if qty:
                inventory.receive(s, t.org_id, portfolio_id=pid, sku_id=sku.id, warehouse_id=wh.id, qty=qty,
                                  unit_cost=cost, reference=f"init-{code}")
        offer = catalog.create_supplier_offer(
            s, owner, supplier={"name": "Toko Supplier", "marketplace": "shopee"}, portfolio_id=pa,
            sku_id=w.sku["A2"].id, marketplace="shopee", listing_url="https://shopee.co.id/supplier/a2",
            expected_variant="500ml", reference_price=D("90000"), price_ceiling=D("100000"),
            reference_shipping=D("10000"), approved=True)
        w.offer["A2"] = offer.id
        if offer_fresh:
            catalog.record_observation(s, owner, offer.id, price=D("90000"), shipping_cost=D("10000"), in_stock=True,
                                       variant_matches=True)
        # Working capital so dropship purchases don't breach the reserve.
        post_journal(s, t.org_id, source_type="owner_capital", source_id=str(uuid.uuid4()),
                     legal_entity_id=t.legal_entity_id, lines=[
            JournalLine(A.CASH, debit=D("50000000")), JournalLine(A.OWNER_CAPITAL, credit=D("50000000"))])
        c = customers.resolve_customer(s, t.org_id, t.brand_id, {"bsuid": f"ID.{uuid.uuid4().hex[:10]}"}, [pa, pb],
                                       "Budi")
        w.customer = c.id
    return w
