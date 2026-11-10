"""Biteship carrier adapter — mock by default; live client is documentation-derived.

Facts used (documentation-verified 2026-11-10, NOT sandbox-verified — see docs/04):
* ``Authorization: Bearer <key>``; test and live keys have distinct prefixes (a test key never books real couriers).
* ``POST /v1/orders`` with ``reference_id`` (our idempotency key); a duplicate ``reference_id`` is rejected with error
  code 40002060. Orders can be retrieved only by Biteship's own id — **there is no lookup by reference_id (G-BS4)**, so
  an unconfirmed booking becomes ``uncertain`` and is resolved by a human (ADR-015); the OMS never re-books.
* Webhook authentication is unverified (G-BS2): callbacks are treated as triggers and the order is re-fetched.
* Booking a paid shipment needs an approved ``paid_shipment_booking`` approval (spec §14).
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from app.core.external_ops import NotSent, OperationUncertain, Rejected  # noqa: F401  (re-exported for callers)
from app.integrations import http as provider_http
from app.integrations import mockstore

BASE = "https://api.biteship.com"
DUPLICATE_REFERENCE = 40002060


@dataclass
class BookingRequest:
    reference_id: str
    courier_company: str
    courier_type: str
    destination: dict
    items: list[dict] = field(default_factory=list)
    cod_amount: Decimal = Decimal("0")


class CarrierAdapter(Protocol):
    def create_order(self, req: BookingRequest) -> dict: ...

    def get_order(self, order_id: str) -> dict | None: ...


class MockBiteship:
    def create_order(self, req: BookingRequest) -> dict:
        with mockstore.state("biteship") as st:
            orders = st.setdefault("orders", {})
            if any(o["reference_id"] == req.reference_id for o in orders.values()):
                # The documented behaviour: duplicate reference rejected — but the first booking exists.
                raise Rejected(f"{DUPLICATE_REFERENCE} duplicate reference_id")
            # Provider ids are globally unique (like Biteship's); a counter would repeat after a mock reset.
            oid = f"bs-{len(orders) + 1:06d}-{uuid.uuid4().hex[:8]}"
            resp = {"ref": oid, "reference_id": req.reference_id, "waybill_id": f"WB{len(orders) + 1:08d}",
                    "price": "12000", "cod_amount": str(req.cod_amount), "status": "confirmed",
                    "updated_at": datetime.now(UTC).isoformat()}
            orders[oid] = resp
        mockstore.fault_after_accept("biteship", "create_order")
        return resp

    def get_order(self, order_id: str) -> dict | None:
        with mockstore.state("biteship") as st:
            return st.get("orders", {}).get(order_id)

    @staticmethod
    def set_status(order_id: str, status: str, updated_at: str) -> None:  # test helper: courier progress
        with mockstore.state("biteship") as st:
            st["orders"][order_id].update(status=status, updated_at=updated_at)

    @staticmethod
    def bookings_for(reference_id: str) -> list[dict]:  # test helper: provider-side truth
        with mockstore.state("biteship") as st:
            return [o for o in st.get("orders", {}).values() if o["reference_id"] == reference_id]


class LiveBiteship:
    def __init__(self, api_key: str, base_url: str = BASE, timeout: float = 20):
        self.h = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.base, self.timeout = base_url.rstrip("/"), timeout

    def create_order(self, req: BookingRequest) -> dict:
        body = {"reference_id": req.reference_id, "courier_company": req.courier_company,
                "courier_type": req.courier_type, "items": req.items, **req.destination}
        if req.cod_amount:
            body["destination_cash_on_delivery"] = int(req.cod_amount)
        from app.integrations.http import request

        r = request("POST", f"{self.base}/v1/orders", breaker_key="biteship", provider="biteship", json=body,
                    headers=self.h)
        if 400 <= r.status_code < 500:
            raise Rejected(f"{r.status_code} {r.text[:300]}")
        r.raise_for_status()
        data = r.json()
        return {"ref": data["id"], "reference_id": req.reference_id,
                "waybill_id": data.get("courier", {}).get("waybill_id"), "price": str(data.get("price", 0)),
                "cod_amount": str(req.cod_amount), "status": data.get("status")}

    def get_order(self, order_id: str) -> dict | None:
        r = provider_http.request("GET", f"{self.base}/v1/orders/{order_id}", breaker_key="biteship",
                                  provider="biteship", headers=self.h, timeout=self.timeout)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
