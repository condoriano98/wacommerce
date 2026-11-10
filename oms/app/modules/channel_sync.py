"""Marketplace channel sync (spec §6, §17; acceptance #10).

* Capability gate first: an account that is live-enabled but has no credentials is ``not_authorized`` and **no HTTP
  call is made**; an account that is not live-enabled uses the mock adapter (``mocked``). Live clients are only built
  from stored credentials (none exist in any environment today).
* Order sync walks provider-sized windows (Shopee 15 days) and upserts through ``orders.ingest_marketplace_order``
  (idempotent on account + external order id), so overlapping windows and replays never duplicate orders.
* Payout sync feeds ``settlements.marketplace_payout`` (idempotent per payout id) which matches reconciliation items.
* Lost pushes are re-fetched by id. Unmapped SKUs or provider errors become ``integration_errors``; the run continues.
* Single-use refresh tokens rotate under a transaction-scoped advisory lock with compare-and-set (migration 0014).
"""

import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.core.authz import system_principal
from app.core.db import worker_session
from app.core.security import decrypt_secret, encrypt_secret
from app.integrations import marketplace
from app.models.base import utcnow
from app.models.governance import IntegrationError
from app.models.platforms import IntegrationCapability, IntegrationSyncRun, PlatformAccount
from app.modules import orders, settlements

CAPABILITIES = {"shopee": ("orders.read", "settlements.read"), "tiktokshop": ("orders.read", "settlements.read"),
                "lazada": ("orders.read", "settlements.read"), "meta": ("insights.read",),
                "google_ads": ("insights.read",), "tiktok_ads": ("insights.read",)}
CREDENTIAL = {"shopee": "access_token", "tiktokshop": "access_token", "lazada": "access_token",
              "meta": "access_token", "google_ads": "refresh_token", "tiktok_ads": "access_token"}


def _has_credentials(session, account: PlatformAccount) -> bool:
    return bool(session.execute(text("select count(*) from list_credentials(:a) where name = :n"),
                                {"a": account.id, "n": CREDENTIAL.get(account.provider, "access_token")}).scalar())


REAUTH = "reauthorization_required"
LIVE_CHECK = "live_check"


def _caps(session, account: PlatformAccount) -> list[IntegrationCapability]:
    return list(session.scalars(select(IntegrationCapability).where(
        IntegrationCapability.platform_account_id == account.id)))


def access_state(session, account: PlatformAccount) -> str:
    """``available`` only after a recorded successful live authorization + capability check (none can pass today:
    live adapters are refused). Credentials alone are ``verification_pending``; a shop needing reauthorization is
    ``not_authorized``."""
    if not account.live_enabled:
        return "mocked"
    caps = _caps(session, account)
    if any(REAUTH in (c.blockers or []) for c in caps):
        return "not_authorized"
    if not _has_credentials(session, account):
        return "not_authorized"
    verified = caps and all(c.verification_source == LIVE_CHECK and c.last_contract_verified_at for c in caps)
    return "available" if verified else "verification_pending"


def refresh_capabilities(session, account: PlatformAccount, *, error: str | None = None) -> list[IntegrationCapability]:
    """Registry rows per capability. Adapters with contract tests are ``mock_tested``; access never reads
    ``available`` without a passed live check; a failing sync marks ``temporarily_unavailable``."""
    state = "temporarily_unavailable" if error else access_state(session, account)
    out = []
    for cap in CAPABILITIES.get(account.provider, ()):
        session.execute(insert(IntegrationCapability).values(
            id=uuid.uuid4(), created_at=utcnow(), organization_id=account.organization_id,
            platform_account_id=account.id, capability=cap, implementation_status="mock_tested", access_state=state,
            scopes=[], rate_limit={}, blockers=[], verification_source="contract tests (documentation-derived fixtures)",
        ).on_conflict_do_update(index_elements=["platform_account_id", "capability"],
                                set_={"access_state": state, "implementation_status": "mock_tested"}))
    session.flush()
    out = session.scalars(select(IntegrationCapability).where(
        IntegrationCapability.platform_account_id == account.id)).all()
    return out


