import hmac

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse

import app.registry  # noqa: F401  (handlers + subscribers)
from app.api import agent, foundation, fpa, imports, operations, reads, webhooks
from app.core import observability  # noqa: E402
from app.core.config import get_settings  # noqa: E402

observability.configure_logging()
app = FastAPI(title="wacommerce OMS", version="0.1.0")
observability.install(app)
app.include_router(foundation.router)
app.include_router(webhooks.router)
app.include_router(agent.router)
app.include_router(operations.router)
app.include_router(fpa.router)
app.include_router(reads.router)
app.include_router(imports.router)


@app.get("/healthz", tags=["ops"])
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
def readyz() -> dict:
    """Ready when the database answers and the schema is at the migration head this build expects."""
    from alembic.script import ScriptDirectory

    from app.core.alembic_cfg import alembic_config

    expected = ScriptDirectory.from_config(alembic_config()).get_current_head()
    current = observability.schema_version()
    if current != expected:
        raise HTTPException(503, {"error": "schema_mismatch", "current": current, "expected": expected})
    return {"status": "ready", "schema": current}


@app.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
def metrics(authorization: str | None = Header(default=None)) -> str:
    token = get_settings().metrics_token
    if not token:
        raise HTTPException(404, "metrics disabled")
    if not authorization or not hmac.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(401, "invalid metrics token")
    return observability.METRICS.render(observability.db_metrics())
