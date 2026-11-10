"""Database engines and tenant-scoped sessions.

Two engines exist on purpose:
* ``app`` engine connects as a role subject to Postgres row-level security. Every transaction sets
  ``app.org_id`` so policies only expose the caller's organization.
* ``system`` engine connects as a BYPASSRLS role. It is used only to resolve which organization an
  inbound webhook belongs to and by the outbox worker (which then sets the org explicitly).
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
    return create_engine(get_settings().database_url, pool_pre_ping=True, future=True)


@lru_cache
def system_engine() -> Engine:
    return create_engine(get_settings().system_database_url, pool_pre_ping=True, future=True)


def _set_identity(session: Session, org_id: uuid.UUID | None, user_id: uuid.UUID | None = None) -> None:
    # set_config(..., true) == SET LOCAL: scoped to the current transaction only. Only the identity
    # is passed; Postgres derives permitted portfolios from role_assignments.
    session.execute(
        text("select set_config('app.org_id', :org, true), set_config('app.user_id', :usr, true)"),
        {"org": str(org_id) if org_id else "", "usr": str(user_id) if user_id else ""},
    )


@contextmanager
def tenant_session(org_id: uuid.UUID | None, user_id: uuid.UUID | None) -> Iterator[Session]:
    """RLS-restricted session for one authenticated identity; commits on success, rolls back on error."""
    session = sessionmaker(bind=app_engine(), expire_on_commit=False)()
    try:
        session.begin()
        _set_identity(session, org_id, user_id)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def system_session(org_id: uuid.UUID | None = None) -> Iterator[Session]:
    session = sessionmaker(bind=system_engine(), expire_on_commit=False)()
    try:
        session.begin()
        if org_id:
            _set_identity(session, org_id)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def set_tenant(session: Session, org_id: uuid.UUID, user_id: uuid.UUID | None = None) -> None:
    """Re-scope an open transaction (used by the worker when switching organizations)."""
    _set_identity(session, org_id, user_id)
