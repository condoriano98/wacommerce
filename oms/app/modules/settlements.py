"""Settlements and refunds (spec §9, §11, §16).

* Gateway disbursement (DOKU payout) clears 1010 to bank and books any fee not yet known.
* Biteship COD remittance: the COD fund report is dashboard/CSV-only (G-BS1, verified 2026-11-10 as no documented
  API) → imported rows clear 1100 into the carrier balance; withdrawals move it to bank.
* Marketplace payout: clears 1110, trues up fees, books withheld tax (PPh 22) as prepaid tax.
* Refunds: approval-backed obligations; payout is a human bank action recorded with evidence.
* Returns after delivery: quantities, restock or loss, return shipping.
"""

import uuid
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.authz import Principal
from app.models.base import utcnow
from app.models.finance import LedgerEntry, Settlement, SettlementLine
from app.models.fulfillment import FulfillmentGroup, Shipment
from app.models.orders import Order, OrderItem, Payment, Refund
from app.modules import approvals, events, exceptions, inventory, orders, reconciliation
from app.modules.finance import accounts as A
from app.modules.finance import posting


def _content_hash(lines) -> str:
    import hashlib
    import json

    return hashlib.sha256(json.dumps(lines, sort_keys=True, default=str).encode()).hexdigest()


def _settlement(session: Session, org_id, account_id, kind: str, external_id: str, gross, fees, net,
                source: str = "api", lines=None) -> tuple[Settlement, bool]:
    """Settlement identity = (provider account, external id). A replay with the same content is a no-op; the same id
    with different content is never re-posted — it opens a case (round 2, P0-3). Concurrent replays insert once."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    digest = _content_hash(lines) if lines is not None else None
    new_id = session.execute(pg_insert(Settlement).values(
        id=uuid.uuid4(), created_at=utcnow(), organization_id=org_id, platform_account_id=account_id, kind=kind,
        external_id=external_id, gross_amount=gross, fees_amount=fees, net_amount=net, paid_at=utcnow(),
        data_source=source, status="received", content_sha256=digest)
        .on_conflict_do_nothing(index_elements=["platform_account_id", "external_id"])
        .returning(Settlement.id)).scalar()
    s = session.scalars(select(Settlement).where(Settlement.platform_account_id == account_id,
                                                 Settlement.external_id == external_id)).one()
    if new_id is None:
        if digest and s.content_sha256 and s.content_sha256 != digest:
            exceptions.open_case(session, org_id, "marketplace_settlement_mismatch" if kind == "marketplace_payout"
                                 else "cod_settlement_mismatch", subject_type="settlement", subject_id=str(s.id),
                                 context={"reason": "same settlement id reported again with different content",
                                          "external_id": external_id, "stored": s.content_sha256, "new": digest})
        return s, False
    if lines is not None:
        for n, ln in enumerate(lines, start=1):
            session.add(SettlementLine(organization_id=org_id, settlement_id=s.id, platform_account_id=account_id,
                                       line_no=n, external_order_id=ln.get("external_order_id"),
                                       line_type=ln.get("line_type", "sale" if kind == "marketplace_payout"
                                                        else "cod_remittance"),
                                       amount=Decimal(str(ln.get("net", ln.get("amount_remitted", 0))))))
        session.flush()
    return s, True


def expect_bank_receipt(session: Session, org_id, legal_entity_id, kind: str, token: str, amount: Decimal,
                        sources: list[tuple[str, str]], portfolio_id=None, platform_account_id=None) -> None:
    """A provider reported a payout (booked to 1050). It becomes verified cash only when a bank statement line of the
    same legal entity, carrying ``token`` in its reference and the exact amount, is matched (remediation B5)."""
    if amount <= 0:
        return
    reconciliation.expect(session, org_id, "bank_payout", f"{kind}:{token}", amount, portfolio_id=portfolio_id,
                          platform_account_id=platform_account_id,
                          evidence={"token": token, "currency": "IDR",  # all provider payouts are IDR (G-ADS1/FX)
                                    "legal_entity_id": str(legal_entity_id) if legal_entity_id else None,
                                    "sources": [list(x) for x in sources]})


def gateway_disbursement(session: Session, principal: Principal, account_id, external_id: str,
                         payments: list[tuple[uuid.UUID, Decimal]]) -> Settlement:
    """payments: [(payment_id, net amount received for it)]."""
    principal.require("finance.manage")
    gross = Decimal("0")
    fees_unbooked = Decimal("0")
    for pid, net in payments:
        p = session.get(Payment, pid)
        gross += p.amount
        booked_fee = Decimal(session.scalar(select(func.coalesce(func.sum(LedgerEntry.debit), 0)).where(
            LedgerEntry.source_type == "gateway_fee", LedgerEntry.source_id == str(p.id),
            LedgerEntry.account_code == A.PAYMENT_FEES)))
        fees_unbooked += p.amount - booked_fee - net
    s, new = _settlement(session, principal.org_id, account_id, "gateway_disbursement", external_id, gross,
                         fees_unbooked, gross - fees_unbooked)
    if not new:
        return s
    first = session.get(Order, session.get(Payment, payments[0][0]).order_id)
    total_net = sum((n for _, n in payments), Decimal("0"))
    # 1010 holds amount − fees already booked; it is cleared by net received + fees not yet booked (F1, F9).
    le = posting.entity_for_order(session, first)
    posting.gateway_disbursement(session, principal.org_id, le, str(s.id),
                                 total_net + fees_unbooked, fees_unbooked, platform_account_id=account_id)
    expect_bank_receipt(session, principal.org_id, le, "gateway", external_id, total_net,
                        [("gateway_disbursement", str(s.id))], platform_account_id=account_id)
    for pid, net in payments:
        p = session.get(Payment, pid)
        if p.status == "captured":
            p.status = "settled"
        reconciliation.match(session, principal.org_id, "gateway", str(pid), actual_ref=str(s.id), actual_amount=net,
                             evidence={"settlement": external_id})
    return s


def cod_remittance(session: Session, principal: Principal, carrier_account_id, report_id: str,
                   rows: list[dict]) -> Settlement:
    """rows from the Biteship COD fund report: {shipment_external_id, amount_remitted}. CSV import (G-BS1).

    Round 2: shipments are found by reference **within this carrier account** (ambiguous → case); each order is
    locked; a shipment's COD receivable is cleared at most up to what is still expected for it — a remittance reported
    again under a new report id, or above the expected amount, goes to 2400 suspense with a case. Lines carry the
    order, so every order's 1100 visibly clears."""
    principal.require("finance.manage")
    total = sum((Decimal(str(r["amount_remitted"])) for r in rows), Decimal("0"))
    s, new = _settlement(session, principal.org_id, carrier_account_id, "cod_remittance", report_id, total,
                         Decimal("0"), total, source="csv", lines=rows)
    if not new:
        return s
    by_order: dict = {}
    le = None
    for r in rows:
        hits = session.scalars(select(Shipment).where(
            Shipment.external_id == r["shipment_external_id"],
            (Shipment.carrier_account_id == carrier_account_id) | Shipment.carrier_account_id.is_(None))).all()
        if len(hits) != 1:
            exceptions.open_case(session, principal.org_id, "cod_settlement_mismatch", subject_type="settlement",
                                 subject_id=f"{s.id}:{r['shipment_external_id']}",
                                 context={"reason": "unknown or ambiguous shipment reference", "matches": len(hits)})
            continue
        sh = hits[0]
        order = session.get(Order, sh.order_id, with_for_update=True)
        amt = Decimal(str(r["amount_remitted"]))
        item = None
        try:
            item = reconciliation.find(session, principal.org_id, "cod", str(sh.id))
        except reconciliation.AmbiguousReference:
            pass
        expected = item.expected_amount if item is not None else Decimal(session.scalar(
            select(func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0)).where(
                LedgerEntry.order_id == order.id, LedgerEntry.account_code == A.COD_RECEIVABLE)))
        already = item.actual_amount or Decimal("0") if item is not None else Decimal("0")
        applied = max(min(amt, expected - already), Decimal("0"))
        excess = amt - applied
        key = (sh.portfolio_id, order.id)
        a0, e0 = by_order.get(key, (Decimal("0"), Decimal("0")))
        by_order[key] = (a0 + applied, e0 + excess)
        le = le or posting.entity_for_order(session, order)
        if excess > 0:
            exceptions.open_case(session, principal.org_id, "cod_settlement_mismatch", subject_type="settlement",
                                 subject_id=f"{s.id}:{sh.id}", order_id=order.id,
                                 context={"reason": "remittance exceeds the COD still expected for this shipment",
                                          "suspense": str(excess), "report": report_id})
        if item is not None and applied > 0:
            reconciliation.accumulate(session, item, actual_ref=str(s.id), amount=applied, final=True,
                                      operator_user_id=principal.user_id,
                                      entry={"report": report_id, "settlement": str(s.id), "amount": str(applied)})
    if by_order:
        posting.cod_remitted(session, principal.org_id, le, str(s.id), by_order, carrier_account_id)
    return s


