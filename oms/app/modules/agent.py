"""AI Sales Admin tool gateway (spec §14, docs/16): the only way the AI (via n8n) acts on the OMS.

Authority matrix — enforced here, never by prompts:
* **allowed**      read-only: ``catalog.search``, ``order.status``, ``conversation.handoff``
* **conditional**  OMS-validated: ``quote.create`` (listing prices, AI discount cap, availability),
                   ``order.confirm`` (requires customer acceptance evidence), ``payment_link.create`` (own order only),
                   ``conversation.reply`` (reply validator + 24h window)
* **human only**   ``refund.create``, ``procurement.purchase``, ``price.change``, ``discount.override``,
                   ``capital.reallocate``, ``ads.budget_change``, ``credentials.change``, ``ledger.adjust`` — always
                   denied for the AI and escalated.

Per-tool contract: actor = AI service principal (role ``ai_sales_agent``, portfolio-scoped by RLS); context =
organization + conversation; parameters validated by a strict schema (unknown fields rejected); idempotency through the
underlying services (quote holds, accept_quote, payment idempotency keys); every call — allowed or denied — is written
to ``agent_execution_logs`` (denials in their own transaction so they survive the request rollback); failure is closed:
a denial returns a reason and, where useful, a handoff recommendation. Customer, marketplace and supplier text are
data: tools take structured parameters only and nothing in a message can change permissions, prices or limits.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.authz import Principal
from app.core.db import tenant_session
from app.models.base import utcnow
from app.models.catalog import PlatformListing, Sku
from app.models.customers import Conversation, Message
from app.models.governance import AgentExecutionLog
from app.models.orders import Order, Quote, QuoteLine
from app.models.org import RiskPolicy
from app.modules import messaging, orders, payments, quotes

AGENT = "ai_sales_admin"
HUMAN_ONLY = {"refund.create", "procurement.purchase", "price.change", "discount.override", "capital.reallocate",
              "ads.budget_change", "credentials.change", "ledger.adjust"}
ACCEPT_TEXT = re.compile(r"^\s*(setuju|ya,?\s*setuju|oke,?\s*lanjut|ok\s*lanjut|deal)\W*\s*$", re.IGNORECASE)
AMOUNT = re.compile(r"(?:rp\.?\s*)(\d{1,3}(?:[.,]\d{3})+|\d+)", re.IGNORECASE)
FORBIDDEN_PROMISES = [
    (re.compile(r"\b(refund|uang\s+kembali|dana\s+kembali)\b", re.IGNORECASE), "refund_promise"),
    (re.compile(r"\b(gratis|free)\s+ongkir\b", re.IGNORECASE), "free_shipping_promise"),
    (re.compile(r"\b(pasti|dijamin|garansi)\s+(sampai|tiba)\b|\bbesok\s+(sampai|tiba)\b", re.IGNORECASE),
     "delivery_date_promise"),
]


@dataclass(frozen=True)
class AgentLimits:
    calls_per_conversation_per_day: int = 60
    confirmations_per_org_per_day: int = 200


LIMITS = AgentLimits()


class AgentDenied(HTTPException):
    def __init__(self, code: str, detail: str, *, escalate: bool = False):
        super().__init__(status.HTTP_403_FORBIDDEN, {"code": code, "detail": detail, "escalate": escalate})
        self.code = code
        self.escalate = escalate


# ---- strict parameter schemas (extra fields rejected: injected keys like "price" or "role" never pass) ----
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CatalogSearch(_Strict):
    query: str = Field(default="", max_length=100)


class QuoteLineIn(_Strict):
    sku_code: str = Field(max_length=64)
    qty: int = Field(ge=1, le=20)
    discount_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1)


class QuoteCreate(_Strict):
    lines: list[QuoteLineIn] = Field(min_length=1, max_length=10)
    payment_method: str = Field(pattern="^(doku_link|manual_transfer|cod)$")


class OrderConfirm(_Strict):
    quote_id: uuid.UUID
    evidence_message_id: uuid.UUID
    shipping_address: dict


class PaymentLink(_Strict):
    order_id: uuid.UUID


class OrderStatus(_Strict):
    order_id: uuid.UUID


class Reply(_Strict):
    text: str = Field(min_length=1, max_length=2000)


class Handoff(_Strict):
    reason: str = Field(max_length=300)


SCHEMAS: dict[str, type[BaseModel]] = {
    "catalog.search": CatalogSearch, "quote.create": QuoteCreate, "order.confirm": OrderConfirm,
    "payment_link.create": PaymentLink, "order.status": OrderStatus, "conversation.reply": Reply,
    "conversation.handoff": Handoff,
}


def _log(session: Session, principal: Principal, conv_id, tool: str, request: dict, decision: str,
         reason: str | None, response: dict | None = None) -> None:
    session.add(AgentExecutionLog(organization_id=principal.org_id, agent=AGENT, conversation_id=conv_id, tool=tool,
                                  request=_jsonable(request), decision=decision, reason=reason,
                                  response=_jsonable(response or {})))


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_jsonable(x) for x in v]
    if isinstance(v, uuid.UUID | Decimal):
        return str(v)
    return v


def _log_denial(principal: Principal, conv_id, tool: str, request: dict, code: str) -> None:
    """Denials are recorded in their own transaction: the request transaction is rolled back by the 403."""
    with tenant_session(principal.org_id, principal.user_id) as s:
        _log(s, principal, conv_id, tool, request, "denied", code)


def run_tool(session: Session, principal: Principal, conversation_id: uuid.UUID, tool: str, params: dict) -> dict:
    try:
        if not (principal.is_service and any(a.role == "ai_sales_agent" for a in principal.assignments)):
            raise AgentDenied("not_an_agent_principal", "agent tools are for the AI service principal only")
        conv = _gate(session, principal, conversation_id, tool)
        if tool not in SCHEMAS:
            raise AgentDenied("unknown_tool", f"tool {tool!r} is not available to the AI")
        try:
            args = SCHEMAS[tool].model_validate(params)
        except ValidationError as exc:
            raise AgentDenied("invalid_parameters", exc.errors(include_url=False, include_context=False)[0]["msg"]) \
                from exc
        result = TOOLS[tool](session, principal, conv, args)
    except quotes.PolicyDenied as exc:
        _log_denial(principal, conversation_id, tool, params, exc.code)
        raise AgentDenied(exc.code, str(exc.detail), escalate=True) from exc
    except AgentDenied as exc:
        _log_denial(principal, conversation_id, tool, params, exc.code)
        raise
    except HTTPException as exc:  # service-level refusal (expired quote, window closed, ...)
        code = exc.detail.get("code") if isinstance(exc.detail, dict) else str(exc.detail)
        _log_denial(principal, conversation_id, tool, params, str(code)[:200])
        raise AgentDenied("service_refused", str(exc.detail), escalate=True) from exc
    _log(session, principal, conversation_id, tool, params, "allowed", None, result)
    return result


def _gate(session: Session, principal: Principal, conversation_id: uuid.UUID, tool: str) -> Conversation:
    if tool in HUMAN_ONLY:
        raise AgentDenied("requires_human_approval", f"{tool} is a human-only action", escalate=True)
    kill = session.scalars(select(RiskPolicy).where(RiskPolicy.key == "ai_agent_enabled",
                                                    RiskPolicy.portfolio_id.is_(None))).first()
    if kill is not None and kill.active and kill.threshold == 0:
        raise AgentDenied("kill_switch", "AI agent disabled for this organization", escalate=True)
    conv = session.get(Conversation, conversation_id)
    if conv is None:  # RLS: the AI only sees conversations of customers in its portfolios
        raise AgentDenied("conversation_not_visible", "conversation not found")
    if not conv.ai_enabled:
        raise AgentDenied("human_handoff_active", "a human agent owns this conversation")
    calls = _usage(session, conv.id, None)
    if calls >= LIMITS.calls_per_conversation_per_day:
        raise AgentDenied("conversation_limit", "daily tool-call limit reached", escalate=True)
    return conv


def _usage(session: Session, conversation_id: uuid.UUID | None, tool: str | None) -> int:
    """Calls in the last 24 h via the ``agent_usage`` definer (the AI cannot read the log table itself)."""
    return session.execute(text("select agent_usage(:c, :t, :s)"),
                           {"c": conversation_id, "t": tool, "s": utcnow() - timedelta(days=1)}).scalar()


# ---- tools ----
def _catalog(session, principal, conv, a: CatalogSearch) -> dict:
    rows = session.execute(select(Sku.code, Sku.name, PlatformListing.price).join(
        PlatformListing, PlatformListing.sku_id == Sku.id).where(
        PlatformListing.platform_account_id == conv.platform_account_id,
        Sku.name.ilike(f"%{a.query}%") if a.query else True)).all()
    return {"items": [{"sku_code": c, "name": n, "price": str(p)} for c, n, p in rows]}


def _quote(session, principal, conv, a: QuoteCreate) -> dict:
    res = quotes.build_quote(session, platform_account_id=conv.platform_account_id, customer_id=conv.customer_id,
                             conversation_id=conv.id, request=[quotes.QuoteRequestLine(x.sku_code, x.qty,
                                                                                       x.discount_rate)
                                                               for x in a.lines],
                             payment_method=a.payment_method, created_by="ai")
    return {"quote_id": str(res.quote.id), "status": res.quote.status, "total": str(res.quote.total),
            "customer_message": res.quote.customer_message}


def acceptance_evidence(session: Session, conv: Conversation, quote: Quote, message_id: uuid.UUID) -> bool:
    """Customer accepted *this* quote: an inbound customer message in the same conversation, after the quote was
    offered, that is an explicit acceptance (text pattern or interactive reply ``accept_quote:<id>``)."""
    m = session.get(Message, message_id)
    if m is None or m.conversation_id != conv.id or m.direction != "inbound" or m.author != "customer":
        return False
    if (m.sent_at or m.created_at) < quote.created_at.replace(microsecond=0):  # Meta timestamps are whole seconds
        return False
    interactive = (m.payload.get("interactive") or {}).get("button_reply", {}).get("id") \
        or (m.payload.get("button") or {}).get("payload")
    if interactive:
        return interactive == f"accept_quote:{quote.id}"
    return bool(ACCEPT_TEXT.match(m.body or ""))


def _confirm(session, principal, conv, a: OrderConfirm) -> dict:
    quote = session.get(Quote, a.quote_id)
    if quote is None or quote.conversation_id != conv.id:
        raise AgentDenied("quote_not_in_conversation", "quote does not belong to this conversation")
    if not acceptance_evidence(session, conv, quote, a.evidence_message_id):
        raise AgentDenied("no_acceptance_evidence", "customer acceptance not evidenced", escalate=True)
    if _usage(session, None, "order.confirm") >= LIMITS.confirmations_per_org_per_day:
        raise AgentDenied("daily_confirmation_limit", "daily AI order limit reached", escalate=True)
    order = orders.accept_quote(session, quote.id, a.shipping_address, actor_type="ai", actor_user_id=principal.user_id)
    return {"order_id": str(order.id), "order_number": order.order_number, "total": str(order.total)}


def _payment_link(session, principal, conv, a: PaymentLink) -> dict:
    order = session.get(Order, a.order_id)
    if order is None or order.customer_id != conv.customer_id:
        raise AgentDenied("order_not_in_conversation", "order does not belong to this customer")
    p = payments.request_doku_link(session, order)
    return {"payment_id": str(p.id), "invoice_number": p.provider_ref, "status": "link_requested"}


def _status(session, principal, conv, a: OrderStatus) -> dict:
    order = session.get(Order, a.order_id)
    if order is None or order.customer_id != conv.customer_id:
        raise AgentDenied("order_not_in_conversation", "order does not belong to this customer")
    return {"order_number": order.order_number, "status": order.status, "payment_status": order.payment_status}


def allowed_amounts(session: Session, conv: Conversation) -> set[Decimal]:
    vals: set[Decimal] = set(session.scalars(select(PlatformListing.price).where(
        PlatformListing.platform_account_id == conv.platform_account_id)))
    for q in session.scalars(select(Quote).where(Quote.conversation_id == conv.id,
                                                 Quote.status.in_(("offered", "accepted")))):
        vals |= quotes.quoted_amounts(q, list(session.scalars(select(QuoteLine).where(QuoteLine.quote_id == q.id))))
    for o in session.scalars(select(Order).where(Order.customer_id == conv.customer_id)):
        vals.add(o.total)
    return {v.quantize(Decimal("1")) for v in vals}


def validate_reply(session: Session, conv: Conversation, text_: str) -> None:
    for rx, code in FORBIDDEN_PROMISES:
        if rx.search(text_):
            raise AgentDenied(code, "reply makes a promise outside policy", escalate=True)
    allowed = allowed_amounts(session, conv)
    for raw in AMOUNT.findall(text_):
        amount = Decimal(re.sub(r"[.,]", "", raw))
        if amount not in allowed:
            raise AgentDenied("amount_not_quoted", f"amount {amount} is not a listed price or quoted amount")


def _reply(session, principal, conv, a: Reply) -> dict:
    validate_reply(session, conv, a.text)
    msg = messaging.queue_outbound(session, conv, a.text, author="ai")
    return {"message_id": str(msg.id), "status": msg.status}


def _handoff(session, principal, conv, a: Handoff) -> dict:
    messaging.handoff(session, conv, a.reason)
    return {"handoff": True}


TOOLS = {"catalog.search": _catalog, "quote.create": _quote, "order.confirm": _confirm,
         "payment_link.create": _payment_link, "order.status": _status, "conversation.reply": _reply,
         "conversation.handoff": _handoff}
