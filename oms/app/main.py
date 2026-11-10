from fastapi import FastAPI

import app.registry  # noqa: F401  (handlers + subscribers)
from app.api import foundation, webhooks

app = FastAPI(title="wacommerce OMS", version="0.1.0")
app.include_router(foundation.router)
app.include_router(webhooks.router)


@app.get("/healthz", tags=["ops"])
def healthz() -> dict:
    return {"status": "ok"}