def carrier_withdrawal(session: Session, principal: Principal, carrier_account_id, withdrawal_id: str,
                       gross: Decimal, fee: Decimal, legal_entity_id) -> None:
    principal.require("finance.manage")
    posting.carrier_withdrawal(session, principal.org_id, legal_entity_id, withdrawal_id, gross, fee,
                               carrier_account_id)
    expect_bank_receipt(session, principal.org_id, legal_entity_id, "carrier_withdrawal", withdrawal_id, gross - fee,
                        [("carrier_withdrawal", withdrawal_id)], platform_account_id=carrier_account_id)


def _balance_item(session: Session, account: str, item_id) -> Decimal:
    return Decimal(session.scalar(select(func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0)).where(
        LedgerEntry.account_code == account, LedgerEntry.order_item_id == item_id)))


def _sum_debits(session: Session, account: str, item_id, source_type: str) -> Decimal:
    return Decimal(session.scalar(select(func.coalesce(func.sum(LedgerEntry.debit), 0)).where(
        LedgerEntry.account_code == account, LedgerEntry.order_item_id == item_id,
        LedgerEntry.source_type == source_type)))


def marketplace_payout(session: Session, principal: Principal, account_id, payout_id: str,
                       lines: list[dict]) -> Settlement:
    """lines (normalized by the marketplace adapter or CSV import): {external_order_id, gross, fee, tax_withheld, net,
    final?}. ``final`` (default true) says the provider will pay nothing more for that order — Shopee escrow releases an
    order once (docs/12). Per order, under a row lock (concurrent payouts serialise), lines are aggregated and
    allocated to items by item gross (B4). Per item (round 2, P0-3):

    * accrued-fee portion = fee accrued at delivery × (gross share ÷ item gross) — partial payouts true up pro rata;
    * the payout explains ``net + tax + (fee share − accrued portion)`` of the receivable;
    * the receivable 1110 is cleared by at most what is still outstanding; anything beyond is **excess** held in 2400
      suspense with a case. An order already fully cleared books the whole payout to suspense (no fee, tax or
      receivable effect again): **a receivable is never cleared twice**;
    * reconciliation accumulates the value received (net + creditable tax) across payouts; a final payout decides
      matched vs variance (fee true-ups and short payouts stay visible as cases);
    * the order's settlement status is derived from ledger, reconciliation and cases (``derive_settlement_status``)."""
    principal.require("finance.manage")
    gross = sum((Decimal(str(ln["gross"])) for ln in lines), Decimal("0"))
    fees = sum((Decimal(str(ln["fee"])) for ln in lines), Decimal("0"))
    net = sum((Decimal(str(ln["net"])) for ln in lines), Decimal("0"))
    s, new = _settlement(session, principal.org_id, account_id, "marketplace_payout", payout_id, gross, fees, net,
                         lines=lines)
    if not new:
        return s
    by_order: dict[str, dict] = {}
    for ln in lines:
        agg = by_order.setdefault(ln["external_order_id"], {k: Decimal("0") for k in ("gross", "fee", "tax", "net")}
                                  | {"final": True})
        agg["gross"] += Decimal(str(ln["gross"]))
        agg["fee"] += Decimal(str(ln["fee"]))
        agg["tax"] += Decimal(str(ln.get("tax_withheld", 0)))
        agg["net"] += Decimal(str(ln["net"]))
        agg["final"] = bool(ln.get("final", True))
    sources, paid, le = [], Decimal("0"), None
    for ext, agg in by_order.items():
        order = session.scalars(select(Order).where(Order.platform_account_id == account_id,
                                                    Order.external_order_id == ext).with_for_update()).first()
        if order is None:
            exceptions.open_case(session, principal.org_id, "marketplace_settlement_mismatch",
                                 subject_type="settlement", subject_id=f"{s.id}:{ext}",
                                 context={"reason": "settlement line for unknown order"})
            continue
        items = orders.items_of(session, order.id)
        weights = [i.line_total for i in items]
        shares = {k: posting.split(agg[k], weights) for k in ("gross", "fee", "tax", "net")}
        outstanding_total = sum((max(_balance_item(session, A.MARKETPLACE_RECEIVABLE, i.id), Decimal("0"))
                                 for i in items), Decimal("0"))
        duplicate = outstanding_total <= 0
        allocations, excess_total = [], Decimal("0")
        for n, item in enumerate(items):
            g, fee, tax, net_i = shares["gross"][n], shares["fee"][n], shares["tax"][n], shares["net"][n]
            if duplicate:  # nothing owed any more: the whole payout is unexplained money
                allocations.append(posting.MarketplaceItemSettlement(
                    item=item, net_paid=net_i, withheld_tax=Decimal("0"), fee_true_up=Decimal("0"),
                    applied=Decimal("0"), excess=net_i))
                excess_total += net_i
                continue
            item_gross = _sum_debits(session, A.MARKETPLACE_RECEIVABLE, item.id, "delivered_revenue")
            accrued = _sum_debits(session, A.MARKETPLACE_FEES, item.id, "marketplace_fee_accrual")
            portion = posting.q(accrued * g / item_gross) if item_gross else accrued
            true_up = fee - portion
            explained = net_i + tax + true_up
            outstanding = max(_balance_item(session, A.MARKETPLACE_RECEIVABLE, item.id), Decimal("0"))
            applied = min(explained, outstanding) if explained > 0 else explained
            excess = explained - applied
            excess_total += excess
            allocations.append(posting.MarketplaceItemSettlement(
                item=item, net_paid=net_i, withheld_tax=tax, fee_true_up=true_up, applied=applied, excess=excess))
        posting.marketplace_settled(session, order, f"{s.id}:{order.id}", allocations,
                                    platform_account_id=account_id)
        sources.append(("marketplace_settled", f"{s.id}:{order.id}"))
        paid += agg["net"]
        le = le or posting.entity_for_order(session, order)
        if excess_total > 0:
            exceptions.open_case(session, principal.org_id, "marketplace_settlement_mismatch",
                                 subject_type="settlement", subject_id=f"{s.id}:{order.id}", order_id=order.id,
                                 context={"reason": "duplicate payout for a cleared order" if duplicate
                                          else "payout exceeds the outstanding receivable",
                                          "suspense": str(excess_total), "payout": payout_id})
        if not duplicate:
            item_rec = _recon_item(session, order)
            if item_rec is not None:
                reconciliation.accumulate(session, item_rec, actual_ref=str(s.id), amount=agg["net"] + agg["tax"],
                                          final=agg["final"], operator_user_id=principal.user_id,
                                          entry={"payout": payout_id, "settlement": str(s.id), "net": str(agg["net"]),
                                                 "fee": str(agg["fee"]), "tax_withheld": str(agg["tax"]),
                                                 "final": agg["final"]})
        derive_settlement_status(session, order)
        orders.derive_status(session, order)
    expect_bank_receipt(session, principal.org_id, le, "marketplace", payout_id, paid, sources,
                        platform_account_id=account_id)
    return s


