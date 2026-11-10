"""CSV import framework (spec §6 CSV fallback): schema-validated batches with provenance (file sha256, uploader,
source), row fingerprints that prevent duplicates across batches, dry-run preview, then commit through the same domain
services the API adapters use (so imported data reconciles exactly like synced data).

Sources: ``bank_statement`` (→ bank statement lines), ``marketplace_settlement`` (→ marketplace payouts),
``ads_spend`` (→ daily ad insights + accrual deltas), ``marketplace_orders`` (→ native order ingest).
"""

import csv
import hashlib
import io
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.authz import Principal
from app.models.base import utcnow

REPORTING_CURRENCY = "IDR"


@dataclass(frozen=True)
class Schema:
    columns: dict[str, str]  # name -> type: str | decimal | int | date
    permission: str
    key: tuple[str, ...]  # columns forming the business identity of a row (fingerprint)


SCHEMAS = {
    "bank_statement": Schema({"booked_at": "date", "amount": "decimal", "reference": "str", "counterparty": "str"},
                             "finance.manage", ("booked_at", "amount", "reference", "counterparty")),
    "marketplace_settlement": Schema({"payout_id": "str", "external_order_id": "str", "gross": "decimal",
                                      "fee": "decimal", "tax_withheld": "decimal", "net": "decimal"},
                                     "finance.manage", ("payout_id", "external_order_id")),
    "ads_spend": Schema({"report_date": "date", "campaign_id": "str", "ad_id": "str", "spend": "decimal",
                         "currency": "str"},
                        "marketing.manage", ("report_date", "ad_id", "spend")),
    "marketplace_orders": Schema({"external_order_id": "str", "status": "str", "external_sku_ref": "str",
                                  "qty": "int", "unit_price": "decimal"},
                                 "orders.manage", ("external_order_id", "external_sku_ref", "status")),
}


def _parse(value: str, typ: str):
    value = (value or "").strip()
    if typ == "str":
        return value
    if typ == "int":
        return int(value)
    if typ == "decimal":
        return Decimal(value.replace(",", ""))
    if typ == "date":
        return date.fromisoformat(value)
    raise ValueError(typ)


def _fingerprint(source: str, row: dict, schema: Schema, scope: str) -> str:
    ident = "|".join(str(row[k]) for k in schema.key)
    return hashlib.sha256(f"{source}|{scope}|{ident}".encode()).hexdigest()


def run_import(session: Session, principal: Principal, source: str, file_name: str, content: str, *,
               dry_run: bool, platform_account_id: uuid.UUID | None = None,
               bank_account_id: uuid.UUID | None = None) -> dict:
    schema = SCHEMAS.get(source)
    if schema is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown import source {source}")
    principal.require(schema.permission)
    reader = csv.DictReader(io.StringIO(content))
    missing = set(schema.columns) - set(reader.fieldnames or [])
    if missing:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"missing columns: {sorted(missing)}")
    scope = str(platform_account_id or bank_account_id or "")
    rows, errors = [], []
    for line_no, raw in enumerate(reader, start=2):
        try:
            row = {k: _parse(raw.get(k, ""), t) for k, t in schema.columns.items()}
        except (ValueError, InvalidOperation) as exc:
            errors.append({"line": line_no, "error": f"invalid value: {exc}"})
            continue
        if "currency" in row and row["currency"].upper() != REPORTING_CURRENCY:
            errors.append({"line": line_no, "error": f"currency {row['currency']} not supported: no FX conversion "
                                                     f"policy configured (G-ADS1); only {REPORTING_CURRENCY}"})
            continue
        rows.append((line_no, row, _fingerprint(source, row, schema, scope)))
    # Deduplicate within the file first: a repeated line must never reach the domain services twice.
    unique, in_file_dupes, firsts = [], [], set()
    for ln, r, f in rows:
        if f in firsts:
            in_file_dupes.append(ln)
        else:
            firsts.add(f)
            unique.append((ln, r, f))
    rows = unique
    seen = set(session.scalars(text("select fingerprint from import_rows where source = :s and fingerprint = any(:f)"),
                               {"s": source, "f": [f for _, _, f in rows]}).all()) if rows else set()
    new = [(ln, r, f) for ln, r, f in rows if f not in seen]
    batch_id = uuid.uuid4()
    file_sha = hashlib.sha256(content.encode()).hexdigest()
    stats = {"rows": len(rows) + len(in_file_dupes) + len(errors), "valid": len(rows) + len(in_file_dupes),
             "invalid": len(errors), "new": len(new), "duplicates": len(rows) - len(new),
             "duplicates_in_file": len(in_file_dupes)}
    batch_status = "dry_run" if dry_run else ("rejected" if errors else "committed")
    session.execute(text(
        "insert into import_batches (id, organization_id, created_at, source, platform_account_id, file_name, "
        "file_sha256, uploaded_by, status, stats, errors) values (:i, :o, now(), :s, :p, :n, :h, :u, :st, "
        "cast(:stats as jsonb), cast(:err as jsonb))"),
        {"i": batch_id, "o": principal.org_id, "s": source, "p": platform_account_id, "n": file_name, "h": file_sha,
         "u": principal.user_id, "st": batch_status, "stats": _json(stats), "err": _json(errors)})
    if dry_run or errors:  # all-or-nothing: a file with invalid rows is rejected as a whole
        return {"batch_id": str(batch_id), "status": batch_status, "file_sha256": file_sha, **stats, "errors": errors}
    # Claim rows atomically: only rows whose tracking insert succeeds in THIS transaction are applied. A concurrent
    # upload of the same file blocks on the unique index and then claims nothing.
    rt = _rows_table()
    claimed = []
    for ln, r, f in new:
        got = session.execute(insert(rt).values(
            id=uuid.uuid4(), organization_id=principal.org_id, batch_id=batch_id, source=source, line_no=ln,
            fingerprint=f, data=_json_safe(r), result="applied").on_conflict_do_nothing(
            index_elements=["organization_id", "source", "fingerprint"]).returning(rt.c.fingerprint)).scalar()
        if got is not None:
            claimed.append(r)
    stats["claimed"] = len(claimed)
    stats["duplicates"] += len(new) - len(claimed)
    session.execute(text("update import_batches set stats = cast(:st as jsonb) where id = :i"),
                    {"st": _json(stats), "i": batch_id})
    applied = APPLY[source](session, principal, claimed, platform_account_id=platform_account_id,
                            bank_account_id=bank_account_id, batch=str(batch_id)) if claimed else 0
    audit(session, principal.org_id, "import.committed", "import_batch", batch_id, actor_user_id=principal.user_id,
          after={"source": source, "file_sha256": file_sha, **stats})
    return {"batch_id": str(batch_id), "status": batch_status, "file_sha256": file_sha, **stats, "applied": applied,
            "errors": []}


