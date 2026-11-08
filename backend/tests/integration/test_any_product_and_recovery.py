"""Any product request, monitor->assisted upgrade, payment sweeper, approval-link secret,
rate limiting by identity, and forward migrations."""
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.util import utcnow
from app.database.models import PaymentTransaction
from app.database.session import SessionLocal, engine, ensure_columns
from app.orchestrator import service as svc

from tests.conftest import A, approve, create_order, get_wf, ready_to_pay, start, verify


@pytest.mark.parametrize("text,phrase", [
    ("Find a chair under $110 that comes from a reliable seller.", "chair"),
    ("office chair between $50 and $120 with free returns", "office chair"),
    ("a desk lamp around $40, no subscription", "desk lamp"),
    ("running shoes, 100 bucks, at least a one-year warranty", "running shoes"),
])
def test_any_product_gets_a_contract(client, text, phrase):
    wf = start(client, text)
    assert wf["state"] == "CONTRACT_CREATED", wf["blocked_reason"]
    assert wf["intent"]["product_description"] == phrase
    statuses = {e["candidate"]["id"].rsplit("-", 1)[-1]: e["status"] for e in wf["evaluations"]}
    assert all(e["candidate"]["simulated"] for e in wf["evaluations"])
    assert statuses["2"] == "FAIL"   # hidden subscription is still caught
    assert statuses["3"] != "PASS"   # undisclosed terms are never a pass
    assert wf["contract"]["terms"]["max_total"] == wf["intent"]["max_budget"]


def test_generated_offer_can_be_bought_end_to_end(client, paypal):
    wf = start(client, "Find a chair under $110 that comes from a reliable seller.")
    approve(client, wf)
    assert verify(client, wf["contract"]["id"]).json()["status"] == "PASS"
    order = create_order(client, wf["contract"]["id"]).json()
    paypal.buyer_decision(order["paypal_order_id"], True)
    client.post(f"/api/v1/payments/{order['id']}/capture", headers=A)
    done = get_wf(client, wf["id"])
    assert done["state"] == "COMPLETED" and done["passport_id"]


def test_generated_listing_survives_restart(client):
    from app.integrations.merchants.demo_merchant import generate_listing

    wf = start(client, "Find a chair under $110 that comes from a reliable seller.")
    pid = wf["contract"]["terms"]["product_id"]
    regenerated = generate_listing(pid)
    assert regenerated["price"] == wf["contract"]["terms"]["unit_price"]


def test_monitor_only_can_switch_to_assisted_and_pay(client):
    wf = start(client, mode="MONITORING_ONLY")
    approve(client, wf)
    assert verify(client, wf["contract"]["id"]).status_code == 409
    r = client.post(f"/api/v1/workflows/{wf['id']}/mode", json={"autonomy_mode": "ASSISTED"}, headers=A)
    assert r.status_code == 200 and r.json()["autonomy_mode"] == "ASSISTED"
    assert verify(client, wf["contract"]["id"]).json()["status"] == "PASS"
    back = client.post(f"/api/v1/workflows/{wf['id']}/mode", json={"autonomy_mode": "MONITORING_ONLY"}, headers=A)
    assert back.status_code == 409


def _age_payment(pid: str, status: str):
    with SessionLocal() as db:
        db.execute(text("UPDATE payment_transactions SET payment_status=:s, updated_at=:t WHERE id=:id"),
                   {"s": status, "t": utcnow() - timedelta(minutes=5), "id": pid})
        db.commit()


def test_sweeper_resets_capture_that_never_reached_paypal(client, paypal):
    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    paypal.buyer_decision(order["paypal_order_id"], True)
    _age_payment(order["id"], "CAPTURING")  # simulated crash mid-capture
    assert order["id"] in svc.sweep_payments()
    assert client.get(f"/api/v1/payments/{order['id']}", headers=A).json()["payment_status"] == "APPROVED"
    r = client.post(f"/api/v1/payments/{order['id']}/capture", headers=A).json()
    assert r["payment_status"] == "CAPTURED"


def test_sweeper_settles_capture_that_did_happen(client, paypal):
    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    paypal.buyer_decision(order["paypal_order_id"], True)
    paypal.capture_order(order["paypal_order_id"], f"cm-capture-{order['id']}")  # PayPal captured...
    _age_payment(order["id"], "CAPTURING")                                        # ...but we crashed
    svc.sweep_payments()
    final = get_wf(client, wf["id"])
    assert final["payments"][0]["payment_status"] == "CAPTURED" and final["state"] == "COMPLETED"


def test_mock_approval_page_requires_link_secret(client):
    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    oid = order["paypal_order_id"]
    assert client.get(f"/mock-paypal/checkout?token={oid}").status_code == 403
    assert client.post(f"/mock-paypal/checkout/{oid}/decision", data={"approve": "1", "ct": "guess"}).status_code == 403
    assert client.get(order["approve_url"].replace("http://backend.test", "")).status_code == 200


def test_rate_limit_cannot_be_bypassed_with_fake_tokens():
    from app.security.auth import RateLimitMiddleware

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, per_minute=3)
    app.get("/x")(lambda: {"ok": True})
    c = TestClient(app)
    codes = [c.get("/x", headers={"Authorization": f"Bearer fake-{i}"}).status_code for i in range(5)]
    assert codes.count(429) == 2
    assert c.get("/x", headers=A).status_code == 200  # a real user has their own bucket


def test_missing_columns_are_added_to_existing_tables():
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE webhook_events DROP COLUMN "result"'))
    assert "webhook_events.result" in ensure_columns()
    assert ensure_columns() == []