def _recon_item(session: Session, order: Order):
    try:
        return reconciliation.find(session, order.organization_id, "marketplace", str(order.id),
                                   platform_account_id=order.platform_account_id)
    except reconciliation.AmbiguousReference:
        return None


def derive_settlement_status(session: Session, order: Order) -> str:
    """Settlement state from the books (round 2, P0-3), never set by hand:
    none / expected (receivable, no payout yet) / partially_settled (receivable left, more payouts expected) /
    matched (receivable cleared, no variance, nothing in suspense) / variance (fee true-up, short final payout or
    suspense) / under_review (a case is being worked) / adjusted (variance accepted with approval) / closed (matched or
    adjusted, cases resolved and the payout proven by the bank)."""
    from app.models.governance import ExceptionCase

    def bal(account: str) -> Decimal:
        return Decimal(session.scalar(select(func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0))
                                      .where(LedgerEntry.order_id == order.id, LedgerEntry.account_code == account)))

    def has(source_type: str) -> bool:
        return session.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.order_id == order.id, LedgerEntry.source_type == source_type)) > 0

    receivable, suspense = bal(A.MARKETPLACE_RECEIVABLE), -bal(A.SETTLEMENT_SUSPENSE)
    item = _recon_item(session, order)
    cases = session.scalars(select(ExceptionCase.status).where(
        ExceptionCase.order_id == order.id, ExceptionCase.type == "marketplace_settlement_mismatch",
        ExceptionCase.status.not_in(("resolved", "written_off")))).all()
    if item is not None and item.exception_case_id:
        case = session.get(ExceptionCase, item.exception_case_id)
        if case is not None and case.status not in ("resolved", "written_off"):
            cases = [*cases, case.status]
    if not has("marketplace_settled") and not has("settlement_adjustment"):
        new = "expected" if receivable > 0 else "none"
    elif has("settlement_adjustment") and receivable == 0 and suspense <= 0:
        new = "adjusted" if cases else "closed"
    elif any(c in ("in_progress", "awaiting_approval") for c in cases):
        new = "under_review"
    elif suspense > 0 or receivable < 0 or (item is not None and item.status == "variance"):
        new = "variance"
    elif receivable > 0:
        new = "partially_settled"
    else:
        in_transit = bal(A.FUNDS_IN_TRANSIT)
        new = "closed" if has("bank_receipt") and in_transit == 0 and not cases else "matched"
    order.settlement_status = new
    return new


