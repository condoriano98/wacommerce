"""Marketplace seller adapters: Shopee Open Platform v2, TikTok Shop (incl. Tokopedia & Shop), Lazada (spec §17).

Status: **mock_tested** only. Normalisers are exercised by contract tests on documentation-derived payload fixtures
(``oms/tests/fixtures/marketplace``) — not payloads recorded from real shops. Signing follows each platform's published
scheme but no request has ever been sent; access is ``not_authorized`` (Shopee eligibility, TikTok ISV, Lazada app
approval). Facts used (docs/04, 2026-11-09 research): Shopee access 4 h / refresh 30 d single-use, 15-day order windows,
escrow detail for fees, lost-push recovery; TikTok ``shop_cipher``, statements with PPh 22; Lazada 10 d / 50 d tokens.
"""

import hashlib
import hmac
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from app.core.external_ops import Rejected
from app.integrations import http as provider_http
from app.integrations import mockstore

MAX_WINDOW = {"shopee": timedelta(days=15), "tiktokshop": timedelta(days=30), "lazada": timedelta(days=30)}
D = Decimal


def windows(start: datetime, end: datetime, size: timedelta) -> Iterator[tuple[datetime, datetime]]:
    cur = start
    while cur < end:
        nxt = min(cur + size, end)
        yield cur, nxt
        cur = nxt


