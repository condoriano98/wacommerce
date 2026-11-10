# 17 — Exception Workflows (SOP + system behaviour)

Every exception is an `exception_cases` row: `type, severity, status (open → in_progress → awaiting_approval → resolved |
written_off), owner_role, sla_due_at, order_id/shipment_id/settlement_id, decision, resolution_note`.
Playbooks live in `oms/app/modules/exceptions/playbooks.py`; this document is the human-readable SOP.

| Type | Trigger | Automatic containment | Human decision options | Approval | Ledger treatment | Close when |
|---|---|---|---|---|---|---|
| `delivery_failed` | Courier status failed / on_hold > 48h | Order → `exception`; customer notified; stop COD Purchase event | Re-attempt · return to sender · cancel | None (re-attempt) / Finance (cancel prepaid → refund) | Extra shipping → 5100; if RTS see below | Delivered or RTS received |
| `returned_to_sender` | Courier `returned` | Inventory `in_transit_return` | Restock · write off damaged | Inventory write-off > Rp500k | Reverse revenue if recognised (4900); restock at cost (1200) or loss (5500) | Item received + inspected |
| `cod_refused` | COD delivery refused | As RTS; flag customer risk score | Block COD for customer · allow | — | Return shipping → 5500 | RTS closed |
| `lost_or_damaged` | Courier lost / damage report | Claim task opened | File courier claim · replace · refund | Replacement or refund approval | Loss 5500; claim proceeds reduce loss | Claim settled or written off |
| `refund_requested` | Customer/agent request | Order flagged; no auto refund | Full · partial · reject | Finance/Portfolio Manager by amount tier | Dr 4900 Cr 2100; on payout Dr 2100 Cr 1000 | Refund paid & confirmed |
| `cod_settlement_mismatch` | Remittance ≠ expected COD − fees | Hold receivable difference | Dispute with courier · accept variance | Variance > Rp50k | Accepted variance → 5200 | Matched or written off |
| `cod_not_remitted` | Delivered COD not remitted after SLA (D+7) | Escalate weekly | Dispute | — | Remains 1100 receivable (aged) | Remitted |
| `supplier_out_of_stock` | Copilot observation stock < qty | Procurement blocked | Alternative approved supplier · wait · cancel & refund | Alternative supplier if landed cost ↑ | — | Purchased or order canceled |
| `supplier_price_drift` | Observed price > ceiling | Procurement blocked | Accept new price · alternative · cancel | Portfolio Manager (margin impact shown) | — | Decision recorded |
| `supplier_canceled` | Marketplace cancels purchase | Supplier prepayment → refund receivable | Re-buy · cancel | Re-buy approval | Dr receivable Cr 1210 | Refund received |
| `dropship_late` | No tracking/delivery past promised date | Customer update | Chase supplier · cancel & refund | Refund approval | — | Delivered/refunded |
| `wrong_item` | Customer report with evidence | Case + evidence | Replace · partial refund · return | Refund/replacement approval | As refund/RTS | Customer resolved |
| `payment_mismatch` | Underpaid / overpaid / duplicate payment | Order not released | Request balance · refund excess | Refund approval | Overpayment → 2100 | Balanced |
| `marketplace_settlement_mismatch` | Settlement ≠ expected net | Hold difference | Dispute with platform · accept | Variance > threshold | Variance → 5300 | Matched or accepted |
| `duplicate_order` | Same customer+items within window | Second order held | Merge · keep both · cancel | — | — | Decision recorded |

General rules
- Containment is automatic; money-moving resolutions (refunds, write-offs, re-purchases, accepting variances above
  thresholds) always need a `human_approvals` row naming the approver and amount.
- Each case keeps an append-only decision log; SLA breaches raise dashboard alerts and n8n notifications.
- Customer communication uses OMS templates sent through the WhatsApp gateway (mirrored into Chatwoot).