def accept_settlement_variance(session: Session, principal: Principal, order_id: uuid.UUID,
                               approval_id: uuid.UUID) -> str:
    """Controlled adjustment (approval ``variance_acceptance``): the receivable the provider will not pay is written
    to marketplace fees/claims; the reconciliation item is accepted and its case resolved. Audited."""
    principal.require("finance.manage")
    approvals.require_approved(session, approval_id, "variance_acceptance", str(order_id))
    order = session.get(Order, order_id, with_for_update=True)
    total = Decimal("0")
    for item in orders.items_of(session, order.id):
        remaining = _balance_item(session, A.MARKETPLACE_RECEIVABLE, item.id)
        if remaining > 0:
            posting.settlement_adjustment(session, order, item, remaining, f"{order.id}:{approval_id}")
            total += remaining
    item_rec = _recon_item(session, order)
    if item_rec is not None and item_rec.status in ("open", "variance"):
        item_rec.status, item_rec.resolved_at = "accepted", utcnow()
        item_rec.evidence = {**item_rec.evidence, "accepted": {"approval": str(approval_id), "amount": str(total)}}
    from app.models.governance import ExceptionCase

    open_cases = session.scalars(select(ExceptionCase).where(
        ExceptionCase.status.not_in(("resolved", "written_off")),
        ((ExceptionCase.order_id == order.id) & (ExceptionCase.type == "marketplace_settlement_mismatch"))
        | (ExceptionCase.id == (item_rec.exception_case_id if item_rec else None)))).all()
    for case in open_cases:
        exceptions.resolve(session, case, actor_user_id=principal.user_id,
                           note=f"variance accepted with approval {approval_id}")
    audit(session, principal.org_id, "settlement.variance_accepted", "order", order.id,
          actor_user_id=principal.user_id, after={"approval": str(approval_id), "adjusted": str(total)})
    new = derive_settlement_status(session, order)
    orders.derive_status(session, order)
    return new


