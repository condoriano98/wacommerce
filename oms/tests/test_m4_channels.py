"""M4 marketplace adapters (mock_tested): contract tests on documentation-derived fixtures, signing, capability gate
(#10), idempotent windowed sync, lost-push recovery, single-use token rotation under concurrency, settlement ageing
(#11b). No network access: respx blocks every real HTTP call."""

import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from sqlalchemy import func, select, text

import app.registry  # noqa: F401
from app.core.db import tenant_session
from app.core.external_ops import Rejected
from app.integrations import marketplace, mockstore
from app.integrations.marketplace import MockChannel
from app.models.base import utcnow
from app.models.governance import ExceptionCase, IntegrationError
from app.models.orders import Order
from app.models.platforms import IntegrationCapability, PlatformAccount
from app.modules import channel_sync, platforms, reconciliation
from tests.conftest import owner_session
from tests.world import build_world, principal

FIX = Path(__file__).parent / "fixtures" / "marketplace"
D = Decimal


def fx(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture(autouse=True)
def _fresh():
    mockstore.reset()
    yield
    mockstore.reset()


def shopee_raw(sn: str, status: str, when: datetime, sku="SHP-C1", qty=1, price=120000) -> dict:
    return {"order_sn": sn, "order_status": status, "update_time": int(when.timestamp()),
            "total_amount": price * qty, "estimated_shipping_fee": 0,
            "item_list": [{"item_sku": sku, "model_sku": "", "model_quantity_purchased": qty,
                           "model_discounted_price": price}]}


# ---- contract tests: provider payload → OMS shapes ----
def test_normalisers_match_documented_payload_shapes():
    o = marketplace.shopee_order(fx("shopee_order_detail.json"))
    assert o["external_order_id"] == "2611100ABCDE" and o["status"] == "ready_to_ship"
    assert o["items"] == [{"external_sku_ref": "SHP-C1", "qty": 2, "unit_price": D("120000")}]
    assert marketplace.shopee_settlement(fx("shopee_escrow_detail.json")) == {
        "external_order_id": "2611100ABCDE", "gross": D("240000"), "fee": D("20000"), "tax_withheld": D("1200"),
        "net": D("218800")}  # F4 fixture amounts
    t = marketplace.tiktok_order(fx("tiktok_order.json"))
    assert t["items"] == [{"external_sku_ref": "TT-C1", "qty": 2, "unit_price": D("60000")}]
    assert marketplace.tiktok_settlement(fx("tiktok_statement_transaction.json")) == {
        "external_order_id": "576461413038785752", "gross": D("120000"), "fee": D("9600"), "tax_withheld": D("600"),
        "net": D("109800")}
    lz = marketplace.lazada_order(fx("lazada_order.json"))
    assert lz["external_order_id"] == "382910443" and lz["items"][0]["qty"] == 2
    (s,) = marketplace.lazada_settlement(fx("lazada_finance_transactions.json"))
    assert (s["gross"], s["fee"], s["tax_withheld"], s["net"]) == (D("90000"), D("6300"), D("450"), D("83250"))
    assert s["gross"] - s["fee"] - s["tax_withheld"] == s["net"]


def test_signing_schemes_are_deterministic_and_parameter_sensitive():
    a = marketplace.shopee_sign("key", 1001, "/api/v2/order/get_order_list", 1793000000, "tok", 77)
    assert a == marketplace.shopee_sign("key", 1001, "/api/v2/order/get_order_list", 1793000000, "tok", 77)
    assert a != marketplace.shopee_sign("key", 1001, "/api/v2/order/get_order_list", 1793000001, "tok", 77)
    p = {"app_key": "k", "timestamp": "1", "shop_cipher": "c"}
    assert marketplace.tiktok_sign("s", "/order/202309/orders", p) != marketplace.tiktok_sign(
        "s", "/order/202309/orders", {**p, "shop_cipher": "d"})
    assert marketplace.tiktok_sign("s", "/x", {**p, "sign": "ignored", "access_token": "ignored"}) == \
        marketplace.tiktok_sign("s", "/x", p)
    sig = marketplace.lazada_sign("s", "/orders/get", {"app_key": "k", "timestamp": "1"})
    assert sig == sig.upper() and len(sig) == 64


@respx.mock
def test_live_shopee_client_signs_paginates_and_maps_errors():
    calls = []

    def order_list(request):
        q = parse_qs(urlparse(str(request.url)).query)
        ts = int(q["timestamp"][0])
        assert q["sign"][0] == marketplace.shopee_sign("pkey", 1001, "/api/v2/order/get_order_list", ts, "tok", 77)
        calls.append(q.get("cursor", [""])[0])
        if q.get("cursor", [""])[0] == "":
            return httpx.Response(200, json={"error": "", "response": {"order_list": [{"order_sn": "A"}],
                                                                       "more": True, "next_cursor": "c2"}})
        return httpx.Response(200, json={"error": "", "response": {"order_list": [], "more": False}})

    respx.get(url__regex=r".*/api/v2/order/get_order_list.*").mock(side_effect=order_list)
    respx.get(url__regex=r".*/api/v2/order/get_order_detail.*").mock(return_value=httpx.Response(200, json={
        "error": "", "response": {"order_list": [shopee_raw("A", "COMPLETED", datetime.now(UTC))]}}))
    client = marketplace.LiveShopee(1001, "pkey", 77, "tok")
    out = client.list_orders(datetime.now(UTC) - timedelta(days=1), datetime.now(UTC))
    assert calls == ["", "c2"] and out[0]["external_order_id"] == "A" and out[0]["status"] == "delivered"
    respx.get(url__regex=r".*/api/v2/push/get_lost_push_message.*").mock(return_value=httpx.Response(
        200, json={"error": "error_auth", "message": "Invalid access_token"}))
    with pytest.raises(Rejected):
        client.lost_pushes()


# ---- #10 capability gate ----
def test_10_missing_credentials_means_not_authorized_and_no_calls(tenant):
    w = build_world(tenant)
    with owner_session() as s:
        s.get(PlatformAccount, w.shopee_seller).live_enabled = True
    MockChannel.seed("shopee", orders=[shopee_raw("NOAUTH-1", "READY_TO_SHIP", utcnow())])
    with respx.mock(assert_all_called=False) as router:  # any real HTTP call would fail the test
        res = channel_sync.sync_orders(tenant.org_id, w.shopee_seller, utcnow() - timedelta(days=1),
                                       utcnow() + timedelta(minutes=1))
        assert router.calls.call_count == 0
    assert res == {"status": "not_authorized", "calls": 0}
    with mockstore.state("shopee") as st:
        assert st.get("calls", 0) == 0  # not even the mock adapter was called
    with owner_session() as s:
        caps = s.scalars(select(IntegrationCapability).where(
            IntegrationCapability.platform_account_id == w.shopee_seller)).all()
        assert caps and {c.access_state for c in caps} == {"not_authorized"}
        assert not any(c.operationally_ready for c in caps)
        assert s.scalar(select(func.count()).select_from(Order).where(Order.external_order_id == "NOAUTH-1")) == 0


# ---- mocked sync ----
def test_windowed_sync_is_idempotent_and_errors_do_not_stop_the_run(tenant):
    w = build_world(tenant)
    now = utcnow()
    MockChannel.seed("shopee", orders=[shopee_raw("SYNC-1", "READY_TO_SHIP", now - timedelta(days=20)),
                                       shopee_raw("SYNC-2", "COMPLETED", now - timedelta(days=2)),
                                       shopee_raw("SYNC-BAD", "READY_TO_SHIP", now - timedelta(days=1), sku="NOPE")])
    first = channel_sync.sync_orders(tenant.org_id, w.shopee_seller, now - timedelta(days=30), now)
    again = channel_sync.sync_orders(tenant.org_id, w.shopee_seller, now - timedelta(days=25), now)  # overlap
    assert first["windows"] == 2 and first["created"] == 2 and first["errors"] == 1
    assert again["created"] == 0
    with owner_session() as s:
        n = s.scalar(select(func.count()).select_from(Order).where(Order.organization_id == tenant.org_id,
                                                                   Order.external_order_id.like("SYNC-%")))
        assert n == 2
        assert s.scalars(select(IntegrationError).where(IntegrationError.platform_account_id == w.shopee_seller,
                                                        IntegrationError.kind == "order_ingest")).all()
        caps = s.scalars(select(IntegrationCapability).where(
            IntegrationCapability.platform_account_id == w.shopee_seller)).all()
        assert {(c.implementation_status, c.access_state) for c in caps} == {("mock_tested", "mocked")}


def test_lost_push_recovery_ingests_missed_order(tenant):
    w = build_world(tenant)
    MockChannel.seed("shopee", orders=[shopee_raw("LOST-1", "READY_TO_SHIP", utcnow() - timedelta(hours=5))],
                     lost_push=["LOST-1"])
    assert channel_sync.recover_lost_pushes(tenant.org_id, w.shopee_seller) == 1
    with owner_session() as s:
        assert s.scalar(select(func.count()).select_from(Order).where(Order.external_order_id == "LOST-1")) == 1


def test_single_use_refresh_token_is_never_spent_twice_under_concurrency(tenant):
    w = build_world(tenant)
    owner = principal(tenant, "owner")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        platforms.store_credential(s, owner, w.shopee_seller, "refresh_token", "shopee-rt-0")
    barrier, results, errors = threading.Barrier(2), [], []

    def worker():
        barrier.wait()
        try:
            results.append(channel_sync.rotate_refresh_token(tenant.org_id, w.shopee_seller, MockChannel("shopee")))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and results == [True, True]
    with mockstore.state("shopee") as st:
        used = st["used_refresh"]
    assert len(used) == len(set(used)) == 2 and used[0] == "shopee-rt-0"  # each token spent exactly once
    with tenant_session(tenant.org_id, owner.user_id) as s:
        creds = {c["name"]: c["version"] for c in platforms.list_credentials(s, w.shopee_seller)}
    assert creds["refresh_token"] == 3 and "access_token" in creds
    assert platforms.read_secret(tenant.org_id, w.shopee_seller, "refresh_token") == "shopee-rt-2"


# ---- #11b marketplace settlement ageing ----
def _delivered_marketplace_order(t, w, sn: str):
    MockChannel.seed("shopee", orders=[shopee_raw(sn, "COMPLETED", utcnow() - timedelta(hours=1))])
    channel_sync.sync_orders(t.org_id, w.shopee_seller, utcnow() - timedelta(days=1), utcnow() + timedelta(minutes=1))
    with owner_session() as s:
        return s.scalars(select(Order).where(Order.external_order_id == sn)).one().id


def _item(oid):
    with owner_session() as s:
        return s.execute(text("select status, expected_amount, exception_case_id from reconciliation_items "
                              "where kind = 'marketplace' and expected_ref = :r"), {"r": str(oid)}).one()


def test_11b_missing_payout_ages_into_case_and_late_payout_resolves_it(tenant):
    w = build_world(tenant)
    oid = _delivered_marketplace_order(tenant, w, "LATE-1")
    status_, expected, _ = _item(oid)
    assert status_ == "open" and expected == D("110400.00")  # 120,000 less the accrued 8% expected fee
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        aged = reconciliation.age(s, today=utcnow().date() + timedelta(days=15))
        assert any(i.expected_ref == str(oid) for i in aged)
    _, _, case_id = _item(oid)
    with owner_session() as s:
        case = s.get(ExceptionCase, case_id)
        assert case.type == "marketplace_settlement_mismatch" and case.exposure == D("110400.00")
    # The payout arrives late with exactly the expected net.
    MockChannel.seed("shopee", payouts=[{"payout_id": "PO-LATE-1", "paid_at": utcnow().isoformat(), "rows": [
        {"order_sn": "LATE-1", "order_income": {"order_selling_price": 120000, "commission_fee": 9600,
                                                "service_fee": 0, "seller_transaction_fee": 0, "withholding_tax": 0,
                                                "escrow_amount": 110400}}]}])
    assert channel_sync.sync_payouts(tenant.org_id, w.shopee_seller, utcnow() - timedelta(days=1),
                                     utcnow() + timedelta(minutes=1))["payouts"] == 1
    assert _item(oid)[0] == "matched"
    with owner_session() as s:
        assert s.get(ExceptionCase, case_id).status == "resolved"
    # Replaying the same payout changes nothing (idempotent per payout id).
    channel_sync.sync_payouts(tenant.org_id, w.shopee_seller, utcnow() - timedelta(days=1),
                              utcnow() + timedelta(minutes=1))
    assert _item(oid)[0] == "matched"


def test_11b_short_payout_opens_case_with_shortfall_exposure(tenant):
    w = build_world(tenant)
    oid = _delivered_marketplace_order(tenant, w, "SHORT-1")
    MockChannel.seed("shopee", payouts=[{"payout_id": "PO-SHORT-1", "paid_at": utcnow().isoformat(), "rows": [
        {"order_sn": "SHORT-1", "order_income": {"order_selling_price": 120000, "commission_fee": 20000,
                                                 "service_fee": 0, "seller_transaction_fee": 0,
                                                 "withholding_tax": 0, "escrow_amount": 100000}}]}])
    channel_sync.sync_payouts(tenant.org_id, w.shopee_seller, utcnow() - timedelta(days=1),
                              utcnow() + timedelta(minutes=1))
    status_, _, case_id = _item(oid)
    assert status_ == "variance"
    with owner_session() as s:
        case = s.get(ExceptionCase, case_id)
        assert case.type == "marketplace_settlement_mismatch" and case.exposure == D("10400.00")


def test_scheduled_sync_isolates_failing_accounts(tenant, monkeypatch):
    w = build_world(tenant)
    MockChannel.seed("shopee", orders=[shopee_raw("SCHED-1", "READY_TO_SHIP", utcnow() - timedelta(hours=2))])
    res = channel_sync.run_scheduled("orders")
    assert res["ok"] >= 1
    with owner_session() as s:
        assert s.scalar(select(func.count()).select_from(Order).where(Order.external_order_id == "SCHED-1",
                                                                      Order.organization_id == tenant.org_id)) == 1

    def boom(*a, **k):
        raise RuntimeError("provider 503")

    monkeypatch.setattr(channel_sync, "sync_orders", boom)
    assert channel_sync.run_scheduled("orders")["failed"] >= 1
    with owner_session() as s:
        caps = s.scalars(select(IntegrationCapability).where(
            IntegrationCapability.platform_account_id == w.shopee_seller)).all()
        assert {c.access_state for c in caps} == {"temporarily_unavailable"}
