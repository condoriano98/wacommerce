from fastapi import FastAPI

import app.registry  # noqa: F401  (handlers + subscribers)
from app.api import agent, foundation, fpa, imports, operations, reads, webhooks

app = FastAPI(title="wacommerce OMS", version="0.1.0")
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
