"""Platform account registry and credential vault."""

import uuid
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.authz import Principal
from app.core.db import system_session
from app.core.security import decrypt_secret, encrypt_secret
from app.models.platforms import AccountCredential, AccountPortfolioLink, PlatformAccount


def register_account(session: Session, principal: Principal, *, provider: str, provider_account_id: str,
                     display_name: str, role: str, portfolio_ids: list[uuid.UUID],
                     public_config: dict | None = None, api_version: str | None = None,
                     brand_id: uuid.UUID | None = None) -> PlatformAccount:
    principal.require("credentials.manage")
    account = PlatformAccount(organization_id=principal.org_id, provider=provider,
                              provider_account_id=provider_account_id, display_name=display_name, role=role,
                              public_config=public_config or {}, api_version=api_version, brand_id=brand_id)
    session.add(account)
    session.flush()  # parent before children (no ORM relationships; see ADR-002)
    for pid in portfolio_ids:
        session.add(AccountPortfolioLink(organization_id=principal.org_id, platform_account_id=account.id,
                                         portfolio_id=pid))
    audit(session, principal.org_id, "platform_account.registered", "platform_account", account.id,
          actor_user_id=principal.user_id,
          after={"provider": provider, "role": role, "portfolios": portfolio_ids, "public_config": public_config})
    return account


def store_credential(session: Session, principal: Principal, account_id: uuid.UUID, name: str, secret: str,
                     expires_at: datetime | None = None) -> None:
    """Encrypts in the API process; the DB function checks owner permission and org ownership.
    The secret value never appears in responses, audit logs or application logs."""
    principal.require("credentials.manage")
    session.execute(text("select store_credential(:a, :n, :c, :e)"),
                    {"a": account_id, "n": name, "c": encrypt_secret(secret), "e": expires_at})
    audit(session, principal.org_id, "credential.stored", "platform_account", account_id,
          actor_user_id=principal.user_id, after={"name": name, "expires_at": expires_at})


def list_credentials(session: Session, account_id: uuid.UUID) -> list[dict]:
    rows = session.execute(text("select * from list_credentials(:a)"), {"a": account_id}).mappings().all()
    return [dict(r) for r in rows]


def read_secret(org_id: uuid.UUID, account_id: uuid.UUID, name: str) -> str | None:
    """Adapters only (system role). Never call from request handlers that return data to clients."""
    with system_session(org_id) as session:
        cred = session.scalars(select(AccountCredential).where(
            AccountCredential.platform_account_id == account_id, AccountCredential.name == name,
            AccountCredential.organization_id == org_id)).first()
        return decrypt_secret(cred.ciphertext) if cred else None


def require_live(account: PlatformAccount) -> None:
    if not account.live_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{account.provider} account is not enabled for live calls")


def serialize_account(a: PlatformAccount) -> dict:
    return {
        "id": str(a.id), "provider": a.provider, "provider_account_id": a.provider_account_id,
        "display_name": a.display_name, "role": a.role, "status": a.status, "live_enabled": a.live_enabled,
        "api_version": a.api_version, "webhook_status": a.webhook_status, "public_config": a.public_config,
        "last_sync_ok_at": a.last_sync_ok_at, "last_sync_failed_at": a.last_sync_failed_at,
        "last_error": a.last_error,
    }
