"""Chatwoot adapter for an **API channel** inbox (docs/06-design-revisions.md R1).

Chatwoot never talks to Meta. The OMS mirrors inbound/outbound WhatsApp messages into Chatwoot and receives
human agents' replies from the API-channel webhook, then sends them through the OMS WhatsApp gateway.
"""

import hashlib
import hmac
from typing import Protocol

import httpx


def verify_chatwoot_signature(secret: str, raw_body: bytes, timestamp: str | None, signature: str | None) -> bool:
    """Chatwoot signs webhooks with HMAC-SHA256 over "{timestamp}.{body}" (header X-Chatwoot-Signature: sha256=...)."""
    if not timestamp or not signature:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.removeprefix("sha256="), expected)


class ChatwootClient(Protocol):
    def ensure_conversation(self, inbox_identifier: str, contact_ref: str, name: str | None) -> int: ...

    def post_message(self, conversation_id: int, content: str, message_type: str, private: bool = False, *,
                     source_id: str | None = None) -> int: ...

    def find_by_source_id(self, conversation_id: int, source_id: str) -> dict | None: ...

    def open_for_human(self, conversation_id: int, reason: str) -> None: ...


class MockChatwoot:
    """Mock API-channel inbox. Like the real one (G-CW1, documentation-verified), every message created through the API
    is echoed to the inbox webhook — ``echo_payload`` builds that callback for tests."""

    def __init__(self):
        self.opened: list[tuple[int, str]] = []

    @property
    def messages(self) -> list[dict]:
        from app.integrations import mockstore

        with mockstore.state("chatwoot") as st:
            return list(st.get("messages", []))

    def ensure_conversation(self, inbox_identifier, contact_ref, name):
        from app.integrations import mockstore

        with mockstore.state("chatwoot") as st:
            convs = st.setdefault("conversations", {})
            if contact_ref not in convs:
                convs[contact_ref] = 1000 + len(convs) + 1
            return convs[contact_ref]

    def post_message(self, conversation_id, content, message_type, private=False, *, source_id=None):
        from app.integrations import mockstore

        with mockstore.state("chatwoot") as st:
            msgs = st.setdefault("messages", [])
            mid = 50000 + len(msgs) + 1
            msgs.append({"id": mid, "conversation_id": conversation_id, "content": content,
                         "message_type": message_type, "private": private, "source_id": source_id})
        mockstore.fault_after_accept("chatwoot", "post_message")
        return mid

    def find_by_source_id(self, conversation_id, source_id):
        hit = next((m for m in self.messages if m["source_id"] == source_id), None)
        return {"ref": hit["id"]} if hit else None

    @staticmethod
    def echo_payload(m: dict, inbox_id: int) -> dict:
        return {"event": "message_created", "id": m["id"], "content": m["content"],
                "message_type": m["message_type"], "private": m["private"], "source_id": m["source_id"],
                "sender": {"type": "agent_bot" if m["source_id"] else "user"},
                "conversation": {"id": m["conversation_id"]}, "inbox": {"id": inbox_id}}

    def open_for_human(self, conversation_id, reason):
        self.opened.append((conversation_id, reason))


class LiveChatwoot:
    def __init__(self, base_url: str, account_id: int, api_token: str, inbox_id: int):
        self.base, self.account, self.inbox = base_url.rstrip("/"), account_id, inbox_id
        self.h = {"api_access_token": api_token}

    def _url(self, path: str) -> str:
        return f"{self.base}/api/v1/accounts/{self.account}{path}"

    def ensure_conversation(self, inbox_identifier, contact_ref, name):
        r = httpx.post(self._url("/contacts"), headers=self.h, timeout=15,
                       json={"inbox_id": self.inbox, "name": name or contact_ref, "identifier": contact_ref})
        contact = r.json()["payload"]["contact"] if r.status_code < 300 else httpx.get(
            self._url("/contacts/search"), headers=self.h, params={"q": contact_ref}).json()["payload"][0]
        source_id = contact["contact_inboxes"][0]["source_id"] if contact.get("contact_inboxes") else contact_ref
        conv = httpx.post(self._url("/conversations"), headers=self.h, timeout=15,
                          json={"source_id": source_id, "inbox_id": self.inbox, "contact_id": contact["id"],
                                "status": "pending"})
        conv.raise_for_status()
        return conv.json()["id"]

    def post_message(self, conversation_id, content, message_type, private=False, *, source_id=None):
        body = {"content": content, "message_type": message_type, "private": private}
        if source_id:  # echo marker (G-CW1); also kept in content_attributes for older versions
            body |= {"source_id": source_id, "content_attributes": {"oms_source_id": source_id}}
        r = httpx.post(self._url(f"/conversations/{conversation_id}/messages"), headers=self.h, timeout=15, json=body)
        r.raise_for_status()
        return r.json()["id"]

    def find_by_source_id(self, conversation_id, source_id):
        r = httpx.get(self._url(f"/conversations/{conversation_id}/messages"), headers=self.h, timeout=15)
        r.raise_for_status()
        for m in r.json().get("payload", []):
            if m.get("source_id") == source_id or (m.get("content_attributes") or {}).get("oms_source_id") == source_id:
                return {"ref": m["id"]}
        return None

    def open_for_human(self, conversation_id, reason):
        self.post_message(conversation_id, f"AI handoff: {reason}", "outgoing", private=True)
        httpx.post(self._url(f"/conversations/{conversation_id}/toggle_status"), headers=self.h, timeout=15,
                   json={"status": "open"}).raise_for_status()
