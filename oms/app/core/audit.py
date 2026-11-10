import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.governance import AuditLog


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal | uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def audit(
    session: Session,
    org_id: uuid.UUID,
    action: str,
    entity: str,
    entity_id: Any = None,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_type: str = "user",
    portfolio_id: uuid.UUID | None = None,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            organization_id=org_id,
            actor_user_id=actor_user_id,
            actor_type=actor_type,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            portfolio_id=portfolio_id,
            before=_jsonable(before),
            after=_jsonable(after),
        )
    )
