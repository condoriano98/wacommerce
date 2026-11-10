# OMS API and worker images (no secrets baked in; all configuration via environment / secret store).
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN pip install --no-cache-dir uv==0.9.5 && useradd --create-home --uid 10001 oms
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev
USER oms
ENV PATH="/app/.venv/bin:$PATH"

FROM base AS api
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log"]

FROM base AS worker
CMD ["python", "-m", "app.worker"]

# One-off migration job (ADR-014): run with the owner role and lock_timeout; never part of app start-up.
FROM base AS migrate
CMD ["alembic", "upgrade", "head"]