@events.subscribe("shipment_delivered")
def _expect_settlement(session: Session, data: dict) -> None:
    order, sh = data["order"], data["shipment"]
    g = session.get(FulfillmentGroup, sh.group_id)
    if order.payment_method == "cod":
        # Only this shipment's own deductions (keyed by shipment id), never other shipments of the same order.
        deducted = Decimal(session.scalar(select(func.coalesce(func.sum(LedgerEntry.credit), 0)).where(
            LedgerEntry.order_id == order.id, LedgerEntry.account_code == A.COD_RECEIVABLE,
            LedgerEntry.source_type.in_(("cod_fee", "carrier_cost")), LedgerEntry.source_id == str(sh.id))))
        reconciliation.expect(session, order.organization_id, "cod", str(sh.id), sh.cod_amount - deducted,
                              platform_account_id=sh.carrier_account_id,
                              portfolio_id=g.portfolio_id)
    elif order.payment_method == "marketplace":
        expected = Decimal(session.scalar(select(func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0))
                                          .where(LedgerEntry.order_id == order.id,
                                                 LedgerEntry.account_code == A.MARKETPLACE_RECEIVABLE)))
        portfolios = {i.portfolio_id for i in orders.items_of(session, order.id)}
        # A multi-portfolio order's settlement is an org-level reconciliation item (not one portfolio's).
        reconciliation.expect(session, order.organization_id, "marketplace", str(order.id), expected,
                              platform_account_id=order.platform_account_id,
                              portfolio_id=g.portfolio_id if len(portfolios) == 1 else None)
        reconciliation.raise_expectation(session, order.organization_id, "marketplace", str(order.id), expected,
                                         platform_account_id=order.platform_account_id)


