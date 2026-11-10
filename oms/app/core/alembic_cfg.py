"""Alembic config locator (readiness checks compare the DB schema with the migration head shipped in this build)."""

from pathlib import Path

from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg
