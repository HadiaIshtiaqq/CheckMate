"""Background jobs: payment sweeper and scheduled Monitoring Agent passes.
Runs in a daemon thread inside the API process (replace with Celery beat / a
worker process when scaling out)."""
from __future__ import annotations

import logging
import threading
import time

from app.core.config import get_settings

log = logging.getLogger("checkmate.jobs")
_stop = threading.Event()


def run_once() -> dict:
    from app.orchestrator.service import monitor_active_contracts, sweep_payments

    out = {"settled_payments": [], "monitored": []}
    try:
        out["settled_payments"] = sweep_payments()
    except Exception:  # noqa: BLE001
        log.exception("payment sweep failed")
    try:
        out["monitored"] = monitor_active_contracts()
    except Exception:  # noqa: BLE001
        log.exception("scheduled monitoring failed")
    return out


def _loop(interval: float) -> None:
    while not _stop.wait(interval):
        result = run_once()
        if result["settled_payments"]:
            log.info("payment sweeper settled %s", result["settled_payments"])


def start() -> threading.Thread | None:
    s = get_settings()
    if not s.jobs_enabled:
        return None
    _stop.clear()
    t = threading.Thread(target=_loop, args=(s.jobs_interval_seconds,), name="checkmate-jobs", daemon=True)
    t.start()
    return t


def stop() -> None:
    _stop.set()