def adapter_for(session, account: PlatformAccount):
    """Mock unless live-enabled; live requires credentials. Raises PermissionError (→ not_authorized) otherwise."""
    if not account.live_enabled:
        return marketplace.MockChannel(account.provider)
    if not _has_credentials(session, account):
        raise PermissionError("no credentials")
    raise PermissionError("live marketplace access is not authorised for this deployment (registry: not_authorized)")


def _run(session, account, kind, start, end) -> IntegrationSyncRun:
    run = IntegrationSyncRun(organization_id=account.organization_id, platform_account_id=account.id, kind=kind,
                             window_start=start, window_end=end, status="running", stats={})
    session.add(run)
    session.flush()
    return run


def _error(session, account, kind: str, message: str, detail: dict) -> None:
    session.add(IntegrationError(organization_id=account.organization_id, platform_account_id=account.id, kind=kind,
                                 severity="error", message=message[:500], detail=detail))


def sync_orders(org_id: uuid.UUID, account_id: uuid.UUID, start: datetime, end: datetime) -> dict:
    with worker_session(org_id) as s:
        account = s.get(PlatformAccount, account_id)
        run = _run(s, account, "orders", start, end)
        try:
            adapter = adapter_for(s, account)
        except PermissionError as exc:
            run.status, run.error, run.finished_at = "skipped", str(exc), utcnow()
            refresh_capabilities(s, account)
            return {"status": "not_authorized", "calls": 0}
        stats = {"windows": 0, "orders": 0, "created": 0, "errors": 0}
        size = marketplace.MAX_WINDOW.get(account.provider, timedelta(days=15))
        for ws, we in marketplace.windows(start, end, size):
            stats["windows"] += 1
            for o in adapter.list_orders(ws, we):
                stats["orders"] += 1
                try:
                    with s.begin_nested():
                        _, created = orders.ingest_marketplace_order(s, account, o)
                        stats["created"] += created
                except HTTPException as exc:
                    stats["errors"] += 1
                    _error(s, account, "order_ingest", str(exc.detail), {"external_order_id": o["external_order_id"]})
        run.status, run.stats, run.finished_at = "ok", stats, utcnow()
        account.last_sync_ok_at = utcnow()
        refresh_capabilities(s, account)
        return {"status": "ok", **stats}


def recover_lost_pushes(org_id: uuid.UUID, account_id: uuid.UUID, lookback: timedelta = timedelta(days=3)) -> int:
    """Push notifications that were never delivered are listed by the platform; re-sync a short window to pick
    them up (the ingest is idempotent)."""
    with worker_session(org_id) as s:
        account = s.get(PlatformAccount, account_id)
        try:
            lost = adapter_for(s, account).lost_pushes()
        except PermissionError:
            return 0
    if lost:
        sync_orders(org_id, account_id, utcnow() - lookback, utcnow() + timedelta(minutes=1))
    return len(lost)


def sync_payouts(org_id: uuid.UUID, account_id: uuid.UUID, start: datetime, end: datetime) -> dict:
    with worker_session(org_id) as s:
        account = s.get(PlatformAccount, account_id)
        run = _run(s, account, "settlements", start, end)
        try:
            adapter = adapter_for(s, account)
        except PermissionError as exc:
            run.status, run.error, run.finished_at = "skipped", str(exc), utcnow()
            refresh_capabilities(s, account)
            return {"status": "not_authorized", "calls": 0}
        principal = system_principal(s, org_id)
        n = 0
        for p in adapter.list_payouts(start, end):
            settlements.marketplace_payout(s, principal, account.id, p["payout_id"], p["lines"])
            n += 1
        run.status, run.stats, run.finished_at = "ok", {"payouts": n}, utcnow()
        refresh_capabilities(s, account)
        return {"status": "ok", "payouts": n}


def _mark_reauth(session, account: PlatformAccount, reason: str) -> None:
    for c in _caps(session, account) or refresh_capabilities(session, account):
        c.blockers = sorted(set(c.blockers or []) | {REAUTH})
        c.access_state = "not_authorized"
    _error(session, account, "token_rotation", reason, {"action": "reauthorize the shop in the provider console"})


