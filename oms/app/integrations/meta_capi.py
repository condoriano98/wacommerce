"""Meta Conversions API for Business Messaging — mock by default (spec §13).

Documented facts used (2026-11-09 research, docs/04): events go to ``POST /{dataset_id}/events`` with
``action_source=business_messaging`` and ``messaging_channel=whatsapp``; Meta does **not** deduplicate these events, so
the OMS never re-sends an event whose delivery is uncertain (ADR-015). Identifiers are hashed (SHA-256) except
``ctwa_clid``. Indonesia hashed-identifier fallback and ``test_event_code`` support are unverified.
"""

from typing import Protocol

import httpx

from app.core.config import get_settings
from app.core.external_ops import NotSent, Rejected
from app.integrations import mockstore


class CapiSender(Protocol):
    def send_event(self, dataset_id: str, event: dict) -> dict: ...


class MockMetaCapi:
    def send_event(self, dataset_id: str, event: dict) -> dict:
        with mockstore.state("meta_capi") as st:
            st.setdefault("events", []).append({"dataset_id": dataset_id, **event})
            n = len(st["events"])
        mockstore.fault_after_accept("meta_capi", "send_event")
        return {"ref": f"fbtrace-{n}", "events_received": 1}

    @staticmethod
    def received() -> list[dict]:
        with mockstore.state("meta_capi") as st:
            return list(st.get("events", []))


class LiveMetaCapi:
    def __init__(self, access_token: str, api_version: str | None = None):
        self.token, self.version = access_token, api_version or get_settings().meta_graph_version

    def send_event(self, dataset_id: str, event: dict) -> dict:
        try:
            r = httpx.post(f"https://graph.facebook.com/{self.version}/{dataset_id}/events", timeout=20,
                           json={"data": [event]}, headers={"Authorization": f"Bearer {self.token}"})
        except httpx.ConnectError as exc:
            raise NotSent(str(exc)) from exc
        if 400 <= r.status_code < 500:
            raise Rejected(f"{r.status_code} {r.text[:300]}")
        r.raise_for_status()
        data = r.json()
        return {"ref": data.get("fbtrace_id"), "events_received": data.get("events_received")}
