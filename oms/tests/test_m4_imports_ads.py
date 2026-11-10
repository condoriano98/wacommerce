"""M4: CSV import framework (dry-run, provenance, cross-batch dedupe, all-or-nothing validation, domain appliers) and
ads insights sync with effective-dated accrual deltas on re-pulls."""

from datetime import timedelta
from decimal import Decimal

import pytest
import respx
from fastapi import HTTPException
from sqlalchemy import func, select, text

import app.registry  # noqa: F401
from app.core.db import tenant_session
from app.integrations import mockstore
from app.integrations.ads import MockAds
from app.models.base import utcnow
from app.models.finance import BankStatementLine
from app.models.org import BankAccount
from app.models.platforms import PlatformAccount
from app.modules import channel_sync, imports
from app.modules.finance import accounts as A
from app.modules.finance import posting
from tests.conftest import owner_session
from tests.world import build_world, principal

D = Decimal


@pytest.fixture(autouse=True)
def _fresh():
    mockstore.reset()
    yield
    mockstore.reset()


def _bank(t):
    with owner_session() as s:
        ba = BankAccount(organization_id=t.org_id, legal_entity_id=t.legal_entity_id, provider="bank",
                         institution="BCA", account_ref=f"CSV-{t.org_id.hex[:6]}")
        s.add(ba)
        s.flush()
        return ba.id


def _ads_account(t, live=False):
    with owner_session() as s:
        a = PlatformAccount(organization_id=t.org_id, brand_id=t.brand_id, provider="meta", provider_account_id="act_1",
                            display_name="Meta ads", role="advertising", legal_entity_id=t.legal_entity_id,
                            live_enabled=live)
        s.add(a)
        s.flush()
        return a.id


BANK_CSV = "booked_at,amount,reference,counterparty\n2026-11-09,115000,TRF-1,BUDI\n2026-11-09,90000,TRF-2,SITI\n"


def test_bank_csv_dry_run_commit_dedupe_and_provenance(tenant, client):
    bid = _bank(tenant)
    fin = principal(tenant, "finance")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        dry = imports.run_import(s, fin, "bank_statement", "bca-nov.csv", BANK_CSV, dry_run=True, bank_account_id=bid)
    assert dry["status"] == "dry_run" and dry["new"] == 2
    with owner_session() as s:  # dry run changes nothing in the books
        assert s.scalar(select(func.count()).select_from(BankStatementLine).where(
            BankStatementLine.bank_account_id == bid)) == 0
    r = client.post("/imports/bank_statement", headers=tenant.headers("finance"), json={
        "file_name": "bca-nov.csv", "content": BANK_CSV, "dry_run": False, "bank_account_id": str(bid)})
    assert r.status_code == 200 and r.json()["applied"] == 2, r.text
    again = client.post("/imports/bank_statement", headers=tenant.headers("finance"), json={
        "file_name": "bca-nov-copy.csv", "content": BANK_CSV, "dry_run": False, "bank_account_id": str(bid)}).json()
    assert (again["new"], again["duplicates"], again["applied"]) == (0, 2, 0)
    with owner_session() as s:
        assert s.scalar(select(func.count()).select_from(BankStatementLine).where(
            BankStatementLine.bank_account_id == bid)) == 2
        b = s.execute(text("select file_sha256, uploaded_by from import_batches where organization_id = :o and "
                           "status = 'committed' order by created_at limit 1"), {"o": tenant.org_id}).one()
        assert b.uploaded_by == fin.user_id and len(b.file_sha256) == 64
    assert client.post("/imports/bank_statement", headers=tenant.headers("pm_a"), json={
        "file_name": "x.csv", "content": BANK_CSV, "dry_run": True, "bank_account_id": str(bid)}).status_code == 403
    listed = client.get("/imports", headers=tenant.headers("finance")).json()
    assert {x["status"] for x in listed} >= {"dry_run", "committed"}