def rotate_refresh_token(org_id: uuid.UUID, account_id: uuid.UUID, adapter) -> bool:
    """Single-use refresh token rotation with crash safety (ADR-015 applied to credentials).

    1. Lock (advisory, transaction-scoped) and read the current refresh token + version.
    2. If a previous rotation of this same version is recorded as ``sent`` (provider may have consumed the token but
       the new tokens were never stored), DO NOT spend the token again: mark the operation ``uncertain``, flag the
       account ``reauthorization_required`` and open a case. A human reauthorizes the shop (docs/26 runbook).
    3. Otherwise record the operation as ``sent`` in its own committed transaction, call the provider once, then store
       both new tokens AND confirm the operation in the locked transaction — atomically. If that transaction fails,
       step 2 catches it on the next attempt.
    """
    from app.core import external_ops
    from app.models.governance import ExternalOperation

    with worker_session(org_id) as s:
        row = s.execute(text("select * from credential_for_rotation(:a, 'refresh_token')"), {"a": account_id}).one()
        if row.ciphertext is None:
            return False
        account = s.get(PlatformAccount, account_id)
        key = f"{account_id}:refresh_token:v{row.version}"
        op = external_ops.prepare(org_id, account.provider, "token_refresh", key, platform_account_id=account_id,
                                  subject_type="platform_account", subject_id=str(account_id))
        if op.status in ("sent", "uncertain"):
            token = external_ops.takeover(org_id, op.id) if op.status == "sent" else None
            if token is not None:
                external_ops._uncertain(org_id, op.id, "refresh token may have been consumed; new tokens not stored",
                                        token=token)
            _mark_reauth(s, account, "refresh token state uncertain after a failed rotation; reauthorization required")
            return False
        if op.status == "confirmed":  # already rotated from this version (should not happen under the lock)
            return False
        claim = external_ops.claim(org_id, op.id)  # committed before the provider call
        if claim is None:
            return False
        new = adapter.refresh_token(decrypt_secret(bytes(row.ciphertext)))
        expires = utcnow() + timedelta(seconds=int(new.get("expire_in", 14400)))
        ok = s.execute(text("select rotate_credential(:a, 'refresh_token', :v, :c, :e)"),
                       {"a": account_id, "v": row.version, "c": encrypt_secret(new["refresh_token"]),
                        "e": utcnow() + timedelta(days=30)}).scalar()
        cur = s.execute(text("select * from credential_for_rotation(:a, 'access_token')"), {"a": account_id}).one()
        s.execute(text("select rotate_credential(:a, 'access_token', :v, :c, :e)"),
                  {"a": account_id, "v": cur.version, "c": encrypt_secret(new["access_token"]), "e": expires})
        o = s.get(ExternalOperation, op.id, with_for_update=True)
        if o.claim_token != claim or o.status != "sent":  # fenced: never confirm someone else's claim
            raise RuntimeError("token rotation claim superseded")
        o.status, o.confirmed_at, o.response = "confirmed", utcnow(), {"rotated_from_version": row.version}
        s.execute(text("update external_operation_attempts set outcome = 'confirmed', finished_at = now() "
                       "where claim_token = :t"), {"t": claim})
        return bool(ok)


