"""Accounts & sessions, mandates (Mode B), price refresh, receipts, signed passports,
image proxy, live updates, tracing and migrations."""
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.database.models import PurchasePassportRow
from app.database.session import SessionLocal, engine
from app.integrations.merchants.router import CatalogRouter
from app.integrations.merchants.serpapi_shopping import SerpApiShoppingAdapter

from tests.conftest import A, HEADPHONES, approve, get_wf, paid, start
from tests.integration import test_real_stores as rs

ORIGIN = {"Origin": "http://frontend.test"}


@pytest.fixture(autouse=True)
def fresh_rate_counters(client):
    """Sign-in throttling is per IP and shared through the database; every test client
    has the same IP, so each test starts with clean counters."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM rate_counters"))


@pytest.fixture
def browser(client):
    """A cookie-carrying client, separate from the shared bearer-token client
    (depends on `client` so app startup / DB initialisation has run)."""
    return TestClient(client.app)


def register(c, email=None, password="correct horse battery"):
    email = email or f"u{uuid.uuid4().hex[:8]}@example.com"
    return c.post("/api/v1/auth/register", json={"email": email, "password": password}, headers=ORIGIN), email


# ================================================================ accounts ===
def test_register_login_logout_and_session_cookie(browser):
    r, email = register(browser)
    assert r.status_code == 201 and r.json()["email"] == email
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert browser.get("/api/v1/auth/me").json()["email"] == email
    assert browser.post("/api/v1/auth/logout", headers=ORIGIN).status_code == 200
    assert browser.get("/api/v1/auth/me").status_code == 401
    assert browser.post("/api/v1/auth/login", json={"email": email, "password": "wrong password!"},
                        headers=ORIGIN).status_code == 401
    assert browser.post("/api/v1/auth/login", json={"email": email, "password": "correct horse battery"},
                        headers=ORIGIN).status_code == 200
    assert browser.get("/api/v1/auth/me").status_code == 200


def test_registration_validation(browser):
    assert register(browser, password="short")[0].status_code == 422
    assert register(browser, email="not-an-email")[0].status_code == 422
    _, email = register(browser)
    assert register(TestClient(browser.app), email=email)[0].status_code == 409


def test_cookie_requests_from_other_origins_are_refused(browser):
    register(browser)
    ok = browser.post("/api/v1/shopping/intents", json={"text": HEADPHONES}, headers=ORIGIN)
    assert ok.status_code == 202
    evil = browser.post("/api/v1/shopping/intents", json={"text": HEADPHONES}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403


def test_accounts_are_isolated(browser):
    register(browser)
    wid = browser.post("/api/v1/shopping/intents", json={"text": HEADPHONES}, headers=ORIGIN).json()["workflow_id"]
    other = TestClient(browser.app)
    register(other)
    assert other.get(f"/api/v1/workflows/{wid}").status_code == 404
    assert browser.get(f"/api/v1/workflows/{wid}").status_code == 200


def test_login_is_throttled(browser):
    email = f"t{uuid.uuid4().hex[:8]}@example.com"
    codes = [browser.post("/api/v1/auth/login", json={"email": email, "password": "x" * 12},
                          headers=ORIGIN).status_code for _ in range(12)]
    assert 429 in codes


def test_passwords_are_hashed(browser):
    from app.database.models import User

    _, email = register(browser)
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == email))
        assert u.password_hash.startswith("scrypt$") and "correct horse" not in u.password_hash


# ================================================================ mandates ===
def make_mandate(client, **over):
    body = {"max_per_purchase": "120", "monthly_cap": "1000", "currency": "USD", "allowed_stores": [],
            "allowed_keywords": [], "require_established_store": True, "days": 30, **over}
    r = client.post("/api/v1/mandates", json=body, headers=A)
    assert r.status_code == 201, r.text
    return r.json()


def test_mandate_auto_approves_within_limits_and_revocation_withdraws(client):
    m = make_mandate(client)
    try:
        wf = start(client, mode="MANDATE")
        c = wf["contract"]
        assert c["approved"] and c["approved_by"] == f"mandate:{m['id']}"
        assert c["approvals"][-1]["signed"] is True
        r = client.post(f"/api/v1/mandates/{m['id']}/revoke", headers=A)
        assert r.json()["status"] == "REVOKED"
        assert get_wf(client, wf["id"])["contract"]["approved"] is False
        wf2 = start(client, mode="MANDATE")  # revoked mandates authorise nothing
        assert wf2["contract"]["approved"] is False
    finally:
        client.post(f"/api/v1/mandates/{m['id']}/revoke", headers=A)


def test_mandate_does_not_cover_purchases_over_its_limit(client):
    m = make_mandate(client, max_per_purchase="100", monthly_cap="100")
    try:
        wf = start(client, mode="MANDATE")
        assert wf["contract"]["approved"] is False
        ev = client.get(f"/api/v1/workflows/{wf['id']}/events", headers=A).json()["events"]
        assert any(e["type"] == "mandate.not_applied" and "per-purchase limit" in e["message"] for e in ev)
    finally:
        client.post(f"/api/v1/mandates/{m['id']}/revoke", headers=A)


def test_mandate_validation(client):
    bad = client.post("/api/v1/mandates", json={"max_per_purchase": "200", "monthly_cap": "100", "currency": "USD"},
                      headers=A)
    assert bad.status_code == 400


# ======================================================== refresh + receipts ===
def _flaky_price_handler():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        data = {"shopping_results": [dict(r) for r in rs.HEADPHONES_RESULTS["shopping_results"]]}
        if calls["n"] > 1:
            data["shopping_results"][0].update({"price": "$94.99", "extracted_price": 94.99})
        return httpx.Response(200, json=data)

    return handler


def test_refresh_offer_revises_plan_when_price_changes(client, monkeypatch):
    adapter = SerpApiShoppingAdapter("k", SessionLocal, transport=httpx.MockTransport(_flaky_price_handler()))
    rs.install(monkeypatch, CatalogRouter(None, adapter))
    monkeypatch.setattr(__import__("app.services.container", fromlist=["x"]).get_container(), "external", adapter)
    wf = start(client, rs.HEADPHONES)
    approve(client, wf)
    r = client.post(f"/api/v1/workflows/{wf['id']}/refresh-offer", headers=A).json()
    assert r == {"found": True, "changed": True, "price": "94.99"}
    after = get_wf(client, wf["id"])
    assert after["contract"]["version"] == 2 and after["contract"]["approved"] is False
    assert after["contract"]["terms"]["unit_price"] == "94.99"


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_receipt_upload_validation_and_passport_evidence(client, real_stores):
    wf = start(client, rs.HEADPHONES)
    approve(client, wf)
    cid = wf["contract"]["id"]
    bad = client.post(f"/api/v1/contracts/{cid}/receipts", files={"file": ("r.png", b"not an image", "image/png")},
                      headers=A)
    assert bad.status_code == 400
    ok = client.post(f"/api/v1/contracts/{cid}/receipts", files={"file": ("receipt.png", PNG, "image/png")}, headers=A)
    assert ok.status_code == 201
    sha = ok.json()["sha256"]
    r = rs.report(client, wf, "97.50", {"warranty": True, "subscription": True}).json()
    body = client.get(f"/api/v1/passports/{r['passport_id']}", headers=A).json()["body"]
    assert body["receipt_evidence"]["file"]["sha256"] == sha
    dl = client.get(f"/api/v1/receipts/{ok.json()['receipt_id']}", headers=A)
    assert dl.status_code == 200 and dl.content == PNG


@pytest.fixture
def real_stores(monkeypatch):
    adapter = SerpApiShoppingAdapter("test-serp-key", SessionLocal, transport=httpx.MockTransport(rs.handler))
    rs.install(monkeypatch, CatalogRouter(None, adapter))
    return adapter


# ========================================================== signed passports ===
def test_passport_signature_verifies_and_detects_tampering(client, paypal):
    wf, _ = paid(client, paypal)
    pid = wf["passport_id"]
    v = client.get(f"/api/v1/passports/{pid}/verify", headers=A).json()
    assert v["valid"] and v["passport_signature_valid"] and v["approval_signature_valid"]
    with SessionLocal() as db:
        row = db.get(PurchasePassportRow, pid)
        body = dict(row.body)
        body["payment"] = {**body["payment"], "captured_amount": "1.00"}
        row.body = body
        db.commit()
    t = client.get(f"/api/v1/passports/{pid}/verify", headers=A).json()
    assert t["valid"] is False and t["passport_signature_valid"] is False and t["passport_hash_matches"] is False


# ================================================================ misc infra ===
def test_image_proxy(client, real_stores, monkeypatch):
    import app.api.account_routes as ar

    real_client = httpx.Client
    real_stores.search_products("wireless headphones", None, currency="USD")
    sony = next(l for l in real_stores.search_products("wireless headphones", None, currency="USD")
                if l["listing_seller_name"] == "Best Buy")
    img = b"\x89PNG\r\n\x1a\nimg"
    monkeypatch.setattr(ar.httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=img, headers={"content-type": "image/png"}))))
    r = client.get(f"/api/v1/images/{sony['id']}", headers=A)
    assert r.status_code == 200 and r.content == img
    assert client.get("/api/v1/images/ext-doesnotexist", headers=A).status_code == 404


def test_live_updates_stream(client, paypal):
    wf, _ = paid(client, paypal)
    seen, done = 0, False
    with client.stream("GET", f"/api/v1/workflows/{wf['id']}/stream", headers=A) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("event: workflow"):
                seen += 1
            if line.startswith("event: done"):
                done = True
                break
    assert done and seen > 10


def test_tracing_headers(client):
    tid = "4bf92f3577b34da6a3ce929d0e0e4736"
    r = client.get("/health", headers={"traceparent": f"00-{tid}-00f067aa0ba902b7-01"})
    assert r.headers["traceparent"].startswith(f"00-{tid}-") and r.headers["x-request-id"] == tid


def test_migrations_recorded():
    with engine.connect() as conn:
        ids = {r[0] for r in conn.execute(text("SELECT id FROM schema_migrations"))}
    assert {"0001_indexes", "0002_approval_source_backfill"} <= ids


def test_redaction_patterns():
    import logging

    from app.security.auth import RedactSecretsFilter

    rec = logging.LogRecord("x", logging.INFO, __file__, 1,
                            "GET /search?api_key=abc123&q=x contract=ctr_1 password: hunter2", (), None)
    RedactSecretsFilter().filter(rec)
    msg = rec.getMessage()
    assert "abc123" not in msg and "hunter2" not in msg and "contract=ctr_1" in msg