# --- refunds and returns ----------------------------------------------------------------------------------------


def request_refund(session: Session, principal: Principal, order_id: uuid.UUID, item_id: uuid.UUID,
                   amount: Decimal, reason: str) -> Refund:
    order = session.get(Order, order_id)
    item = session.get(OrderItem, item_id)
    if principal.is_service:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "the AI cannot request refunds")
    if not (principal.can("refund.request", item.portfolio_id) or principal.can("refund.approve", item.portfolio_id)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "refund authority required")
    refund = Refund(organization_id=order.organization_id, order_id=order.id, amount=amount, reason=reason,
                    requested_by=principal.user_id, method=str(item.id))
    session.add(refund)
    session.flush()
    refund.approval_id = approvals.request_approval(
        session, principal, kind="refund", subject_type="refund", subject_id=str(refund.id), amount=amount,
        portfolio_id=item.portfolio_id, idempotency_key=f"refund:{refund.id}",
        payload={"order": order.order_number, "item": str(item.id), "reason": reason}).id
    return refund


def approve_refund_obligation(session: Session, principal: Principal, refund_id: uuid.UUID) -> Refund:
    """After approval: post the obligation (revenue reduction if delivered, advance release otherwise)."""
    refund = session.get(Refund, refund_id, with_for_update=True)
    approvals.require_approved(session, refund.approval_id, "refund", str(refund.id))
    order = session.get(Order, refund.order_id)
    item = session.get(OrderItem, uuid.UUID(refund.method))
    if item.qty_delivered > 0:
        posting.refund_after_delivery(session, order, item, refund.amount, str(refund.id))
    refund.status = "approved"
    return refund