def sync_insights(org_id: uuid.UUID, account_id: uuid.UUID, start, end) -> dict:
    """Daily ad insights (re-pulled up to 28 days back) → ``daily_ad_insights`` upsert → effective-dated accrual
    deltas (``marketing.accrue_day``). Campaigns seen for the first time are registered with no allocation (their
    cost stays unallocated until a human sets one)."""
    from app.integrations import ads
    from app.models.marketing import AdCampaign, DailyAdInsight
    from app.modules import marketing

    with worker_session(org_id) as s:
        account = s.get(PlatformAccount, account_id)
        run = _run(s, account, "insights", None, None)
        if account.live_enabled:
            run.status, run.error, run.finished_at = "skipped", "no live ads access", utcnow()
            refresh_capabilities(s, account)
            return {"status": "not_authorized", "calls": 0}
        if account.legal_entity_id is None:  # no implicit default entity (financial boundary, G-ADS1)
            run.status, run.error, run.finished_at = "failed", "ad account has no legal entity mapping", utcnow()
            _error(s, account, "insights_mapping", "ad account has no legal entity mapping", {})
            return {"status": "rejected", "reason": "no_legal_entity"}
        fetched = ads.MockAds(account.provider).insights(start, end)
        rows, rejected = [], 0
        for r in fetched:  # spend is booked in IDR only until an FX conversion policy exists (G-ADS1)
            if (r.pop("currency", "") or "").upper() != "IDR":
                rejected += 1
                continue
            rows.append(r)
        if rejected:
            _error(s, account, "insights_currency", f"{rejected} insight rows not in IDR were rejected",
                   {"rejected": rejected})
        touched = set()
        for r in rows:
            s.execute(insert(DailyAdInsight).values(
                id=uuid.uuid4(), created_at=utcnow(), organization_id=org_id, platform_account_id=account.id,
                provider=account.provider, currency="IDR", reported_conversions=0, reported_conversion_value=0,
                reach=0, link_clicks=0, data_source="api", pulled_at=utcnow(), **r,
            ).on_conflict_do_update(index_elements=["platform_account_id", "report_date", "ad_external_id"],
                                    set_={"spend": r["spend"], "impressions": r["impressions"], "clicks": r["clicks"],
                                          "messaging_conversations": r["messaging_conversations"],
                                          "pulled_at": utcnow()}))
            touched.add((r["campaign_external_id"], r["report_date"]))
        deltas = 0
        le = account.legal_entity_id
        for ext, day in sorted(touched):
            camp = s.scalars(select(AdCampaign).where(AdCampaign.platform_account_id == account.id,
                                                      AdCampaign.external_id == ext)).first()
            if camp is None:
                camp = AdCampaign(organization_id=org_id, platform_account_id=account.id, provider=account.provider,
                                  external_id=ext, name=f"{account.provider} {ext}")
                s.add(camp)
                s.flush()
            deltas += len(marketing.accrue_day(s, org_id, le, camp, day))
        run.status, run.finished_at = "ok", utcnow()
        run.stats = {"rows": len(rows), "rejected_currency": rejected, "accrual_deltas": deltas}
        refresh_capabilities(s, account)
        return {"status": "ok", "rows": len(rows), "rejected_currency": rejected, "accrual_deltas": deltas}


ADS = ("meta", "google_ads", "tiktok_ads")


def run_scheduled(kind: str, now: datetime | None = None) -> dict:
    """Entry point for the worker's periodic tasks. Each account syncs independently; one failing account marks its
    capability ``temporarily_unavailable`` and does not stop the others."""
    now = now or utcnow()
    with worker_session() as s:
        targets = s.execute(text("select * from sync_targets()")).all()
    done = {"ok": 0, "skipped": 0, "failed": 0}
    for t in targets:
        is_ads = t.provider in ADS
        if (kind == "insights") != is_ads:
            continue
        try:
            if kind == "orders":
                res = sync_orders(t.organization_id, t.platform_account_id, now - timedelta(days=2), now)
                recover_lost_pushes(t.organization_id, t.platform_account_id)
            elif kind == "payouts":
                res = sync_payouts(t.organization_id, t.platform_account_id, now - timedelta(days=15), now)
            else:
                res = sync_insights(t.organization_id, t.platform_account_id, (now - timedelta(days=28)).date(),
                                    now.date())
            done["ok" if res["status"] == "ok" else "skipped"] += 1
        except Exception as exc:  # noqa: BLE001 - isolate accounts; recorded for operators
            done["failed"] += 1
            with worker_session(t.organization_id) as s:
                account = s.get(PlatformAccount, t.platform_account_id)
                _error(s, account, f"sync_{kind}", str(exc), {})
                refresh_capabilities(s, account, error=str(exc))
    return done