# ---- request signing (published schemes; not verified against live endpoints) ----
def shopee_sign(partner_key: str, partner_id: int, path: str, ts: int, access_token: str = "", shop_id: int | str = "") -> str:
    base = f"{partner_id}{path}{ts}{access_token}{shop_id}"
    return hmac.new(partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()


def tiktok_sign(app_secret: str, path: str, params: dict, body: str = "") -> str:
    keys = sorted(k for k in params if k not in ("sign", "access_token"))
    base = app_secret + path + "".join(f"{k}{params[k]}" for k in keys) + body + app_secret
    return hmac.new(app_secret.encode(), base.encode(), hashlib.sha256).hexdigest()


def lazada_sign(app_secret: str, api_path: str, params: dict) -> str:
    base = api_path + "".join(f"{k}{params[k]}" for k in sorted(params) if k != "sign")
    return hmac.new(app_secret.encode(), base.encode(), hashlib.sha256).hexdigest().upper()


# ---- normalisers: provider payload → OMS shapes (orders.ingest_marketplace_order, settlements.marketplace_payout) ----
SHOPEE_STATUS = {"UNPAID": "unpaid", "READY_TO_SHIP": "ready_to_ship", "PROCESSED": "ready_to_ship",
                 "SHIPPED": "shipped", "TO_CONFIRM_RECEIVE": "shipped", "COMPLETED": "delivered",
                 "CANCELLED": "canceled", "IN_CANCEL": "canceled", "TO_RETURN": "returned"}
TIKTOK_STATUS = {"UNPAID": "unpaid", "AWAITING_SHIPMENT": "ready_to_ship", "AWAITING_COLLECTION": "ready_to_ship",
                 "IN_TRANSIT": "shipped", "DELIVERED": "delivered", "COMPLETED": "delivered", "CANCELLED": "canceled"}
LAZADA_STATUS = {"unpaid": "unpaid", "pending": "ready_to_ship", "ready_to_ship": "ready_to_ship",
                 "shipped": "shipped", "delivered": "delivered", "confirmed": "delivered", "canceled": "canceled",
                 "returned": "returned"}


def shopee_order(raw: dict) -> dict:
    items = [{"external_sku_ref": i.get("model_sku") or i["item_sku"], "qty": int(i["model_quantity_purchased"]),
              "unit_price": D(str(i["model_discounted_price"]))} for i in raw["item_list"]]
    return {"external_order_id": raw["order_sn"], "status": SHOPEE_STATUS[raw["order_status"]], "items": items,
            "shipping_fee": D(str(raw.get("estimated_shipping_fee", 0))), "total": D(str(raw["total_amount"])),
            "updated_at": datetime.fromtimestamp(int(raw["update_time"]), UTC)}


def shopee_settlement(raw: dict) -> dict:
    inc = raw["order_income"]
    fee = sum((D(str(inc.get(k, 0))) for k in ("commission_fee", "service_fee", "seller_transaction_fee")), D(0))
    return {"external_order_id": raw["order_sn"], "gross": D(str(inc["order_selling_price"])), "fee": fee,
            "tax_withheld": D(str(inc.get("withholding_tax", 0))), "net": D(str(inc["escrow_amount"]))}


def tiktok_order(raw: dict) -> dict:
    agg: dict[str, dict] = {}
    for li in raw["line_items"]:  # TikTok returns one line item per unit
        row = agg.setdefault(li["seller_sku"], {"external_sku_ref": li["seller_sku"], "qty": 0,
                                                "unit_price": D(str(li["sale_price"]))})
        row["qty"] += 1
    pay = raw["payment"]
    return {"external_order_id": raw["id"], "status": TIKTOK_STATUS[raw["status"]], "items": list(agg.values()),
            "shipping_fee": D(str(pay.get("shipping_fee", 0))), "total": D(str(pay["total_amount"])),
            "updated_at": datetime.fromtimestamp(int(raw["update_time"]), UTC)}


def tiktok_settlement(raw: dict) -> dict:
    return {"external_order_id": raw["order_id"], "gross": D(str(raw["revenue_amount"])),
            "fee": -D(str(raw["fee_amount"])), "tax_withheld": -D(str(raw.get("tax_amount", 0))),
            "net": D(str(raw["settlement_amount"]))}


def lazada_order(raw: dict) -> dict:
    agg: dict[str, dict] = {}
    for it in raw["items"]:  # Lazada returns one item row per unit
        row = agg.setdefault(it["sku"], {"external_sku_ref": it["sku"], "qty": 0, "unit_price": D(str(it["paid_price"]))})
        row["qty"] += 1
    return {"external_order_id": str(raw["order_id"]), "status": LAZADA_STATUS[raw["statuses"][0]],
            "items": list(agg.values()), "shipping_fee": D(str(raw.get("shipping_fee", 0))),
            "total": D(str(raw["price"])), "updated_at": datetime.fromisoformat(raw["updated_at"])}


def lazada_settlement(rows: list[dict]) -> list[dict]:
    """Finance transaction rows (one per fee type) grouped per order."""
    out: dict[str, dict] = {}
    for r in rows:
        o = out.setdefault(str(r["order_no"]), {"external_order_id": str(r["order_no"]), "gross": D(0), "fee": D(0),
                                                "tax_withheld": D(0), "net": D(0)})
        amt = D(str(r["amount"]))
        kind = r["transaction_type"]
        if kind == "Item Price Credit":
            o["gross"] += amt
        elif kind == "Withholding Tax":
            o["tax_withheld"] += -amt
        else:  # Commission, Payment Fee, Shipping Fee Voucher ...
            o["fee"] += -amt
        o["net"] += amt
    return list(out.values())


NORMALISE = {"shopee": (shopee_order, shopee_settlement), "tiktokshop": (tiktok_order, tiktok_settlement),
             "lazada": (lazada_order, None)}


class SalesChannelAdapter(Protocol):
    provider: str

    def list_orders(self, start: datetime, end: datetime) -> list[dict]: ...

    def list_payouts(self, start: datetime, end: datetime) -> list[dict]: ...

    def lost_pushes(self) -> list[str]: ...

    def refresh_token(self, refresh_token: str) -> dict: ...


class MockChannel:
    """Provider-shaped payloads kept in ``mockstore``; normalised exactly like live responses would be."""

    def __init__(self, provider: str):
        self.provider = provider

    def list_orders(self, start: datetime, end: datetime) -> list[dict]:
        norm = NORMALISE[self.provider][0]
        with mockstore.state(self.provider) as st:
            st["calls"] = st.get("calls", 0) + 1
            raws = list(st.get("orders", {}).values())
        return [o for o in map(norm, raws) if start <= o["updated_at"] < end]

    def list_payouts(self, start: datetime, end: datetime) -> list[dict]:
        with mockstore.state(self.provider) as st:
            payouts = list(st.get("payouts", []))
        out = []
        for p in payouts:
            paid = datetime.fromisoformat(p["paid_at"])
            if not (start <= paid < end):
                continue
            lines = (lazada_settlement(p["rows"]) if self.provider == "lazada"
                     else [NORMALISE[self.provider][1](r) for r in p["rows"]])
            out.append({"payout_id": p["payout_id"], "paid_at": paid, "lines": lines})
        return out

    def lost_pushes(self) -> list[str]:
        with mockstore.state(self.provider) as st:
            return list(st.pop("lost_push", []))

    def refresh_token(self, refresh_token: str) -> dict:
        with mockstore.state(self.provider) as st:
            used = st.setdefault("used_refresh", [])
            if refresh_token in used:
                raise Rejected("refresh token already used (single-use)")
            used.append(refresh_token)
            n = len(used)
        return {"access_token": f"{self.provider}-at-{n}", "refresh_token": f"{self.provider}-rt-{n}",
                "expire_in": 14400}

    @staticmethod
    def seed(provider: str, *, orders: list[dict] | None = None, payouts: list[dict] | None = None,
             lost_push: list[str] | None = None) -> None:  # test helper
        key = {"shopee": "order_sn", "tiktokshop": "id", "lazada": "order_id"}[provider]
        with mockstore.state(provider) as st:
            for o in orders or []:
                st.setdefault("orders", {})[str(o[key])] = o
            st.setdefault("payouts", []).extend(payouts or [])
            st.setdefault("lost_push", []).extend(lost_push or [])


class LiveShopee:
    """Documentation-derived client; never selected without live flag + credentials (none exist)."""

    BASE = "https://partner.shopeemobile.com"

    def __init__(self, partner_id: int, partner_key: str, shop_id: int, access_token: str, timeout: float = 20):
        self.pid, self.key, self.shop, self.token, self.timeout = partner_id, partner_key, shop_id, access_token, timeout
        self.provider = "shopee"

    def _get(self, path: str, params: dict) -> dict:
        ts = int(datetime.now(UTC).timestamp())
        q = {"partner_id": self.pid, "timestamp": ts, "access_token": self.token, "shop_id": self.shop,
             "sign": shopee_sign(self.key, self.pid, path, ts, self.token, self.shop), **params}
        r = provider_http.request("GET", self.BASE + path, breaker_key=f"shopee:{self.shop}", provider="shopee",
                                  params=q, timeout=self.timeout)  # connect failures raise NotSent
        if r.status_code == 429:
            raise TimeoutError("rate limited")  # caller backs off
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise Rejected(f"{body['error']}: {body.get('message')}")
        return body["response"]

    def list_orders(self, start: datetime, end: datetime) -> list[dict]:
        out, cursor = [], ""
        while True:
            resp = self._get("/api/v2/order/get_order_list", {
                "time_range_field": "update_time", "time_from": int(start.timestamp()),
                "time_to": int(end.timestamp()), "page_size": 100, "cursor": cursor})
            sns = [o["order_sn"] for o in resp.get("order_list", [])]
            if sns:
                detail = self._get("/api/v2/order/get_order_detail", {
                    "order_sn_list": ",".join(sns),
                    "response_optional_fields": "item_list,total_amount,estimated_shipping_fee,update_time"})
                out += [shopee_order(o) for o in detail["order_list"]]
            if not resp.get("more"):
                return out
            cursor = resp.get("next_cursor", "")

    def list_payouts(self, start: datetime, end: datetime) -> list[dict]:
        raise NotImplementedError("Shopee payouts: escrow list + detail; mock only until access is granted")

    def lost_pushes(self) -> list[str]:
        return [m.get("order_sn") for m in self._get("/api/v2/push/get_lost_push_message", {}).get("data_list", [])]

    def refresh_token(self, refresh_token: str) -> dict:
        raise NotImplementedError("live token refresh disabled until access is granted")
