"""Server-computed quotes. The only way an AI or a human creates a WhatsApp order.

Authority limits (docs/06-design-revisions.md R5) are enforced here, not in prompts:
* only SKUs listed on the conversation's WhatsApp account, in active portfolios linked to it;
* prices come from the listing; discounts above the listing limit are refused (AI) or need approval;
* owned stock is held (TTL) only when available-to-promise covers the quantity;
* dropship lines are offered only when an approved supplier offer was verified recently, in stock and
  under the ceiling; otherwise the quote is ``pending_availability`` and promises nothing;
* COD is allowed only for owned-stock lines and customers not blocked for COD.
"""

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.catalog import PlatformListing, Sku, SupplierOffer
from app.models.customers import Customer
from app.models.orders import Quote, QuoteLine
from app.models.org import Portfolio
from app.models.platforms import AccountPortfolioLink, PlatformAccount
from app.modules import inventory


@dataclass
class QuotePolicy:
    hold_ttl: timedelta = timedelta(hours=2)
    validity: timedelta = timedelta(hours=2)
    dropship_freshness: timedelta = timedelta(hours=24)
    flat_shipping_fee: Decimal = Decimal("15000")
    ai_max_discount_rate: Decimal = Decimal("0.05")
    max_lines: int = 10
    max_qty_per_line: int = 20


class PolicyDenied(HTTPException):
    def __init__(self, code: str, detail: str):
        super().__init__(status.HTTP_422_UNPROCESSABLE_ENTITY, {"code": code, "detail": detail})
        self.code = code


@dataclass
class QuoteRequestLine:
    sku_code: str
    qty: int
    discount_rate: Decimal = Decimal("0")


@dataclass
class QuoteResult:
    quote: Quote
    lines: list[QuoteLine] = field(default_factory=list)


def _rupiah(v: Decimal) -> str:
    return "Rp" + f"{int(v.quantize(Decimal('1'), ROUND_HALF_UP)):,}".replace(",", ".")


