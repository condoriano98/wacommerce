"""M4 review findings (2026-11-10): in-file and concurrent CSV duplicates (P0), registry never reports ``available``
without a passed live check (P1), refresh-token crash after provider success (P1), ad-spend financial boundaries (P1)."""

import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text

import app.registry  # noqa: F401
from app.core.db import tenant_session
from app.integrations import mockstore
from app.integrations.ads import MockAds
from app.models.base import utcnow
from app.models.finance import LedgerEntry, Settlement
from app.models.governance import ExceptionCase, ExternalOperation
from app.models.platforms import IntegrationCapability, PlatformAccount
from app.modules import channel_sync, imports, platforms
from app.modules.finance import accounts as A
from app.modules.finance import posting
from tests.conftest import owner_session
from tests.test_m4_channels import _delivered_marketplace_order
from tests.test_m4_imports_ads import _ads_account
from tests.world import build_world, principal

D = Decimal


@pytest.fixture(autouse=True)
def _fresh():
    mockstore.reset()
    yield
    mockstore.reset()


def _settlement_csv(sn: str, repeat: int) -> str:
    return "payout_id,external_order_id,gross,fee,tax_withheld,net\n" + \
        f"PO-{sn},{sn},120000,9600,0,110400\n" * repeat


def test_p0_duplicate_lines_within_one_file_are_applied_once(tenant):
    w = build_world(tenant)
    _delivered_marketplace_order(tenant, w, "DUP-1")
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        res = imports.run_import(s, fin, "marketplace_settlement", "dup.csv", _settlement_csv("DUP-1", 2),
                                 dry_run=False, platform_account_id=w.shopee_seller)
    assert (res["duplicates_in_file"], res["claimed"], res["applied"]) == (1, 1, 1)
    with owner_session() as s:
        st = s.scalars(select(Settlement).where(Settlement.external_id == "PO-DUP-1")).one()
        assert st.net_amount == D("110400") and st.gross_amount == D("120000")  # header = sum of distinct lines
        assert s.execute(text("select ri.status from reconciliation_items ri join orders o on o.id::text = "
                              "ri.expected_ref where o.external_order_id = 'DUP-1'")).scalar() == "matched"


def test_p0_concurrent_uploads_of_the_same_file_apply_exactly_once(tenant):
    w = build_world(tenant)
    _delivered_marketplace_order(tenant, w, "CONC-1")
    fin = principal(tenant, "finance")
    barrier, results, errors = threading.Barrier(2), [], []

    def upload(name):
        barrier.wait()
        try:
            with tenant_session(tenant.org_id, fin.user_id) as s:
                results.append(imports.run_import(s, fin, "marketplace_settlement", name,
                                                  _settlement_csv("CONC-1", 1), dry_run=False,
                                                  platform_account_id=w.shopee_seller))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=upload, args=(f"conc-{i}.csv",)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert sorted(r["applied"] for r in results) == [0, 1]
    with owner_session() as s:
        assert s.scalar(select(func.count()).select_from(Settlement).where(
            Settlement.external_id == "PO-CONC-1")) == 1


def test_p1_credentials_alone_are_verification_pending_never_available(tenant):
    w = build_world(tenant)
    owner = principal(tenant, "owner")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        platforms.store_credential(s, owner, w.shopee_seller, "access_token", "at-1")
    with owner_session() as s:
        s.get(PlatformAccount, w.shopee_seller).live_enabled = True
    res = channel_sync.sync_orders(tenant.org_id, w.shopee_seller, utcnow() - timedelta(days=1), utcnow())
    assert res["status"] == "not_authorized"  # live adapters stay refused
    with owner_session() as s:
        caps = s.scalars(select(IntegrationCapability).where(
            IntegrationCapability.platform_account_id == w.shopee_seller)).all()
        assert caps and {c.access_state for c in caps} == {"verification_pending"}


class _ProviderOk:
    def __init__(self):
        self.spent = []

    def refresh_token(self, token):
        self.spent.append(token)
        return {"access_token": "at-new", "refresh_token": "rt-new", "expire_in": 14400}


