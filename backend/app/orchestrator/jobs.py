"""Background jobs: payment sweeper and scheduled Monitoring Agent passes.

Only one process runs them at a time: on PostgreSQL the job runner holds an
advisory lock (leader election), so several API processes never duplicate work;
on SQLite (single process) the runner always leads."""
from __future__ import annotations

import logging
import threading

from sqlalchemy import text

from app.core.config import get_settings
from app.database.session import engine

log = logging.getLogger("checkmate.jobs")
_stop = threading.Event()
_leader_conn = None


def is_leader() -> bool:
    """Try to become (or confirm being) the single job runner."""
    global _leader_conn
    if engine.dialect.name != "postgresql":
        return True
    from app.api.locks import lock_key

    if _leader_conn is None:
        _leader_conn = engine.connect()
    try:
        return bool(_leader_conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                         {"k": lock_key("checkmate:jobs-leader")}).scalar())
    except Exception:  # noqa: BLE001 - connection lost; retry next tick
        _leader_conn = None
        return False


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
    resumed = False
    while not _stop.is_set():
        if is_leader():
            if not resumed:
                from app.orchestrator.service import resume_incomplete

                resume_incomplete()  # only the leader resumes interrupted workflows
                resumed = True
            result = run_once()
            if result["settled_payments"]:
                log.info("payment sweeper settled %s", result["settled_payments"])
        if _stop.wait(interval):
            break


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
