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

    def post_message(self, conversation_id: int, content: str, message_type: str, private: bool = False) -> int: ...

    def open_for_human(self, conversation_id: int, reason: str) -> None: ...


class MockChatwoot:
    def __init__(self):
        self.conversations: dict[str, int] = {}
        self.messages: list[dict] = []
        self.opened: list[tuple[int, str]] = []

    def ensure_conversation(self, inbox_identifier, contact_ref, name):
        return self.conversations.setdefault(contact_ref, len(self.conversations) + 1)

    def post_message(self, conversation_id, content, message_type, private=False):
        self.messages.append({"conversation_id": conversation_id, "content": content, "type": message_type,
                              "private": private})
        return len(self.messages)

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

    def post_message(self, conversation_id, content, message_type, private=False):
        r = httpx.post(self._url(f"/conversations/{conversation_id}/messages"), headers=self.h, timeout=15,
                       json={"content": content, "message_type": message_type, "private": private})
        r.raise_for_status()
        return r.json()["id"]

    def open_for_human(self, conversation_id, reason):
        self.post_message(conversation_id, f"AI handoff: {reason}", "outgoing", private=True)
        httpx.post(self._url(f"/conversations/{conversation_id}/toggle_status"), headers=self.h, timeout=15,
                   json={"status": "open"}).raise_for_status()
