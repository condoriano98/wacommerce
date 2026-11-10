"""Risk-alert notification channels (remediation E2).

Only the mocked channel exists until a live channel (n8n webhook, WhatsApp template to the owner, e-mail) is
authorised with credentials; ``channel_for`` refuses anything else. Payloads carry identifiers and amounts only —
never customer data or secrets.

Mock fault injection (tests only): ``MockNotifier.fail_next(n)`` makes the next ``n`` sends raise ``ConnectionError``.
"""

import uuid
from typing import Protocol

from app.integrations import mockstore


class NotificationChannel(Protocol):
    name: str

    def send(self, payload: dict) -> dict: ...


class MockNotifier:
    name = "mock"

    def send(self, payload: dict) -> dict:
        with mockstore.state("notify") as st:
            if st.get("fail_remaining", 0) > 0:
                st["fail_remaining"] -= 1
                raise ConnectionError("mock notification channel unavailable")
            ref = f"ntf-{uuid.uuid4().hex[:12]}"
            st.setdefault("sent", []).append({**payload, "ref": ref})
        return {"ref": ref}

    @staticmethod
    def fail_next(n: int) -> None:
        with mockstore.state("notify") as st:
            st["fail_remaining"] = n

    @staticmethod
    def sent() -> list[dict]:
        with mockstore.state("notify") as st:
            return list(st.get("sent", []))


def channel_for(name: str) -> NotificationChannel:
    if name == "mock":
        return MockNotifier()
    raise ValueError(f"notification channel {name!r} is not enabled (no authorised credentials)")
