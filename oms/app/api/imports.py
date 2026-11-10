"""CSV imports (fallback for every provider without API access). Dry-run first, then commit."""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.core.deps import CurrentPrincipal, DbSession
from app.modules import imports

router = APIRouter(prefix="/imports", tags=["imports"])


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_name: str = Field(max_length=200)
    content: str = Field(max_length=5_000_000)
    dry_run: bool = True
    platform_account_id: uuid.UUID | None = None
    bank_account_id: uuid.UUID | None = None


@router.post("/{source}")
def run(source: str, body: ImportIn, session: DbSession, principal: CurrentPrincipal) -> dict:
    return imports.run_import(session, principal, source, body.file_name, body.content, dry_run=body.dry_run,
                              platform_account_id=body.platform_account_id, bank_account_id=body.bank_account_id)


@router.get("")
def batches(session: DbSession, principal: CurrentPrincipal) -> list[dict]:
    rows = session.execute(text("select id, source, file_name, file_sha256, status, stats, created_at "
                                "from import_batches order by created_at desc limit 200")).mappings().all()
    return [{**dict(r), "id": str(r["id"]), "created_at": r["created_at"].isoformat()} for r in rows]
