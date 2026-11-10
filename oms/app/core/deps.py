import uuid
from collections.abc import Iterator
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.authz import Assignment, Principal
from app.core.db import tenant_session
from app.core.security import decode_token
from app.models.org import RoleAssignment, User


def _principal_from_header(authorization: Annotated[str | None, Header()] = None) -> tuple[uuid.UUID, uuid.UUID]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        claims = decode_token(authorization.split(" ", 1)[1])
        return uuid.UUID(claims["sub"]), uuid.UUID(claims["org"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc


def get_session(ids: Annotated[tuple, Depends(_principal_from_header)]) -> Iterator[Session]:
    user_id, org_id = ids
    with tenant_session(org_id, user_id) as session:
        yield session


# scope="function": the transaction commits when the endpoint returns, *before* the response is sent,
# so a failed commit (e.g. an unbalanced journal) becomes a 500 instead of a false 200.
SessionDep = Depends(get_session, scope="function")


def get_principal(
    ids: Annotated[tuple, Depends(_principal_from_header)], session: Annotated[Session, SessionDep]
) -> Principal:
    user_id, org_id = ids
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "inactive user")
    # RLS lets a user read their own assignments.
    rows = session.scalars(select(RoleAssignment).where(RoleAssignment.user_id == user_id)).all()
    return Principal(
        user_id=user_id,
        org_id=org_id,
        is_service=user.is_service,
        assignments=[Assignment(r.role, r.scope_type, r.scope_id) for r in rows],
    )


DbSession = Annotated[Session, SessionDep]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
