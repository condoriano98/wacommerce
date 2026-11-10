"""Offline contract tests for the live adapters (respx; no network). They check that requests follow the
documentation-verified shapes (docs/04) — they do NOT prove sandbox or production behaviour."""

import base64
import hashlib
import hmac
import json
from decimal import Decimal

import httpx
import pytest
import respx

from app.core.external_ops import NotSent, Rejected
from app.integrations import factory
from app.integrations.biteship import BookingRequest, LiveBiteship, MockBiteship
from app.integrations.doku import SANDBOX_BASE, LiveDoku, MockDoku, PaymentRequest, signature, verify_notification


def test_doku_signature_matches_documented_component_format():
    body = b'{"order":{"amount":100000}}'
    comps = ("Client-Id:MCH-1\nRequest-Id:INV-1\nRequest-Timestamp:2026-11-10T00:00:00Z\n"
             "Request-Target:/checkout/v1/payment\nDigest:" + base64.b64encode(hashlib.sha256(body).digest()).decode())
    expected = "HMACSHA256=" + base64.b64encode(hmac.new(b"SK", comps.encode(), hashlib.sha256).digest()).decode()
    assert signature("MCH-1", "INV-1", "2026-11-10T00:00:00Z", "/checkout/v1/payment", "SK", body) == expected
    # No Digest component for body-less requests (GET status).
    assert signature("MCH-1", "R", "T", "/orders/v1/status/INV-1", "SK", None) != signature(
        "MCH-1", "R", "T", "/orders/v1/status/INV-1", "SK", b"x")


def test_doku_notification_verification_rejects_tampering():
    body = b'{"transaction":{"status":"SUCCESS"}}'
    h = {"client-id": "MCH-1", "request-id": "N1", "request-timestamp": "2026-11-10T00:00:00Z"}
    h["signature"] = signature("MCH-1", "N1", h["request-timestamp"], "/webhooks/doku", "SK", body)
    assert verify_notification(h, body, "SK", "/webhooks/doku")
    assert not verify_notification(h, body.replace(b"SUCCESS", b"FAILED"), "SK", "/webhooks/doku")


@respx.mock
def test_live_doku_request_shape_and_error_mapping():
    route = respx.post(f"{SANDBOX_BASE}/checkout/v1/payment").mock(return_value=httpx.Response(
        200, json={"response": {"payment": {"url": "https://pay/x", "token_id": "tok1", "expired_date": None}}}))
    resp = LiveDoku("MCH-1", "SK").create_payment(PaymentRequest("INV-9", Decimal("100000"), 60))
    req = route.calls.last.request
    assert req.headers["Request-Id"] == "INV-9" and req.headers["Signature"].startswith("HMACSHA256=")
    assert json.loads(req.content)["order"] == {"amount": 100000, "invoice_number": "INV-9", "currency": "IDR",
                                                 "callback_url": None}
    assert resp["ref"] == "tok1" and resp["url"] == "https://pay/x"

    respx.post(f"{SANDBOX_BASE}/checkout/v1/payment").mock(return_value=httpx.Response(409, json={}))
    with pytest.raises(Rejected):
        LiveDoku("MCH-1", "SK").create_payment(PaymentRequest("INV-9", Decimal("100000"), 60))
    respx.post(f"{SANDBOX_BASE}/checkout/v1/payment").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(NotSent):
        LiveDoku("MCH-1", "SK").create_payment(PaymentRequest("INV-9", Decimal("100000"), 60))


@respx.mock
def test_live_biteship_bearer_reference_and_duplicate_rejection():
    route = respx.post("https://api.biteship.com/v1/orders").mock(return_value=httpx.Response(
        200, json={"id": "bs1", "courier": {"waybill_id": "WB1"}, "price": 12000, "status": "confirmed"}))
    req = BookingRequest("grp-1", "jne", "reg", {"destination_postal_code": 40111}, [], Decimal("165000"))
    resp = LiveBiteship("biteship_test.key").create_order(req)
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer biteship_test.key"
    body = json.loads(sent.content)
    assert body["reference_id"] == "grp-1" and body["destination_cash_on_delivery"] == 165000
    assert resp == {"ref": "bs1", "reference_id": "grp-1", "waybill_id": "WB1", "price": "12000",
                    "cod_amount": "165000", "status": "confirmed"}
    respx.post("https://api.biteship.com/v1/orders").mock(
        return_value=httpx.Response(400, json={"code": 40002060, "error": "duplicate reference_id"}))
    with pytest.raises(Rejected):
        LiveBiteship("biteship_test.key").create_order(req)


def test_factory_defaults_to_mocks_without_live_account():
    assert isinstance(factory.payment_provider(None), MockDoku)
    assert isinstance(factory.carrier(None), MockBiteship)
