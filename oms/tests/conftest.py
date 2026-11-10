"""Test harness: a fresh database migrated as the owner; the app and tests then connect as the
RLS-restricted ``oms_app`` role exactly as in production."""

import os
import uuid
from dataclasses import dataclass, field

import pytest

PG_ADMIN = os.environ.get("OMS_TEST_PG_ADMIN", "postgresql+psycopg://postgres@localhost:5432")
TEST_DB = os.environ.get("OMS_TEST_DB", "oms_test")
os.environ["OMS_DATABASE_URL"] = f"postgresql+psycopg://oms_app:oms_app@localhost:5432/{TEST_DB}"
os.environ["OMS_SYSTEM_DATABASE_URL"] = f"postgresql+psycopg://oms_system:oms_system@localhost:5432/{TEST_DB}"
os.environ["OMS_MIGRATION_DATABASE_URL"] = f"{PG_ADMIN}/{TEST_DB}"
os.environ["OMS_JWT_SECRET"] = "test-secret"

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app.core.db import system_session  # noqa: E402
from app.core.security import hash_password, issue_token  # noqa: E402
from app.models.org import Brand, Organization, Portfolio, RoleAssignment, User  # noqa: E402
from app.modules.org import seed_org_defaults  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def database():
    admin = create_engine(f"{PG_ADMIN}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"drop database if exists {TEST_DB} with (force)"))
        c.execute(text(f"create database {TEST_DB}"))
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "migrations"))
    cfg.attributes["url"] = f"{PG_ADMIN}/{TEST_DB}"
    command.upgrade(cfg, "head")
    with admin.connect() as c:  # deployment step: give the NOLOGIN roles credentials
        c.execute(text("alter role oms_app login password 'oms_app'"))
        c.execute(text("alter role oms_system login password 'oms_system'"))
    yield
    admin.dispose()


@dataclass
class Tenant:
    org_id: uuid.UUID
    brand_id: uuid.UUID
    portfolios: dict[str, uuid.UUID] = field(default_factory=dict)
    users: dict[str, uuid.UUID] = field(default_factory=dict)

    def token(self, user: str) -> str:
        return issue_token(self.users[user], self.org_id)

    def headers(self, user: str) -> dict:
        return {"Authorization": f"Bearer {self.token(user)}"}


def make_tenant(portfolio_codes=("A", "B", "C"), users: dict | None = None) -> Tenant:
    """users: name -> list of (role, scope) where scope is 'org' or a portfolio code."""
    users = users or {
        "owner": [("owner", "org")],
        "owner2": [("owner", "org")],
        "pm_a": [("portfolio_manager", "A")],
        "pm_b": [("portfolio_manager", "B")],
        "finance": [("finance_manager", "org")],
        "procurement_a": [("procurement_admin", "A")],
        "packer": [("packing_admin", "org")],
        "cs": [("cs_admin", "org")],
        "ai": [("ai_sales_agent", "A"), ("ai_sales_agent", "B")],
    }
    suffix = uuid.uuid4().hex[:8]
    with system_session() as s:
        org = Organization(name=f"Org {suffix}")
        s.add(org)
        s.flush()
        brand = Brand(organization_id=org.id, name="Main brand")
        s.add(brand)
        s.flush()
        t = Tenant(org_id=org.id, brand_id=brand.id)
        for code in portfolio_codes:
            p = Portfolio(organization_id=org.id, brand_id=brand.id, code=code, name=f"Portfolio {code}",
                          approved_channels=[], approved_procurement_sources=[])
            s.add(p)
            s.flush()
            t.portfolios[code] = p.id
        for name, roles in users.items():
            u = User(organization_id=org.id, email=f"{name}-{suffix}@example.test", name=name,
                     password_hash=hash_password("pw-" + name), is_service=(name == "ai"))
            s.add(u)
            s.flush()
            t.users[name] = u.id
            for role, scope in roles:
                s.add(RoleAssignment(organization_id=org.id, user_id=u.id, role=role,
                                     scope_type="organization" if scope == "org" else "portfolio",
                                     scope_id=None if scope == "org" else t.portfolios[scope]))
        seed_org_defaults(s, org.id)
    return t


@pytest.fixture
def tenant() -> Tenant:
    return make_tenant()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