def test_p1_db_failure_after_provider_rotation_requires_reauth_and_never_reuses_token(tenant, monkeypatch):
    w = build_world(tenant)
    owner = principal(tenant, "owner")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        platforms.store_credential(s, owner, w.shopee_seller, "refresh_token", "rt-0")
    with owner_session() as s:
        s.get(PlatformAccount, w.shopee_seller).live_enabled = True
    provider = _ProviderOk()
    real = channel_sync.encrypt_secret

    def fail_after_provider(value):
        raise RuntimeError("database unavailable while storing rotated tokens")

    monkeypatch.setattr(channel_sync, "encrypt_secret", fail_after_provider)
    with pytest.raises(RuntimeError):
        channel_sync.rotate_refresh_token(tenant.org_id, w.shopee_seller, provider)
    monkeypatch.setattr(channel_sync, "encrypt_secret", real)
    assert provider.spent == ["rt-0"]
    assert platforms.read_secret(tenant.org_id, w.shopee_seller, "refresh_token") == "rt-0"  # old token kept

    # Next attempt must not spend rt-0 again: uncertain → reauthorization required, case opened.
    assert channel_sync.rotate_refresh_token(tenant.org_id, w.shopee_seller, provider) is False
    assert provider.spent == ["rt-0"]
    with owner_session() as s:
        op = s.scalars(select(ExternalOperation).where(ExternalOperation.operation == "token_refresh",
                                                       ExternalOperation.platform_account_id == w.shopee_seller)).one()
        assert op.status == "uncertain"
        assert s.scalars(select(ExceptionCase).where(ExceptionCase.subject_id == str(op.id))).one().type == \
            "external_uncertain"
        caps = s.scalars(select(IntegrationCapability).where(
            IntegrationCapability.platform_account_id == w.shopee_seller)).all()
        assert all(channel_sync.REAUTH in c.blockers and c.access_state == "not_authorized" for c in caps)
    channel_sync.sync_orders(tenant.org_id, w.shopee_seller, utcnow() - timedelta(days=1), utcnow())
    with owner_session() as s:  # a later sync keeps the account not_authorized until a human reauthorizes
        assert {c.access_state for c in s.scalars(select(IntegrationCapability).where(
            IntegrationCapability.platform_account_id == w.shopee_seller))} == {"not_authorized"}


def test_p1_ad_spend_requires_idr_and_explicit_legal_entity(tenant):
    build_world(tenant)
    acc = _ads_account(tenant)
    owner = principal(tenant, "owner")
    day = utcnow().date() - timedelta(days=1)
    usd = f"report_date,campaign_id,ad_id,spend,currency\n{day},C1,AD1,25.00,USD\n"
    with tenant_session(tenant.org_id, owner.user_id) as s:
        res = imports.run_import(s, owner, "ads_spend", "usd.csv", usd, dry_run=False, platform_account_id=acc)
    assert res["status"] == "rejected" and "FX" in res["errors"][0]["error"]
    MockAds.seed("meta", [{"date_start": str(day), "campaign_id": "C1", "ad_id": "AD1", "spend": "25.00",
                           "account_currency": "USD"},
                          {"date_start": str(day), "campaign_id": "C1", "ad_id": "AD2", "spend": "10000",
                           "account_currency": "IDR"}])
    res = channel_sync.sync_insights(tenant.org_id, acc, day, day)
    assert (res["rows"], res["rejected_currency"]) == (1, 1)
    with owner_session() as s:
        assert posting._balance(s, A.ADVERTISING, organization_id=tenant.org_id) == D("10000")
        s.get(PlatformAccount, acc).legal_entity_id = None
    assert channel_sync.sync_insights(tenant.org_id, acc, day, day)["status"] == "rejected"
    idr = f"report_date,campaign_id,ad_id,spend,currency\n{day},C1,AD3,5000,IDR\n"
    with pytest.raises(HTTPException):
        with tenant_session(tenant.org_id, owner.user_id) as s:
            imports.run_import(s, owner, "ads_spend", "idr.csv", idr, dry_run=False, platform_account_id=acc)
    with owner_session() as s:
        assert posting._balance(s, A.ADVERTISING, organization_id=tenant.org_id) == D("10000")


def test_p1_closed_period_ad_repull_is_booked_in_the_open_period(tenant):
    from app.modules.finance import periods
    from app.modules.fpa import snapshots

    build_world(tenant)
    acc = _ads_account(tenant)
    owner, fin = principal(tenant, "owner"), principal(tenant, "finance")
    last_month = periods.month_bounds(utcnow().date())[0] - timedelta(days=10)
    csv1 = f"report_date,campaign_id,ad_id,spend,currency\n{last_month},C7,AD7,40000,IDR\n"
    with tenant_session(tenant.org_id, owner.user_id) as s:
        imports.run_import(s, owner, "ads_spend", "a.csv", csv1, dry_run=False, platform_account_id=acc)
    with tenant_session(tenant.org_id, fin.user_id) as s:
        snapshots.close_and_snapshot(s, fin, tenant.legal_entity_id, periods.month_bounds(last_month)[0])
    csv2 = f"report_date,campaign_id,ad_id,spend,currency\n{last_month},C7,AD7,45000,IDR\n"
    with tenant_session(tenant.org_id, owner.user_id) as s:
        assert imports.run_import(s, owner, "ads_spend", "b.csv", csv2, dry_run=False,
                                  platform_account_id=acc)["applied"] == 1
    lm_start, lm_end = periods.month_bounds(last_month)
    with owner_session() as s:
        closed = s.scalar(select(func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0)).where(
            LedgerEntry.organization_id == tenant.org_id, LedgerEntry.account_code == A.ADVERTISING,
            LedgerEntry.accounting_date.between(lm_start, lm_end)))
        assert closed == D("40000")  # closed month untouched
        assert posting._balance(s, A.ADVERTISING, organization_id=tenant.org_id,
                                accounting_date=utcnow().date()) == D("5000")