def _json(v) -> str:
    import json

    return json.dumps(v, default=str)


def _json_safe(r: dict) -> dict:
    return {k: (str(v) if isinstance(v, Decimal | date) else v) for k, v in r.items()}


def _rows_table():
    from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Uuid, func

    md = MetaData()
    return Table("import_rows", md, Column("id", Uuid, primary_key=True), Column("organization_id", Uuid),
                 Column("created_at", DateTime(timezone=True), server_default=func.now()), Column("batch_id", Uuid),
                 Column("source", String), Column("line_no", Integer), Column("fingerprint", String),
                 Column("data", JSON), Column("result", String))


# ---- appliers (reuse the domain services) ----
def _bank(session, principal, rows, *, bank_account_id, batch, **_):
    from app.modules import payments

    if bank_account_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "bank_account_id required")
    return payments.import_bank_statement(session, principal, bank_account_id, rows, batch)


def _settlement(session, principal, rows, *, platform_account_id, **_):
    from app.modules import settlements

    by_payout = defaultdict(list)
    for r in rows:
        by_payout[r["payout_id"]].append({k: r[k] for k in ("external_order_id", "gross", "fee", "tax_withheld",
                                                            "net")})
    for payout_id, lines in by_payout.items():
        settlements.marketplace_payout(session, principal, platform_account_id, payout_id, lines)
    return len(by_payout)


def _ads(session, principal, rows, *, platform_account_id, **_):
    from app.models.marketing import AdCampaign, DailyAdInsight
    from app.models.platforms import PlatformAccount
    from app.modules import marketing

    account = session.get(PlatformAccount, platform_account_id)
    if account is None or account.legal_entity_id is None:  # no implicit entity: spend must land in the right books
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "ad account has no legal entity mapping; set it before importing spend")
    deltas = 0
    for r in rows:
        session.execute(insert(DailyAdInsight).values(
            id=uuid.uuid4(), created_at=utcnow(), organization_id=principal.org_id,
            platform_account_id=platform_account_id, provider=account.provider, report_date=r["report_date"],
            campaign_external_id=r["campaign_id"], ad_external_id=r["ad_id"], spend=r["spend"],
            currency=r["currency"].upper(),
            impressions=0, reach=0, clicks=0, link_clicks=0, messaging_conversations=0, reported_conversions=0,
            reported_conversion_value=0, data_source="csv", pulled_at=utcnow(),
        ).on_conflict_do_update(index_elements=["platform_account_id", "report_date", "ad_external_id"],
                                set_={"spend": r["spend"], "data_source": "csv", "pulled_at": utcnow()}))
    for ext, day in sorted({(r["campaign_id"], r["report_date"]) for r in rows}):
        camp = session.scalars(select(AdCampaign).where(AdCampaign.platform_account_id == platform_account_id,
                                                        AdCampaign.external_id == ext)).first()
        if camp is None:
            camp = AdCampaign(organization_id=principal.org_id, platform_account_id=platform_account_id,
                              provider=account.provider, external_id=ext, name=f"{account.provider} {ext}")
            session.add(camp)
            session.flush()
        deltas += len(marketing.accrue_day(session, principal.org_id, account.legal_entity_id, camp, day))
    return deltas


def _orders(session, principal, rows, *, platform_account_id, **_):
    from app.models.platforms import PlatformAccount
    from app.modules import orders

    account = session.get(PlatformAccount, platform_account_id)
    by_order = defaultdict(list)
    for r in rows:
        by_order[(r["external_order_id"], r["status"])].append(r)
    created = 0
    for (oid, st), items in by_order.items():
        total = sum((i["unit_price"] * i["qty"] for i in items), Decimal(0))
        _, new = orders.ingest_marketplace_order(session, account, {
            "external_order_id": oid, "status": st, "total": total, "shipping_fee": 0,
            "items": [{"external_sku_ref": i["external_sku_ref"], "qty": i["qty"], "unit_price": i["unit_price"]}
                      for i in items]})
        created += new
    return created


APPLY = {"bank_statement": _bank, "marketplace_settlement": _settlement, "ads_spend": _ads,
         "marketplace_orders": _orders}
