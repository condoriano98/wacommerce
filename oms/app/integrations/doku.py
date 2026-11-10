"""DOKU Checkout adapter (non-SNAP) — mock by default; live client is documentation-derived.

Facts used (documentation-verified 2026-11-10, NOT sandbox-verified — see docs/04):
* ``POST /checkout/v1/payment`` creates a payment page; ``GET /orders/v1/status/{invoice_number}`` returns status.
* Headers ``Client-Id``, ``Request-Id`` (idempotency key; a replay returns 409), ``Request-Timestamp`` (UTC ISO-8601)
  and ``Signature: HMACSHA256=base64(HMAC_SHA256(secret, components))`` where components are the newline-joined
  ``Client-Id``, ``Request-Id``, ``Request-Timestamp``, ``Request-Target`` and — only for requests with a body —
  ``Digest`` = base64(SHA-256(body)).
* Notifications are triggers only: the OMS re-checks status through the API before capturing (spec §16).
"""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

import httpx

from app.core.external_ops import Rejected
from app.integrations import mockstore

SANDBOX_BASE = "https://api-sandbox.doku.com"
CHECKOUT_PATH = "/checkout/v1/payment"


def digest(body: bytes) -> str:
    return base64.b64encode(hashlib.sha256(body).digest()).decode()


def signature(client_id: str, request_id: str, timestamp: str, target: str, secret: str, body: bytes | None) -> str:
    parts = [f"Client-Id:{client_id}", f"Request-Id:{request_id}", f"Request-Timestamp:{timestamp}",
             f"Request-Target:{target}"]
    if body:
        parts.append(f"Digest:{digest(body)}")
    mac = hmac.new(secret.encode(), "\n".join(parts).encode(), hashlib.sha256).digest()
    return "HMACSHA256=" + base64.b64encode(mac).decode()


def verify_notification(headers: dict, body: bytes, secret: str, target: str) -> bool:
    expected = signature(headers.get("client-id", ""), headers.get("request-id", ""),
                         headers.get("request-timestamp", ""), target, secret, body)
    return hmac.compare_digest(expected, headers.get("signature", ""))


@dataclass
class PaymentRequest:
    invoice_number: str
    amount: Decimal
    expiry_minutes: int
    customer_name: str | None = None
    callback_url: str | None = None


class PaymentProvider(Protocol):
    def create_payment(self, req: PaymentRequest) -> dict: ...

    def get_status(self, invoice_number: str) -> dict | None: ...


class MockDoku:
    """Behaves like the documented API: one payment page per invoice; a replayed invoice is rejected (409)."""

    def create_payment(self, req: PaymentRequest) -> dict:
        with mockstore.state("doku") as st:
            payments = st.setdefault("payments", {})
            if req.invoice_number in payments:
                raise Rejected("409 duplicate request")
            resp = {"ref": f"doku-{req.invoice_number}", "invoice_number": req.invoice_number,
                    "url": f"https://checkout-sandbox.doku.mock/p/{req.invoice_number}",
                    "amount": str(req.amount), "status": "PENDING",
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=req.expiry_minutes)).isoformat()}
            payments[req.invoice_number] = resp
            st["create_calls"] = st.get("create_calls", 0) + 1
        mockstore.fault_after_accept("doku", "create_payment")
        return resp

    def get_status(self, invoice_number: str) -> dict | None:
        with mockstore.state("doku") as st:
            return st.get("payments", {}).get(invoice_number)

    @staticmethod
    def mark_paid(invoice_number: str) -> None:  # test helper: the customer pays
        with mockstore.state("doku") as st:
            st["payments"][invoice_number]["status"] = "SUCCESS"


class LiveDoku:
    """Not used unless the platform account is live-enabled with credentials (none exist; registry: mocked)."""

    def __init__(self, client_id: str, secret_key: str, base_url: str = SANDBOX_BASE, timeout: float = 20):
        self.client_id, self.secret, self.base, self.timeout = client_id, secret_key, base_url.rstrip("/"), timeout

    def _headers(self, request_id: str, target: str, body: bytes | None) -> dict:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"Client-Id": self.client_id, "Request-Id": request_id, "Request-Timestamp": ts,
                "Signature": signature(self.client_id, request_id, ts, target, self.secret, body),
                "Content-Type": "application/json"}

    def create_payment(self, req: PaymentRequest) -> dict:
        body = json.dumps({"order": {"amount": int(req.amount), "invoice_number": req.invoice_number,
                                     "currency": "IDR", "callback_url": req.callback_url},
                           "payment": {"payment_due_date": req.expiry_minutes},
                           "customer": {"name": req.customer_name or "Customer"}}, separators=(",", ":")).encode()
        from app.integrations.http import request

        r = request("POST", self.base + CHECKOUT_PATH, breaker_key=f"doku:{self.client_id}", provider="doku",
                    content=body, headers=self._headers(req.invoice_number, CHECKOUT_PATH, body))
        if r.status_code == 409 or 400 <= r.status_code < 500:
            raise Rejected(f"{r.status_code} {r.text[:300]}")
        r.raise_for_status()
        pay = r.json().get("response", {}).get("payment", {})
        return {"ref": pay.get("token_id") or req.invoice_number, "invoice_number": req.invoice_number,
                "url": pay.get("url"), "amount": str(req.amount), "expires_at": pay.get("expired_date")}

    def get_status(self, invoice_number: str) -> dict | None:
        target = f"/orders/v1/status/{invoice_number}"
        r = httpx.get(self.base + target, timeout=self.timeout,
                      headers=self._headers(f"status-{invoice_number}-{datetime.now(UTC):%H%M%S%f}", target, None))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        return {"ref": invoice_number, "invoice_number": invoice_number,
                "status": data.get("transaction", {}).get("status"), "amount": str(data.get("order", {}).get("amount"))}
