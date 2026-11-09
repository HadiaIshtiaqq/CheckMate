"""Accounts, mandates, receipts, image proxy, passport verification and live updates."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit
import json
import re
import threading
from collections import OrderedDict

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.locks import serialized_user
from app.api.views import event_view
from app.core.config import get_settings
from app.database.models import ExternalOffer, PurchasingMandate, ReceiptFile, User, Workflow
from app.database.session import SessionLocal, get_db
from app.orchestrator import service as svc
from app.orchestrator.events import list_events
from app.orchestrator.state_machine import TERMINAL, WS
from app.security.auth import (
    client_ip,
    create_session,
    current_user,
    end_session,
    hash_password,
    hit,
    verify_password,
)
from app.core.util import new_id, utcnow

router = APIRouter(prefix="/api/v1")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")


# ================================================================ accounts ===
class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


def _account(user: User) -> dict:
    return {"id": user.id, "email": user.email, "display_name": user.display_name}


def _login_throttle(request: Request, email: str) -> None:
    key_ip = f"login-ip:{client_ip(request)}"
    key_email = f"login-email:{email.lower()}"
    import hashlib

    if not hit(hashlib.sha256(key_ip.encode()).hexdigest()[:40], 20, 600) or \
            not hit(hashlib.sha256(key_email.encode()).hexdigest()[:40], 10, 600):
        raise HTTPException(429, "Too many sign-in attempts. Wait a few minutes and try again.")


@router.post("/auth/register", status_code=201)
def register(body: Credentials, request: Request, response: Response, db: Session = Depends(get_db)):
    if not get_settings().allow_registration:
        raise HTTPException(403, "Registration is closed")
    _login_throttle(request, body.email)
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address")
    if len(body.password) < 10:
        raise HTTPException(422, "Use a password of at least 10 characters")
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "An account with this email already exists")
    user = User(id=new_id("usr"), display_name=email.split("@")[0], email=email,
                password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    create_session(db, user, response)
    return _account(user)


@router.post("/auth/login")
def login(body: Credentials, request: Request, response: Response, db: Session = Depends(get_db)):
    _login_throttle(request, body.email)
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Email or password is incorrect")
    create_session(db, user, response)
    return _account(user)


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    end_session(db, request, response)
    return {"ok": True}


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return _account(user)


# ================================================================ mandates ===
class MandateIn(BaseModel):
    max_per_purchase: str = Field(max_length=14)
    monthly_cap: str = Field(max_length=14)
    currency: str = Field(min_length=3, max_length=3)
    allowed_stores: list[str] = Field(default_factory=list, max_length=20)
    allowed_keywords: list[str] = Field(default_factory=list, max_length=20)
    require_established_store: bool = True
    days: int = 30


def _mandate_view(db: Session, m: PurchasingMandate) -> dict:
    return {"id": m.id, "max_per_purchase": m.max_per_purchase, "monthly_cap": m.monthly_cap,
            "currency": m.currency, "allowed_stores": m.allowed_stores, "allowed_keywords": m.allowed_keywords,
            "require_established_store": m.require_established_store, "status":
                "EXPIRED" if m.status == "ACTIVE" and m.expires_at <= utcnow() else m.status,
            "expires_at": m.expires_at.isoformat(), "created_at": m.created_at.isoformat(),
            "spent_this_month": str(svc.month_spend(db, m.user_id, m.currency))}


@router.get("/mandates")
def list_mandates(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(PurchasingMandate).where(PurchasingMandate.user_id == user.id)
                      .order_by(PurchasingMandate.created_at.desc())).all()
    return [_mandate_view(db, m) for m in rows]


@router.post("/mandates", status_code=201)
def create_mandate(body: MandateIn, user: User = Depends(serialized_user), db: Session = Depends(get_db)):
    m = svc.create_mandate(db, user.id, max_per_purchase=body.max_per_purchase, monthly_cap=body.monthly_cap,
                           currency=body.currency, allowed_stores=body.allowed_stores,
                           allowed_keywords=body.allowed_keywords,
                           require_established_store=body.require_established_store, days=body.days)
    return _mandate_view(db, m)


@router.post("/mandates/{mandate_id}/revoke")
def revoke_mandate(mandate_id: str, user: User = Depends(serialized_user), db: Session = Depends(get_db)):
    return _mandate_view(db, svc.revoke_mandate(db, user.id, mandate_id))


# ============================================================ offers/receipts ===
@router.post("/workflows/{workflow_id}/refresh-offer")
def refresh_offer(workflow_id: str, user: User = Depends(serialized_user), db: Session = Depends(get_db)):
    return svc.refresh_offer(db, user.id, workflow_id)


@router.post("/contracts/{contract_id}/receipts", status_code=201)
async def upload_receipt(contract_id: str, file: UploadFile = File(...), user: User = Depends(serialized_user),
                         db: Session = Depends(get_db)):
    limit = get_settings().receipt_max_bytes
    data = await file.read(limit + 1)
    rf = await run_in_threadpool(svc.save_receipt, db, user.id, contract_id, file.filename or "receipt",
                                 file.content_type or "", data)
    return svc.latest_receipt(db, rf.contract_id)


@router.get("/receipts/{receipt_id}")
def download_receipt(receipt_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rf = db.get(ReceiptFile, receipt_id)
    if not rf or rf.user_id != user.id:
        raise svc.NotFound("Receipt not found")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", rf.filename)[:100] or "receipt"
    return Response(rf.data, media_type=rf.content_type,
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


# ============================================================== image proxy ===
def _public_https(url: str) -> bool:
    """True only for https URLs whose host resolves exclusively to public addresses (SSRF guard)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    addrs = {ipaddress.ip_address(i[4][0]) for i in infos}
    return bool(addrs) and all(a.is_global for a in addrs)


