import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgMixin


class LedgerAccount(Base):
    """Global chart of accounts (see docs/06-design-revisions.md R2)."""

    __tablename__ = "ledger_accounts"
    code: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    type: Mapped[str]  # asset | liability | equity | revenue | expense
    is_cash: Mapped[bool] = mapped_column(default=False)
    is_working_capital: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True)


class LedgerEntry(OrgMixin, Base):
    """Immutable double-entry lines. A deferred constraint trigger rejects any journal whose
    debits and credits do not balance at commit."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("debit >= 0 and credit >= 0", name="non_negative"),
        CheckConstraint("(debit = 0) <> (credit = 0)", name="one_sided"),
        UniqueConstraint("journal_id", "line_no"),
    )
    journal_id: Mapped[uuid.UUID]
    line_no: Mapped[int]
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    bank_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bank_accounts.id"))
    event_time: Mapped[datetime | None]  # operational time of the business event (accounting_date may differ)
    posting_rule_version: Mapped[int] = mapped_column(default=2)
    account_code: Mapped[str] = mapped_column(ForeignKey("ledger_accounts.code"))
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"), index=True)
    platform_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("platform_accounts.id"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"), index=True)
    order_item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("order_items.id"))
    debit: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    currency: Mapped[str] = mapped_column(default="IDR")
    accounting_date: Mapped[date]
    source_type: Mapped[str]  # order_delivered | settlement | refund | ad_spend | capital_transfer ...
    source_id: Mapped[str]
    memo: Mapped[str | None]
    reverses_journal_id: Mapped[uuid.UUID | None]


class Settlement(OrgMixin, Base):
    """Money arriving from a third party: marketplace payout, courier COD remittance, gateway disbursement."""

    __tablename__ = "settlements"
    __table_args__ = (UniqueConstraint("platform_account_id", "external_id"),)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"), index=True)
    kind: Mapped[str]  # marketplace_payout | cod_remittance | gateway_disbursement
    external_id: Mapped[str]
    period_start: Mapped[date | None]
    period_end: Mapped[date | None]
    gross_amount: Mapped[Decimal]
    fees_amount: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    net_amount: Mapped[Decimal]
    paid_at: Mapped[datetime | None]
    bank_reference: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="received")  # received | reconciled | mismatch
    data_source: Mapped[str] = mapped_column(default="api")
    content_sha256: Mapped[str | None]  # same id + same content = replay; same id + other content = flagged


class SettlementLine(OrgMixin, Base):
    __tablename__ = "settlement_lines"
    settlement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("settlements.id"), index=True)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"), index=True)
    external_order_id: Mapped[str | None]
    shipment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("shipments.id"))
    line_type: Mapped[str]  # sale | commission | transaction_fee | shipping | cod_fee | adjustment | refund | tax
    amount: Mapped[Decimal]  # signed: + increases payout, - deducts
    expected_amount: Mapped[Decimal | None]
    match_status: Mapped[str] = mapped_column(default="unmatched")  # matched | unmatched | variance
    variance: Mapped[Decimal | None]
    line_no: Mapped[int]  # position in the provider report; (settlement_id, line_no) unique, rows immutable


class OperatingExpense(OrgMixin, Base):
    __tablename__ = "operating_expenses"
    __table_args__ = (CheckConstraint("amount > 0"),)
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"), index=True)
    category: Mapped[str]  # software | salaries | rent | other
    description: Mapped[str]
    amount: Mapped[Decimal]
    incurred_on: Mapped[date]
    allocation_rule: Mapped[str] = mapped_column(default="direct")  # direct | revenue_share | equal


class AccountingPeriod(OrgMixin, Base):
    """Monthly periods per legal entity. Posting into a closed period is refused by the database trigger."""

    __tablename__ = "accounting_periods"
    __table_args__ = (
        UniqueConstraint("legal_entity_id", "period_start"),
        CheckConstraint("status in ('open','soft_closed','closed')"),
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("legal_entities.id"))
    period_start: Mapped[date]
    period_end: Mapped[date]
    status: Mapped[str] = mapped_column(default="open")
    closed_by: Mapped[uuid.UUID | None]
    closed_at: Mapped[datetime | None]
    snapshot_hash: Mapped[str | None]


class BankStatementLine(OrgMixin, Base):
    """Bank evidence imported from statements (CSV/API). The only thing that can make a manual payment bank_matched."""

    __tablename__ = "bank_statement_lines"
    __table_args__ = (UniqueConstraint("bank_account_id", "fingerprint"),)
    bank_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bank_accounts.id"), index=True)
    booked_at: Mapped[date]
    amount: Mapped[Decimal]  # signed: + credit to our account
    reference: Mapped[str | None]
    counterparty: Mapped[str | None]
    import_batch: Mapped[str]
    fingerprint: Mapped[str]  # sha256(account, date, amount, reference, sequence) for duplicate prevention
    matched_type: Mapped[str | None]
    matched_id: Mapped[str | None]


class ReconciliationRun(OrgMixin, Base):
    __tablename__ = "reconciliation_runs"
    kind: Mapped[str]  # bank_manual | gateway | cod | marketplace | supplier | stock | ads
    period_start: Mapped[date | None]
    period_end: Mapped[date | None]
    source: Mapped[str]
    status: Mapped[str] = mapped_column(default="open")


class ReconciliationItem(OrgMixin, Base):
    __tablename__ = "reconciliation_items"
    # Identity (migration 0025): unique NULLS NOT DISTINCT (organization_id, kind, platform_account_id, expected_ref).
    __table_args__ = (
        CheckConstraint("status in ('open','matched','variance','accepted','written_off')"),
    )
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"), index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reconciliation_runs.id"))
    kind: Mapped[str]
    expected_ref: Mapped[str]
    expected_amount: Mapped[Decimal]
    expected_at: Mapped[date]
    actual_ref: Mapped[str | None]
    actual_amount: Mapped[Decimal | None]
    difference: Mapped[Decimal | None]
    evidence: Mapped[dict] = mapped_column(default=dict)
    operator_user_id: Mapped[uuid.UUID | None]
    classification: Mapped[str | None]  # timing | fee_variance | missing | duplicate | unknown
    status: Mapped[str] = mapped_column(default="open")
    exception_case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("exception_cases.id"))
    resolved_at: Mapped[datetime | None]
    platform_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("platform_accounts.id"))
    identity_quarantined: Mapped[bool] = mapped_column(default=False)  # legacy item without a provable account