def test_invalid_rows_reject_the_whole_file_with_line_errors(tenant):
    bid = _bank(tenant)
    fin = principal(tenant, "finance")
    bad = BANK_CSV + "2026-13-01,abc,TRF-3,X\n"
    with tenant_session(tenant.org_id, fin.user_id) as s:
        res = imports.run_import(s, fin, "bank_statement", "bad.csv", bad, dry_run=False, bank_account_id=bid)
    assert res["status"] == "rejected" and res["errors"][0]["line"] == 4
    with owner_session() as s:
        assert s.scalar(select(func.count()).select_from(BankStatementLine).where(
            BankStatementLine.bank_account_id == bid)) == 0
    with pytest.raises(HTTPException):
        with tenant_session(tenant.org_id, fin.user_id) as s:
            imports.run_import(s, fin, "bank_statement", "x.csv", "booked_at,amount\n", dry_run=True,
                               bank_account_id=bid)


def test_marketplace_orders_and_settlement_csv_reconcile(tenant):
    w = build_world(tenant)
    owner, fin = principal(tenant, "owner"), principal(tenant, "finance")
    orders_csv = ("external_order_id,status,external_sku_ref,qty,unit_price\n"
                  "CSV-ORD-1,delivered,SHP-C1,1,120000\n")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        assert imports.run_import(s, owner, "marketplace_orders", "orders.csv", orders_csv, dry_run=False,
                                  platform_account_id=w.shopee_seller)["applied"] == 1
    settle_csv = ("payout_id,external_order_id,gross,fee,tax_withheld,net\n"
                  "PO-CSV-1,CSV-ORD-1,120000,9600,0,110400\n")
    with tenant_session(tenant.org_id, fin.user_id) as s:
        assert imports.run_import(s, fin, "marketplace_settlement", "payout.csv", settle_csv, dry_run=False,
                                  platform_account_id=w.shopee_seller)["applied"] == 1
    with owner_session() as s:
        st = s.execute(text("select ri.status from reconciliation_items ri join orders o on o.id::text = "
                            "ri.expected_ref where o.external_order_id = 'CSV-ORD-1'")).scalar()
        assert st == "matched"


def test_ads_insights_sync_and_csv_repull_post_effective_dated_deltas(tenant):
    build_world(tenant)
    acc = _ads_account(tenant)
    day = utcnow().date() - timedelta(days=1)
    row = {"date_start": str(day), "campaign_id": "C9", "adset_id": "S9", "ad_id": "AD9", "spend": "50000",
           "account_currency": "IDR",
           "impressions": "1000", "clicks": "40",
           "actions": [{"action_type": "onsite_conversion.messaging_conversation_started_7d", "value": "7"}]}
    MockAds.seed("meta", [row])
    res = channel_sync.sync_insights(tenant.org_id, acc, day - timedelta(days=28), day)
    assert res["rows"] == 1 and res["accrual_deltas"] == 1
    MockAds.seed("meta", [{**row, "spend": "56000"}], replace=True)  # Meta revises spend within 28 days
    assert channel_sync.sync_insights(tenant.org_id, acc, day - timedelta(days=28), day)["accrual_deltas"] == 1
    with owner_session() as s:
        assert posting._balance(s, A.ADVERTISING, organization_id=tenant.org_id) == D("56000")
    # CSV re-pull with another revision: applied as a delta, identical re-upload ignored.
    owner = principal(tenant, "owner")
    csv_ = f"report_date,campaign_id,ad_id,spend,currency\n{day},C9,AD9,60000,IDR\n"
    for expected in (1, 0):
        with tenant_session(tenant.org_id, owner.user_id) as s:
            assert imports.run_import(s, owner, "ads_spend", "meta.csv", csv_, dry_run=False,
                                      platform_account_id=acc)["applied"] == expected
    with owner_session() as s:
        assert posting._balance(s, A.ADVERTISING, organization_id=tenant.org_id) == D("60000")


def test_10_live_ads_account_without_access_makes_no_calls(tenant):
    acc = _ads_account(tenant, live=True)
    MockAds.seed("meta", [])
    with respx.mock(assert_all_called=False) as router:
        assert channel_sync.sync_insights(tenant.org_id, acc, utcnow().date(), utcnow().date())["status"] == \
            "not_authorized"
        assert router.calls.call_count == 0
    with mockstore.state("ads_meta") as st:
        assert st.get("calls", 0) == 0