_images: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_images_lock = threading.Lock()


@router.get("/images/{offer_id}")
def offer_image(offer_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Serves a listing's thumbnail through the backend, so the shopper's browser
    never contacts third-party image hosts directly."""
    with _images_lock:
        if offer_id in _images:
            _images.move_to_end(offer_id)
            data, ctype = _images[offer_id]
            return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=3600"})
    row = db.get(ExternalOffer, offer_id)
    url = (row.data or {}).get("thumbnail") if row else None
    if not url or not url.startswith("https://"):
        raise svc.NotFound("Image not found")
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as http:
            for _ in range(4):  # follow redirects manually so every hop is checked
                if not _public_https(url):
                    raise HTTPException(502, "Image unavailable")
                r = http.get(url)
                if r.is_redirect and r.headers.get("location"):
                    url = str(r.url.join(r.headers["location"]))
                    continue
                break
    except httpx.HTTPError as e:
        raise HTTPException(502, "Image unavailable") from e
    ctype = r.headers.get("content-type", "").split(";")[0]
    if r.status_code != 200 or not ctype.startswith("image/") or ctype == "image/svg+xml" or len(r.content) > 2_000_000:
        raise HTTPException(502, "Image unavailable")
    with _images_lock:
        _images[offer_id] = (r.content, ctype)
        while len(_images) > 300:
            _images.popitem(last=False)
    return Response(r.content, media_type=ctype, headers={"Cache-Control": "private, max-age=3600"})


# ======================================================== client ip check ===
@router.get("/system/client-ip")
def my_client_ip(request: Request, user: User = Depends(current_user)):
    """The address rate limits use for *you* (helps confirm proxy setup on a host)."""
    seen = [h for h in ("true-client-ip", "cf-connecting-ip", "x-forwarded-for") if request.headers.get(h)]
    return {"ip": client_ip(request), "proxy_headers_present": seen}


# ======================================================== passport verify ===
@router.get("/passports/{passport_id}/verify")
def verify_passport(passport_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return svc.verify_passport(db, user.id, passport_id)


# ============================================================= live updates ===
@router.get("/workflows/{workflow_id}/stream")
async def stream(workflow_id: str, request: Request, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    """Server-sent events: pushes new workflow events as they happen."""
    svc.owned_workflow(db, user.id, workflow_id)

    def poll(after: int) -> tuple[list[dict], str]:
        with SessionLocal() as s:
            evs = [event_view(e) for e in list_events(s, workflow_id, after)]
            wf = s.get(Workflow, workflow_id)
            return evs, wf.state if wf else WS.CANCELLED.value

    async def gen():
        after, idle = 0, 0
        yield "retry: 3000\n\n"
        for _ in range(600):  # at most ~10 minutes per connection; the browser reconnects
            if await request.is_disconnected():
                return
            evs, state = await run_in_threadpool(poll, after)
            for e in evs:
                after = max(after, e["seq"])
                yield f"id: {e['seq']}\nevent: workflow\ndata: {json.dumps(e)}\n\n"
            idle = 0 if evs else idle + 1
            if idle and idle % 15 == 0:
                yield ": keep-alive\n\n"
            if WS(state) in TERMINAL and not evs:
                yield "event: done\ndata: {}\n\n"
                return
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/account/summary")
def account_summary(user: User = Depends(current_user), db: Session = Depends(get_db)):
    n = db.scalar(select(func.count()).select_from(Workflow).where(Workflow.user_id == user.id)) or 0
    return {"workflows": n}
