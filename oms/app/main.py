from fastapi import FastAPI

from app.api import foundation

app = FastAPI(title="wacommerce OMS", version="0.1.0")
app.include_router(foundation.router)


@app.get("/healthz", tags=["ops"])
def healthz() -> dict:
    return {"status": "ok"}
