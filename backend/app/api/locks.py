"""Per-user serialisation of state-changing requests, so two clicks, tabs or API
processes can't interleave contract/payment state transitions.

* PostgreSQL: a session-level advisory lock on a dedicated connection, so the lock
  holds across every API process and worker.
* SQLite: an in-process lock (SQLite deployments are single-process by nature).
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Depends, HTTPException
from sqlalchemy import text

from app.database.models import User
from app.database.session import engine
from app.security.auth import current_user

_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_guard = threading.Lock()
IS_POSTGRES = engine.dialect.name == "postgresql"


def lock_key(name: str) -> int:
    """Stable signed 64-bit key for pg_advisory_lock."""
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


@contextmanager
def named_lock(name: str, timeout: float = 30.0) -> Iterator[None]:
    if IS_POSTGRES:
        conn = engine.connect()
        try:
            deadline = time.monotonic() + timeout
            while not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key(name)}).scalar():
                if time.monotonic() > deadline:
                    raise HTTPException(409, "Another action on your account is still in progress; please retry.")
                time.sleep(0.1)
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key(name)})
        finally:
            conn.close()
        return
    with _guard:
        lock = _locks[name]
    if not lock.acquire(timeout=timeout):
        raise HTTPException(409, "Another action on your account is still in progress; please retry.")
    try:
        yield
    finally:
        lock.release()


@contextmanager
def user_lock(user_id: str, timeout: float = 30.0) -> Iterator[None]:
    with named_lock(f"user:{user_id}", timeout):
        yield


def serialized_user(user: User = Depends(current_user)) -> Iterator[User]:
    with user_lock(user.id):
        yield user
