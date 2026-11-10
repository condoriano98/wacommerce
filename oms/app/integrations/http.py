"""Shared outbound HTTP policy for live provider clients (M5): explicit timeouts and a per-(provider, account)
circuit breaker. An open circuit fails fast with ``NotSent`` — the request provably never left — so ADR-015 callers
may retry later without an uncertain outcome. State is per process (each worker trips independently); provider
errors are also counted in ``/metrics``.
"""

import threading
import time
from dataclasses import dataclass, field

import httpx

from app.core.external_ops import NotSent
from app.core.observability import METRICS

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


@dataclass
class Breaker:
    threshold: int = 5
    cooldown: float = 60.0
    failures: int = 0
    opened_at: float | None = None
    half_open: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self) -> bool:
        with self.lock:
            if self.opened_at is None:
                return True
            if time.monotonic() - self.opened_at >= self.cooldown and not self.half_open:
                self.half_open = True  # one probe request
                return True
            return False

    def record(self, ok: bool) -> None:
        with self.lock:
            if ok:
                self.failures, self.opened_at, self.half_open = 0, None, False
                return
            self.failures += 1
            if self.half_open or self.failures >= self.threshold:
                self.opened_at, self.half_open = time.monotonic(), False


_BREAKERS: dict[str, Breaker] = {}
_LOCK = threading.Lock()


def breaker(key: str) -> Breaker:
    with _LOCK:
        return _BREAKERS.setdefault(key, Breaker())


def request(method: str, url: str, *, breaker_key: str, provider: str, **kwargs) -> httpx.Response:
    b = breaker(breaker_key)
    if not b.allow():
        METRICS.inc("provider_circuit_open_total", {"provider": provider})
        raise NotSent(f"circuit open for {breaker_key}")
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    try:
        r = httpx.request(method, url, **kwargs)
    except httpx.ConnectError as exc:
        b.record(False)
        METRICS.inc("provider_errors_total", {"provider": provider, "kind": "connect"})
        raise NotSent(str(exc)) from exc
    except httpx.TimeoutException:
        b.record(False)
        METRICS.inc("provider_errors_total", {"provider": provider, "kind": "timeout"})
        raise  # may have been sent: callers treat as uncertain
    b.record(r.status_code < 500 and r.status_code != 429)
    if r.status_code >= 500 or r.status_code == 429:
        METRICS.inc("provider_errors_total", {"provider": provider, "kind": str(r.status_code)})
    return r
