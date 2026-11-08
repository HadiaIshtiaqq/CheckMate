"""Authentication, sessions, rate limiting, tracing and log hygiene.

* Accounts: email + password (scrypt). Sessions are random tokens in an
  HttpOnly, SameSite=Lax cookie; only a SHA-256 of the token is stored.
* State-changing requests made with a session cookie must come from an allowed
  Origin (CSRF protection).
* Bearer tokens (CHECKMATE_DEMO_USERS) exist only for tests/automation and are
  off unless configured; production refuses publicly known tokens.
* Rate limiting is stored in the database, so every API process shares it.
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.core.util import constant_time_equals, utcnow
from app.database.models import AuthSession, User
from app.database.session import SessionLocal, get_db

SESSION_COOKIE = "cm_session"
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")


# ================================================================ passwords ===
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = (stored or "").split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64), n=int(n), r=int(r),
                            p=int(p), dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


# ================================================================= sessions ===
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: Session, user: User, response: Response) -> None:
    s = get_settings()
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(id=_token_hash(token), user_id=user.id, expires_at=utcnow() + timedelta(days=s.session_days)))
    db.commit()
    response.set_cookie(SESSION_COOKIE, token, max_age=s.session_days * 86400, httponly=True, samesite="lax",
                        secure=s.secure_cookies, path="/")


def end_session(db: Session, request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        row = db.get(AuthSession, _token_hash(token))
        if row:
            row.revoked = True
            db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")


def _session_user_id(db: Session, token: str | None) -> str | None:
    if not token:
        return None
    row = db.get(AuthSession, _token_hash(token))
    if not row or row.revoked or row.expires_at <= utcnow():
        return None
    return row.user_id


def _bearer_user_id(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    for t, uid in get_settings().demo_users.items():
        if token and constant_time_equals(t, token):
            return uid
    return None


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/") in {o.rstrip("/") for o in get_settings().allowed_origins}
    return request.headers.get("sec-fetch-site") in ("same-origin", "none")


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = _bearer_user_id(request)
    if uid is None and request.headers.get("authorization"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
    if uid is None:
        uid = _session_user_id(db, request.cookies.get(SESSION_COOKIE))
        if uid and request.method not in ("GET", "HEAD", "OPTIONS") and not _origin_allowed(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Request origin not allowed")
    if uid is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required")
    user = db.get(User, uid)
    if user is None:  # bearer-token users are provisioned on first use
        user = User(id=uid, display_name=uid)
        db.add(user)
        db.commit()
    return user


# ============================================================ rate limiting ===
def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if peer in get_settings().trusted_proxies:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return peer


def hit(key: str, limit: int, window_seconds: int = 60) -> bool:
    """Atomically count a hit in the shared rate_counters table. True if allowed."""
    window = int(time.time() // window_seconds)
    with SessionLocal() as db:
        count = db.execute(text(
            "INSERT INTO rate_counters (key, window_start, count) VALUES (:k, :w, 1) "
            "ON CONFLICT (key) DO UPDATE SET "
            "count = CASE WHEN rate_counters.window_start = :w THEN rate_counters.count + 1 ELSE 1 END, "
            "window_start = :w RETURNING count"), {"k": key, "w": window}).scalar()
        db.commit()
    return (count or 0) <= limit


def _rate_key(request: Request) -> str:
    uid = _bearer_user_id(request)
    if uid is None:
        with SessionLocal() as db:
            uid = _session_user_id(db, request.cookies.get(SESSION_COOKIE))
    # Valid users get their own bucket; anything else shares its IP's bucket, so made-up
    # tokens or cookies can't mint fresh allowances. Raw credentials are never stored.
    raw = f"user:{uid}" if uid else f"ip:{client_ip(request)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, per_minute: int):
        super().__init__(app)
        self.per_minute = per_minute

    async def dispatch(self, request: Request, call_next):
        key = await run_in_threadpool(_rate_key, request)
        allowed = await run_in_threadpool(hit, key, self.per_minute)
        if not allowed:
            return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"})
        return await call_next(request)


# ================================================================== tracing ===
_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
access_log = logging.getLogger("checkmate.access")


class TracingMiddleware(BaseHTTPMiddleware):
    """W3C traceparent-compatible request tracing: continues an incoming trace or
    starts one, tags every log line with it, and returns it to the caller."""

    async def dispatch(self, request: Request, call_next):
        m = _TRACEPARENT.match(request.headers.get("traceparent", ""))
        trace_id = m.group(1) if m else uuid.uuid4().hex
        token = trace_id_var.set(trace_id)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            trace_id_var.reset(token)
        response.headers["traceparent"] = f"00-{trace_id}-{secrets.token_hex(8)}-01"
        response.headers["X-Request-ID"] = trace_id
        access_log.info("%s %s -> %s in %dms", request.method, request.url.path, response.status_code,
                        int((time.perf_counter() - t0) * 1000), extra={"trace_id": trace_id})
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Cache-Control", "no-store")
        if get_settings().secure_cookies:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


# ============================================================= log hygiene ===
class RedactSecretsFilter(logging.Filter):
    """Keeps credentials out of logs: every configured secret verbatim, plus generic
    patterns for bearer tokens, session cookies and key/token/password parameters."""

    _patterns = [
        re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=\-]+", re.I),
        re.compile(r"(\b(?:api_key|apikey|key|token|access_token|password|secret|client_secret|ct|"
                   + SESSION_COOKIE + r")[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,;]+", re.I),
        re.compile(r"(x-goog-api-key[\"']?\s*[:=]\s*[\"']?)[^\s\"',]+", re.I),
    ]

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
               "trace_id": getattr(record, "trace_id", None) or trace_id_var.get(), "msg": record.getMessage()}
        if record.exc_text:
            out["exc"] = record.exc_text
        return json.dumps(out, ensure_ascii=False)
