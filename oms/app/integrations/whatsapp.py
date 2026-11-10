"""WhatsApp Cloud API adapter: webhook parsing and message sending (mock + live).

Payload shapes follow the installed ``whatsapp-cloud-api`` skill and Meta docs (Graph version configurable,
default v27.0). BSUIDs (``from_user_id`` / ``contacts[].user_id``) are captured; phone (``from``/``wa_id``)
may be absent for username-only users.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import httpx

from app.core.config import get_settings


@dataclass
class InboundMessage:
    phone_number_id: str
    wamid: str
    timestamp: datetime
    msg_type: str
    text: str | None
    wa_id: str | None
    bsuid: str | None
    profile_name: str | None
    referral: dict | None
    interactive_reply_id: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class StatusUpdate:
    phone_number_id: str
    wamid: str
    status: str
    timestamp: datetime
    errors: list = field(default_factory=list)


def parse_webhook(payload: dict) -> tuple[list[InboundMessage], list[StatusUpdate]]:
    messages, statuses = [], []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value", {})
            pnid = value.get("metadata", {}).get("phone_number_id", "")
            contacts = value.get("contacts", [])
            for m in value.get("messages", []):
                contact = next((c for c in contacts if c.get("wa_id") == m.get("from")
                                or c.get("user_id") == m.get("from_user_id")), contacts[0] if contacts else {})
                text = None
                reply_id = None
                if m.get("type") == "text":
                    text = m.get("text", {}).get("body")
                elif m.get("type") == "interactive":
                    inter = m.get("interactive", {})
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    text, reply_id = reply.get("title"), reply.get("id")
                elif m.get("type") == "button":
                    text, reply_id = m.get("button", {}).get("text"), m.get("button", {}).get("payload")
                messages.append(InboundMessage(
                    phone_number_id=pnid, wamid=m["id"],
                    timestamp=datetime.fromtimestamp(int(m.get("timestamp", "0")), UTC),
                    msg_type=m.get("type", "unknown"), text=text,
                    wa_id=m.get("from") or contact.get("wa_id"),
                    bsuid=m.get("from_user_id") or contact.get("user_id"),
                    profile_name=contact.get("profile", {}).get("name"),
                    referral=m.get("referral"), interactive_reply_id=reply_id, raw=m))
            for s in value.get("statuses", []):
                statuses.append(StatusUpdate(pnid, s["id"], s.get("status", ""),
                                             datetime.fromtimestamp(int(s.get("timestamp", "0")), UTC),
                                             s.get("errors", [])))
    return messages, statuses


class WhatsAppSender(Protocol):
    def send_text(self, phone_number_id: str, to: dict, body: str) -> str: ...

    def send_template(self, phone_number_id: str, to: dict, template: str, language: str, params: list[str]) -> str: ...


class MockWhatsAppSender:
    sent: list[dict] = []

    def send_text(self, phone_number_id: str, to: dict, body: str) -> str:
        wamid = f"wamid.mock.{len(self.sent) + 1}"
        self.sent.append({"type": "text", "pnid": phone_number_id, "to": to, "body": body, "wamid": wamid})
        return wamid

    def send_template(self, phone_number_id, to, template, language, params) -> str:
        wamid = f"wamid.mock.{len(self.sent) + 1}"
        self.sent.append({"type": "template", "pnid": phone_number_id, "to": to, "template": template,
                          "params": params, "wamid": wamid})
        return wamid


class LiveWhatsAppSender:
    """Live Cloud API client. Recipient is addressed by BSUID ("recipient") when no phone is known."""

    def __init__(self, access_token: str, api_version: str | None = None):
        self.token = access_token
        self.version = api_version or get_settings().meta_graph_version

    def _post(self, phone_number_id: str, body: dict) -> str:
        r = httpx.post(f"https://graph.facebook.com/{self.version}/{phone_number_id}/messages", json=body,
                       headers={"Authorization": f"Bearer {self.token}"}, timeout=20)
        r.raise_for_status()
        return r.json()["messages"][0]["id"]

    @staticmethod
    def _to(to: dict) -> dict:
        return {"to": to["phone"]} if to.get("phone") else {"recipient": to["bsuid"]}

    def send_text(self, phone_number_id: str, to: dict, body: str) -> str:
        return self._post(phone_number_id, {"messaging_product": "whatsapp", **self._to(to), "type": "text",
                                            "text": {"body": body[:4096]}})

    def send_template(self, phone_number_id, to, template, language, params) -> str:
        comps = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}] if params else []
        return self._post(phone_number_id, {"messaging_product": "whatsapp", **self._to(to), "type": "template",
                                            "template": {"name": template, "language": {"code": language},
                                                         "components": comps}})
