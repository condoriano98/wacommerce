"""State for mocked providers (DOKU, Biteship, Meta, Chatwoot) — never used for live traffic.

By default state is in memory. When ``OMS_MOCK_PROVIDER_DIR`` is set (tests that kill worker processes), each provider's
effects are kept in a JSON file guarded by ``flock`` so they survive the process: that is how the reliability tests
count *provider-side* effects independently of the OMS database.

Fault injection (tests only): ``OMS_MOCK_FAULT="<provider>.<operation>:<fault>"`` with fault
``kill_after_accept`` (the provider records the effect, then the calling process is SIGKILLed before it can commit) or
``timeout_after_accept`` (effect recorded, then a ``TimeoutError`` is raised).
"""

import fcntl
import json
import os
import signal
from contextlib import contextmanager
from pathlib import Path

_MEMORY: dict[str, dict] = {}


def _dir() -> Path | None:
    d = os.environ.get("OMS_MOCK_PROVIDER_DIR")
    return Path(d) if d else None


@contextmanager
def state(provider: str):
    """Yield a mutable dict for the provider; persisted atomically when a directory is configured."""
    d = _dir()
    if d is None:
        yield _MEMORY.setdefault(provider, {})
        return
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{provider}.json"
    with open(d / f"{provider}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(path.read_text()) if path.exists() else {}
        yield data
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, default=str))
        os.replace(tmp, path)


def reset(provider: str | None = None) -> None:
    for p in [provider] if provider else list(_MEMORY):
        _MEMORY.pop(p, None)
    d = _dir()
    if d and d.exists():
        for f in d.glob(f"{provider or '*'}.json"):
            f.unlink()


def fault_after_accept(provider: str, operation: str) -> None:
    spec = os.environ.get("OMS_MOCK_FAULT", "")
    target, _, fault = spec.partition(":")
    if target != f"{provider}.{operation}":
        return
    if fault == "kill_after_accept":
        os.kill(os.getpid(), signal.SIGKILL)
    if fault == "timeout_after_accept":
        raise TimeoutError(f"mock {provider} timed out after accepting {operation}")
