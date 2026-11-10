"""Database engines and tenant-scoped sessions (spec §7, ADR-007, ADR-014).

* ``app``     — API requests; identity = the authenticated user.
* ``worker``  — background jobs; identity = the organization's *system principal* (a service user with an
  explicit org-scope role). Subject to RLS: there is no BYPASSRLS runtime role in ``v31`` mode.
* ``webhook`` — inbound provider callbacks; tenant resolved by SECURITY DEFINER resolver functions that return only
  identifiers, then processing runs as the org's system principal.
Identity is always transaction-local (``set_config(..., true)``); pooled connections cannot leak it.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


@lru_cache
def app_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def worker_engine() -> Engine:
    s = get_settings()
    url = s.system_database_url if s.db_role_mode == "legacy" else s.worker_database_url
    return create_engine(url, pool_pre_ping=True)


@lru_cache
def webhook_engine() -> Engine:
    s = get_settings()
    url = s.system_database_url if s.db_role_mode == "legacy" else s.webhook_database_url
    return create_engine(url, pool_pre_ping=True)


def _set_identity(session: Session, org_id: uuid.UUID | None, user_id: uuid.UUID | None = None) -> None:
    session.execute(
        text("select set_config('app.org_id', :org, true), set_config('app.user_id', :usr, true)"),
        {"org": str(org_id) if org_id else "", "usr": str(user_id) if user_id else ""},
    )


@contextmanager
def _session(engine: Engine) -> Iterator[Session]:
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        session.begin()
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def tenant_session(org_id: uuid.UUID | None, user_id: uuid.UUID | None) -> Iterator[Session]:
    """RLS-restricted session for one authenticated identity; commits on success, rolls back on error."""
    with _session(app_engine()) as session:
        _set_identity(session, org_id, user_id)
        yield session


def system_principal_id(session: Session, org_id: uuid.UUID) -> uuid.UUID:
    pid = session.execute(text("select system_principal(:o)"), {"o": org_id}).scalar()
    if pid is None:
        raise RuntimeError(f"organization {org_id} has no system principal")
    return pid


def act_as_system(session: Session, org_id: uuid.UUID) -> uuid.UUID:
    """Scope an open worker/webhook transaction to an organization's system principal."""
    pid = system_principal_id(session, org_id)
    _set_identity(session, org_id, pid)
    return pid


@contextmanager
def worker_session(org_id: uuid.UUID | None = None) -> Iterator[Session]:
    with _session(worker_engine()) as session:
        if org_id:
            act_as_system(session, org_id)
        yield session


@contextmanager
def webhook_session() -> Iterator[Session]:
    """Unscoped until the caller resolves the tenant (resolvers) and calls ``act_as_system``."""
    with _session(webhook_engine()) as session:
        yield session


# Backward-compatible alias used by V3.0 call sites; resolves to the worker session (RLS-bound in v31 mode).
system_session = worker_session


def set_tenant(session: Session, org_id: uuid.UUID, user_id: uuid.UUID | None = None) -> None:
    if user_id is None:
        act_as_system(session, org_id)
    else:
        _set_identity(session, org_id, user_id)
