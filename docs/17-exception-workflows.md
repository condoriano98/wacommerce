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
| `oversold_marketplace` | Native marketplace order cannot reserve shared stock | Case; platform SLA timer | Source from supplier · cancel on platform | Purchase approval | As dropship / cancellation | Fulfilled or canceled on platform |
| `manual_review` | Router cannot decide a fulfillment group | Group held | Route manually · cancel & refund | Refund approval | — | Group routed |
| `bank_unmatched` | Attested/captured payment without a matched bank or disbursement line after T+2 business days | Exposure recorded; order continues per release policy | Match manually · chase gateway/bank · reverse payment | Variance acceptance above threshold | Reverse 2200/1000 if payment invalid | Bank line matched or reversed |
| `stock_count_variance` | Physical count ≠ system balance | Balance flagged | Adjust · recount | Inventory write-off tier | Dr 5500 / Cr 1200 (or reverse) | Adjusted and approved |
| `ad_spend_unallocated` | Ad spend without a campaign→portfolio allocation for > 7 days | Kept in unallocated marketing | Allocate (effective-dated) · keep unallocated | — | Reclass within open period only | Allocated or accepted |

V3.1 additions to every case: link to the `reconciliation_item` (if any), **financial exposure** (Rp at risk), **ageing
bucket** (0–2, 3–7, 8–14, >14 days), and full approval history. Dashboard exposure totals equal the sum of open
reconciliation differences.

General rules
- Containment is automatic; money-moving resolutions (refunds, write-offs, re-purchases, accepting variances above
  thresholds) always need a `human_approvals` row naming the approver and amount.
- Each case keeps an append-only decision log; SLA breaches raise dashboard alerts and n8n notifications.
- Customer communication uses OMS templates sent through the WhatsApp gateway (mirrored into Chatwoot).
