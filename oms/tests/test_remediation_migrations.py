"""Phase G: the remediation migrations (0018-0024) are forward-only expand steps that upgrade and downgrade cleanly,
and the journal-header backfill flags (never deletes) pre-existing duplicate postings."""

import os
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from tests.conftest import PG_ADMIN

HERE = os.path.dirname(__file__)


def _cfg(db: str) -> Config:
    cfg = Config(os.path.join(HERE, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(HERE, "..", "migrations"))
    cfg.attributes["url"] = f"{PG_ADMIN}/{db}"
    return cfg


def _journal(c, org, le, jid, source_id):
    for n, (acct, dr, cr) in enumerate((("1000", 100, 0), ("3000", 0, 100)), start=1):
        c.execute(text("insert into ledger_entries (id, organization_id, created_at, journal_id, line_no, account_code, "
                       "debit, credit, accounting_date, source_type, source_id, event_time, posting_rule_version, "
                       "legal_entity_id, currency) values (gen_random_uuid(), :o, now(), :j, :n, :a, :d, :c, "
                       "current_date, 'owner_capital', :s, now(), 2, :le, 'IDR')"),
                  {"o": org, "j": jid, "n": n, "a": acct, "d": dr, "c": cr, "s": source_id, "le": le})


def test_G_remediation_migrations_round_trip_and_backfill_flags_legacy_duplicates():
    db = f"oms_migr_{uuid.uuid4().hex[:8]}"
    admin = create_engine(f"{PG_ADMIN}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"create database {db}"))
    eng = create_engine(f"{PG_ADMIN}/{db}")
    try:
        cfg = _cfg(db)
        command.upgrade(cfg, "0018")
        org, le = uuid.uuid4(), uuid.uuid4()
        with eng.begin() as c:  # history written before 0019: the same event posted twice (pre-fix race)
            c.execute(text("insert into organizations (id, created_at, name, base_currency) values (:o, now(), 'migr', 'IDR')"), {"o": org})
            c.execute(text("insert into legal_entities (id, organization_id, created_at, name, pkp_status, "
                           "accounting_framework, base_currency) values (:l, :o, now(), 'PT migr', 'unknown', "
                           "'unconfirmed', 'IDR')"), {"l": le, "o": org})
            _journal(c, org, le, uuid.uuid4(), "cap-1")
            _journal(c, org, le, uuid.uuid4(), "cap-1")
            _journal(c, org, le, uuid.uuid4(), "cap-2")
        command.upgrade(cfg, "head")
        with eng.connect() as c:
            rows = c.execute(text("select source_id, legacy_duplicate from journals order by source_id, "
                                  "legacy_duplicate")).all()
            assert [(r[0], r[1]) for r in rows] == [("cap-1", False), ("cap-1", True), ("cap-2", False)]
            assert c.execute(text("select count(*) from ledger_entries")).scalar() == 6  # nothing deleted
        command.downgrade(cfg, "0017")
        with eng.connect() as c:
            assert c.execute(text("select to_regclass('journals')")).scalar() is None
            assert c.execute(text("select count(*) from ledger_entries")).scalar() == 6
        command.upgrade(cfg, "head")
        with eng.connect() as c:
            assert c.execute(text("select version_num from alembic_version")).scalar() == "0024"
            assert c.execute(text("select count(*) from journals where legacy_duplicate")).scalar() == 1
    finally:
        eng.dispose()
        with admin.connect() as c:
            c.execute(text(f"drop database if exists {db} with (force)"))
        admin.dispose()
