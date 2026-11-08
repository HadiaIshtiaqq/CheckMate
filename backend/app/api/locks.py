"""Per-user serialisation of state-changing requests, so two clicks (or two tabs)
can't interleave contract/payment state transitions. Background jobs take the same
lock. Single-process only: with several API instances use row locks
(SELECT ... FOR UPDATE on PostgreSQL) instead."""
from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Depends, HTTPException

from app.database.models import User
from app.security.auth import current_user

_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_guard = threading.Lock()


def _lock_for(user_id: str) -> threading.Lock:
    with _guard:
        return _locks[user_id]


@contextmanager
def user_lock(user_id: str, timeout: float = 30.0) -> Iterator[None]:
    lock = _lock_for(user_id)
    if not lock.acquire(timeout=timeout):
        raise HTTPException(409, "Another action on your account is still in progress; please retry.")
    try:
        yield
    finally:
        lock.release()


def serialized_user(user: User = Depends(current_user)) -> Iterator[User]:
    with user_lock(user.id):
        yield user