def build_quote(session: Session, *, platform_account_id: uuid.UUID, customer_id: uuid.UUID,
                conversation_id: uuid.UUID | None, request: list[QuoteRequestLine], payment_method: str,
                created_by: str, policy: QuotePolicy | None = None) -> QuoteResult:
    policy = policy or QuotePolicy()
    account = session.get(PlatformAccount, platform_account_id)
    customer = session.get(Customer, customer_id)
    if account is None or customer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account or customer not visible")
    if not request or len(request) > policy.max_lines:
        raise PolicyDenied("invalid_lines", f"between 1 and {policy.max_lines} lines required")
    if payment_method not in ("doku_link", "manual_transfer", "cod"):
        raise PolicyDenied("payment_method", f"unsupported payment method {payment_method}")
    if payment_method == "cod" and customer.cod_blocked:
        raise PolicyDenied("cod_blocked", "COD is not available for this customer")
    linked = set(session.scalars(select(AccountPortfolioLink.portfolio_id).where(
        AccountPortfolioLink.platform_account_id == account.id)))

    quote = Quote(organization_id=account.organization_id, brand_id=customer.brand_id, platform_account_id=account.id,
                  conversation_id=conversation_id, customer_id=customer.id, valid_until=utcnow() + policy.validity,
                  subtotal=Decimal("0"), discount_total=Decimal("0"), shipping_fee=Decimal("0"), total=Decimal("0"),
                  payment_method=payment_method, created_by=created_by, customer_message="")
    session.add(quote)
    session.flush()
    out: list[QuoteLine] = []
    for req in request:
        if req.qty < 1 or req.qty > policy.max_qty_per_line:
            raise PolicyDenied("qty_out_of_range", f"qty must be 1..{policy.max_qty_per_line}")
        sku = session.scalars(select(Sku).where(Sku.code == req.sku_code, Sku.active.is_(True))).first()
        if sku is None:
            raise PolicyDenied("unknown_sku", f"SKU {req.sku_code} is not sold")
        listing = session.scalars(select(PlatformListing).where(
            PlatformListing.platform_account_id == account.id, PlatformListing.sku_id == sku.id,
            PlatformListing.active.is_(True), PlatformListing.portfolio_id.in_(linked))).first()
        if listing is None:
            raise PolicyDenied("not_listed_on_channel", f"SKU {req.sku_code} is not listed on this WhatsApp channel")
        portfolio = session.get(Portfolio, listing.portfolio_id)
        if portfolio is None or portfolio.status != "active":
            raise PolicyDenied("portfolio_inactive", "portfolio is not active")
        limit = listing.max_discount_rate if created_by == "human" else min(listing.max_discount_rate,
                                                                              policy.ai_max_discount_rate)
        if req.discount_rate < 0 or req.discount_rate > limit:
            raise PolicyDenied("discount_exceeds_limit",
                               f"discount {req.discount_rate} above allowed {limit}; human approval required")
        gross = listing.price * req.qty
        line_total = (gross * (1 - req.discount_rate)).quantize(Decimal("0.01"))

        source = "unavailable"
        if sku.inventory_policy in ("owned", "hybrid"):
            atp = inventory.available_to_promise(session, listing.portfolio_id, sku.id, sku.safety_stock)
            if atp >= req.qty:
                inventory.reserve(session, account.organization_id, portfolio_id=listing.portfolio_id, sku_id=sku.id,
                                  qty=req.qty, kind="quote_hold", quote_id=quote.id, ttl=policy.hold_ttl,
                                  safety_stock=sku.safety_stock)
                source = "owned"
        if source == "unavailable" and sku.inventory_policy in ("dropship_only", "hybrid"):
            offer = session.scalars(select(SupplierOffer).where(
                SupplierOffer.sku_id == sku.id, SupplierOffer.portfolio_id == listing.portfolio_id,
                SupplierOffer.approved.is_(True), SupplierOffer.last_observed_in_stock.is_(True),
                SupplierOffer.last_observed_at >= utcnow() - policy.dropship_freshness,
                SupplierOffer.last_observed_price <= SupplierOffer.price_ceiling)
                .order_by(SupplierOffer.priority, SupplierOffer.last_observed_price)).first()
            if offer is not None:
                source = "dropship"
        if source == "dropship" and payment_method == "cod":
            raise PolicyDenied("cod_not_allowed_for_dropship", "dropship items must be prepaid")
        line = QuoteLine(organization_id=account.organization_id, quote_id=quote.id, portfolio_id=listing.portfolio_id,
                         sku_id=sku.id, listing_id=listing.id, qty=req.qty, unit_price=listing.price,
                         discount_rate=req.discount_rate, line_total=line_total, availability_source=source)
        session.add(line)
        out.append(line)
        quote.subtotal += gross
        quote.discount_total += gross - line_total

    quote.shipping_fee = policy.flat_shipping_fee
    quote.total = quote.subtotal - quote.discount_total + quote.shipping_fee
    pending = any(ln.availability_source == "unavailable" for ln in out)
    quote.status = "pending_availability" if pending else "offered"
    if pending:
        inventory.release(session, list(_holds(session, quote.id)))
    quote.policy_snapshot = {"hold_ttl_h": policy.hold_ttl.total_seconds() / 3600,
                             "ai_max_discount_rate": str(policy.ai_max_discount_rate),
                             "dropship_freshness_h": policy.dropship_freshness.total_seconds() / 3600}
    quote.customer_message = render_message(session, quote, out)
    session.flush()
    return QuoteResult(quote, out)


def _holds(session: Session, quote_id: uuid.UUID):
    from app.models.inventory import InventoryReservation

    return session.scalars(select(InventoryReservation).where(
        InventoryReservation.quote_id == quote_id, InventoryReservation.status == "active")).all()


def render_message(session: Session, quote: Quote, lines: list[QuoteLine]) -> str:
    rows = []
    for ln in lines:
        sku = session.get(Sku, ln.sku_id)
        disc = f" (diskon {ln.discount_rate * 100:.0f}%)" if ln.discount_rate else ""
        rows.append(f"- {sku.name} x{ln.qty}: {_rupiah(ln.line_total)}{disc}")
    if quote.status == "pending_availability":
        return ("Terima kasih! Ketersediaan beberapa produk sedang kami cek terlebih dahulu. "
                "Admin kami akan mengonfirmasi harga dan stok sebelum pesanan dibuat.\n" + "\n".join(rows))
    return ("Rincian pesanan:\n" + "\n".join(rows) + f"\nOngkir: {_rupiah(quote.shipping_fee)}"
            + f"\nTotal: {_rupiah(quote.total)}"
            + f"\nPenawaran berlaku sampai {quote.valid_until:%H:%M} WIB. Balas 'SETUJU' untuk melanjutkan.")


def quoted_amounts(quote: Quote, lines: list[QuoteLine]) -> set[Decimal]:
    """Amounts the AI is allowed to state to the customer for this quote."""
    vals = {quote.subtotal, quote.total, quote.shipping_fee, quote.discount_total}
    for ln in lines:
        vals |= {ln.unit_price, ln.line_total}
    return {v.quantize(Decimal("1"), ROUND_HALF_UP) for v in vals if v is not None}