def mark_refund_paid(session: Session, principal: Principal, refund_id: uuid.UUID, bank_reference: str) -> Refund:
    """A human paid the refund from the bank (OMS never moves money); recorded with evidence."""
    principal.require("finance.manage")
    refund = session.get(Refund, refund_id, with_for_update=True)
    if refund.status != "approved":
        raise HTTPException(status.HTTP_409_CONFLICT, f"refund is {refund.status}")
    order = session.get(Order, refund.order_id)
    item = session.get(OrderItem, uuid.UUID(refund.method))
    posting.refund_paid(session, order, item, refund.amount, str(refund.id))
    refund.status, refund.paid_at = "paid", utcnow()
    item.qty_refunded = min(item.qty, item.qty_refunded + max(1, int(refund.amount // max(item.unit_price, 1))))
    audit(session, principal.org_id, "refund.paid", "refund", refund.id, actor_user_id=principal.user_id,
          portfolio_id=item.portfolio_id, after={"amount": refund.amount, "bank_reference": bank_reference})
    orders.derive_status(session, order)
    return refund


def receive_return(session: Session, principal: Principal, order_id: uuid.UUID, item_id: uuid.UUID, qty: int, *,
                   resellable: bool, warehouse_id, return_shipping_cost: Decimal) -> None:
    order = session.get(Order, order_id)
    item = session.get(OrderItem, item_id, with_for_update=True)
    principal.require("fulfillment.operate", item.portfolio_id)
    if qty <= 0 or item.qty_returned + qty > item.qty_delivered:
        raise HTTPException(status.HTTP_409_CONFLICT, "cannot return more than delivered")
    item.qty_returned += qty
    # The exact cost of what was delivered for this line (per-warehouse allocations), not the last pickup's cost.
    unit_cost = inventory.delivered_unit_cost(session, item.id) or item.unit_cost or Decimal("0")
    if unit_cost:
        inventory.restock_return(session, order.organization_id, portfolio_id=item.portfolio_id, sku_id=item.sku_id,
                                 warehouse_id=warehouse_id, qty=qty, unit_cost=unit_cost, order_id=order.id,
                                 resellable=resellable)
        posting.return_received(session, order, item, qty, unit_cost, resellable=resellable,
                                source_id=f"{item.id}:{item.qty_returned}")
    if return_shipping_cost:
        posting.return_shipping(session, order, item, return_shipping_cost, f"{item.id}:{item.qty_returned}")
    orders.derive_status(session, order)
    if order.status == "returned":
        events.emit(session, "order_returned", order=order)


def credit_loss(session: Session, principal: Principal, payment_id: uuid.UUID, kind: str,
                approval_id: uuid.UUID | None = None) -> None:
    """F13: allowance / release / write_off (approval) / recovery for release-on-attestation receivables."""
    principal.require("finance.manage")
    p = session.get(Payment, payment_id)
    order = session.get(Order, p.order_id)
    items = orders.items_of(session, order.id)
    if kind == "write_off":
        approvals.require_approved(session, approval_id, "ledger_adjustment", str(payment_id))
    shares = posting.split(p.amount, [i.line_total for i in items])
    for item, share in zip(items, shares, strict=True):
        posting.credit_loss(session, order, item, share, kind, f"{payment_id}:{item.id}")
