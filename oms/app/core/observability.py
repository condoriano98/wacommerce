"""Observability (M5): structured JSON logs with correlation IDs, request metrics, Prometheus text exposition.

* A correlation ID (``X-Request-ID`` or generated) is bound per request, copied into outbox payloads (``_cid``) and
  re-bound when the worker processes the message, so request → job → provider call share one ID.
* Logs never contain request bodies, headers, tokens or secrets: only method, route template, status and timing.
* ``/metrics`` (bearer-protected; disabled when no token is configured) exposes in-process request metrics plus
  database aggregates from ``ops_metrics()`` (counts and amounts only, no tenant identifiers).
"""

import json
import logging
import threading
import time
import uuid
from contextvars import ContextVar

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")
BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)
SECRET_KEYS = ("authorization", "password", "secret", "token", "signature", "api_key", "ciphertext")


def new_cid() -> str:
    return uuid.uuid4().hex


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)), "level": record.levelname,
               "logger": record.name, "msg": record.getMessage(), "cid": correlation_id.get() or None}
        for k, v in (getattr(record, "fields", None) or {}).items():
            out[k] = "[redacted]" if any(s in k.lower() for s in SECRET_KEYS) else v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info).splitlines()[-1][:500]
        return json.dumps(out, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    h = logging.StreamHandler()
    h.setFormatter(JsonFormatter())
    root.addHandler(h)
    root.setLevel(level)


class Metrics:
    """Minimal thread-safe registry (counters + histograms) rendered in Prometheus text format."""

    def __init__(self):
        self._lock = threading.Lock()
        self.counters: dict[tuple, float] = {}
        self.hist: dict[tuple, list] = {}

    def inc(self, name: str, labels: dict, value: float = 1) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self.counters[key] = self.counters.get(key, 0) + value

    def observe(self, name: str, labels: dict, value: float) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            h = self.hist.setdefault(key, [[0] * len(BUCKETS), 0.0, 0])
            for i, b in enumerate(BUCKETS):
                if value <= b:
                    h[0][i] += 1
            h[1] += value
            h[2] += 1

    def render(self, extra: list[tuple[str, dict, float]] = ()) -> str:
        def fmt(labels) -> str:
            items = labels if isinstance(labels, tuple) else tuple(sorted(labels.items()))
            return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in items) + "}" if items else ""

        lines = []
        with self._lock:
            for (name, labels), v in sorted(self.counters.items()):
                lines.append(f"oms_{name}{fmt(labels)} {v}")
            for (name, labels), (counts, total, n) in sorted(self.hist.items()):
                for b, c in zip(BUCKETS, counts, strict=True):
                    lines.append(f"oms_{name}_bucket{fmt(labels + (('le', str(b)),))} {c}")
                lines.append(f"oms_{name}_bucket{fmt(labels + (('le', '+Inf'),))} {n}")
                lines.append(f"oms_{name}_sum{fmt(labels)} {total}")
                lines.append(f"oms_{name}_count{fmt(labels)} {n}")
        for name, labels, v in extra:
            lines.append(f"oms_{name}{fmt(labels)} {v}")
        return "\n".join(lines) + "\n"


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


METRICS = Metrics()
access_log = logging.getLogger("oms.access")


def install(app) -> None:
    """Correlation-ID + metrics middleware (pure ASGI; no body access)."""

    async def middleware(scope, receive, send, _app=app.router):
        if scope["type"] != "http":
            return await _app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        cid = (headers.get(b"x-request-id") or b"").decode()[:64] or new_cid()
        token = correlation_id.set(cid)
        start = time.perf_counter()
        status_code = {"v": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_code["v"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", cid.encode()))
            await send(message)

        try:
            await _app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            path = getattr(route, "path", None) or "unmatched"
            dur = time.perf_counter() - start
            labels = {"method": scope["method"], "route": path, "status": str(status_code["v"])}
            METRICS.inc("http_requests_total", labels)
            METRICS.observe("http_request_seconds", {"route": path}, dur)
            access_log.info("request", extra={"fields": {**labels, "ms": round(dur * 1000, 1)}})
            correlation_id.reset(token)

    app.add_middleware(_Asgi, inner=middleware)


class _Asgi:
    def __init__(self, app, inner):
        self.app, self.inner = app, inner

    async def __call__(self, scope, receive, send):
        await self.inner(scope, receive, send, _app=self.app)


def db_metrics() -> list[tuple[str, dict, float]]:
    """Aggregates from ``ops_metrics()`` via the API role (no worker credentials in the API process)."""
    from sqlalchemy import text

    from app.core.db import tenant_session

    with tenant_session(None, None) as s:
        rows = s.execute(text("select metric, label, value from ops_metrics()")).all()
    return [(m, {"label": lbl} if lbl else {}, float(v)) for m, lbl, v in rows if m != "schema_version"]


def schema_version() -> str | None:
    from sqlalchemy import text

    from app.core.db import tenant_session

    with tenant_session(None, None) as s:
        return s.execute(text("select label from ops_metrics() where metric = 'schema_version'")).scalar()
