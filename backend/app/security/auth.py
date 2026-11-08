"""Bearer-token authentication for the demo. Tokens map to users via the
CHECKMATE_DEMO_USERS environment variable. Replace with a real identity
provider (OIDC) before any production use."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.core.util import constant_time_equals
from app.database.models import User
from app.database.session import get_db

_bearer = HTTPBearer(auto_error=False)


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                 db: Session = Depends(get_db)) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required",
                            headers={"WWW-Authenticate": "Bearer"})
    user_id = None
    for token, uid in get_settings().demo_users.items():
        if constant_time_equals(token, creds.credentials):
            user_id = uid
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
    user = db.get(User, user_id)
    if user is None:
        user = User(id=user_id, display_name=user_id.capitalize())
        db.add(user)
        db.commit()
    return user


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window limiter per client (token or IP). In-process only; use a
    shared store (Redis) when running multiple API instances."""

    def __init__(self, app, per_minute: int):
        super().__init__(app)
        self.per_minute = per_minute
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()
        self._requests = 0

    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        user = next((uid for t, uid in get_settings().demo_users.items() if token and constant_time_equals(t, token)),
                    None)
        # Valid users get their own bucket; everything else shares the caller's IP bucket, so rotating
        # made-up tokens cannot bypass the limit. Raw tokens are never kept as keys.
        raw = f"user:{user}" if user else f"ip:{request.client.host if request.client else 'unknown'}"
        key = hashlib.sha256(raw.encode()).hexdigest()[:32]
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429,
                                    headers={"Retry-After": "60"})
            q.append(now)
            self._requests += 1
            if self._requests % 1000 == 0:  # prune idle clients
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > 60]:
                    del self._hits[k]
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Cache-Control", "no-store")
        return response


class RedactSecretsFilter(logging.Filter):
    """Keeps bearer tokens and PayPal credentials out of logs."""

    _patterns = [re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I),
                 re.compile(r"(client_secret[\"']?\s*[:=]\s*[\"']?)[^\s\"',]+", re.I)]

    def __init__(self):
        super().__init__()
        self._literals = sorted(get_settings().secret_values(), key=len, reverse=True)

    def redact(self, msg: str) -> str:
        for lit in self._literals:
            msg = msg.replace(lit, "[REDACTED]")
        for p in self._patterns:
            msg = p.sub(r"\1[REDACTED]", msg)
        return msg

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = self.redact(record.getMessage()), ()
        if record.exc_info:
            record.exc_text = self.redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record), "level": record.levelname, "logger": record.name,
               "msg": record.getMessage()}
        if record.exc_text:
            out["exc"] = record.exc_text
        return json.dumps(out, ensure_ascii=False)
