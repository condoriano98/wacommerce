"""M5 deployment artefacts (static checks; Docker is not available in this environment, images are not built here):
compose parses and keeps providers mocked, env template carries no secrets, the contract migration is not part of the
Alembic chain, the DR drill script exists and is executable."""

import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_compose_parses_and_has_no_live_flags():
    c = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text())
    assert {"db", "migrate", "api", "worker", "dashboard"} <= set(c["services"])
    lines = [ln for ln in (ROOT / "deploy" / "docker-compose.yml").read_text().lower().splitlines()
             if not ln.lstrip().startswith("#")]
    text = "\n".join(lines)
    assert "live_enabled" not in text and "oms_env: production" not in text


def test_env_template_contains_placeholders_only():
    env = (ROOT / "deploy" / "sumopod" / "oms.env.example").read_text()
    for line in env.splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            if any(s in key for s in ("SECRET", "KEY", "TOKEN", "URL")) and value:
                assert "<" in value and ">" in value, key  # placeholder, never a real value


def test_contract_migration_is_not_in_the_alembic_chain():
    versions = (ROOT / "oms" / "migrations" / "versions").glob("*.py")
    assert not any("oms_system" in p.read_text() and "drop role" in p.read_text().lower() for p in versions)
    sql = (ROOT / "deploy" / "migrations" / "contract_drop_oms_system.sql").read_text()
    assert "NOT applied by default" in sql and re.search(r"lock_timeout", sql)


def test_dr_drill_script_is_executable():
    p = ROOT / "deploy" / "backup" / "dr_drill.sh"
    assert os.access(p, os.X_OK) and "PRELIMINARY" in p.read_text()
