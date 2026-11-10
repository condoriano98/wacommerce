"""Seed a throw-away database for the dashboard's live end-to-end tests (remediation E1). Test support only.

Creates (or recreates) ``$OMS_TEST_DB`` (default ``oms_e2e``), migrates it to head, gives the runtime roles their
local passwords, and seeds one tenant with the standard test users and an order that has lines in portfolios A and B.
Writes the user e-mails and ids the browser tests need to the JSON file given as the first argument.

Refuses to run unless the database name ends with ``_e2e`` (it drops that database first).
"""

import json
import os
import sys

os.environ.setdefault("OMS_TEST_DB", "oms_e2e")
if not os.environ["OMS_TEST_DB"].endswith("_e2e"):
    raise SystemExit("refusing to seed a database whose name does not end with _e2e")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from tests.conftest import PG_ADMIN, TEST_DB, make_tenant  # noqa: E402

HERE = os.path.dirname(__file__)


def main(out_path: str) -> None:
    admin = create_engine(f"{PG_ADMIN}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"drop database if exists {TEST_DB} with (force)"))
        c.execute(text(f"create database {TEST_DB}"))
    cfg = Config(os.path.join(HERE, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(HERE, "..", "migrations"))
    cfg.attributes["url"] = f"{PG_ADMIN}/{TEST_DB}"
    command.upgrade(cfg, "head")
    with admin.connect() as c:
        for role in ("oms_app", "oms_worker", "oms_webhook"):
            c.execute(text(f"alter role {role} login password '{role}'"))
    admin.dispose()

    import app.registry  # noqa: F401
    from app.core.outbox import dispatch_due
    from tests.test_accounting_services import _order, _pay_gateway
    from tests.world import build_world

    t = make_tenant()
    w = build_world(t)
    oid = _order(t, w, [("A1", 1, "0"), ("B1", 1, "0")])
    _pay_gateway(t, oid)
    dispatch_due()
    from tests.conftest import owner_session

    with owner_session() as s:
        emails = dict(s.execute(text("select name, email from users where organization_id = :o"),
                                {"o": t.org_id}).all())
        order_number = s.execute(text("select order_number from orders where id = :o"), {"o": oid}).scalar()
    seed = {"org_id": str(t.org_id), "legal_entity_id": str(t.legal_entity_id),
            "portfolios": {k: str(v) for k, v in t.portfolios.items()},
            "users": {name: {"email": emails[name], "password": f"pw-{name}"} for name in
                      ("owner", "pm_a", "pm_b", "finance", "cs")},
            "order_number": order_number}
    with open(out_path, "w") as f:
        json.dump(seed, f, indent=2)
    print(f"seeded {TEST_DB}: org {t.org_id}, order {order_number}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "e2e-seed.json")
