import pytest

from app.agents.payment_agent import PaymentNotAuthorized
from app.policies.engine import PaymentAuthorization
from app.services.container import get_container

from tests.conftest import A, B, approve, create_order, get_wf, ready_to_pay, start, verify


def test_unauthenticated_cannot_create_payment(client):
    assert client.post("/api/v1/payments/orders", json={"contract_id": "x"}).status_code == 401
    assert client.post("/api/v1/shopping/intents", json={"text": "hello there"}).status_code == 401
    assert client.get("/api/v1/workflows", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_user_b_cannot_access_user_a_resources(client):
    wf = start(client)
    cid = wf["contract"]["id"]
    assert client.get(f"/api/v1/workflows/{wf['id']}", headers=B).status_code == 404
    assert client.get(f"/api/v1/contracts/{cid}", headers=B).status_code == 404
    assert client.post(f"/api/v1/contracts/{cid}/approve", json={"contract_hash": wf["contract"]["contract_hash"]},
                       headers=B).status_code == 404
    assert client.post("/api/v1/payments/orders", json={"contract_id": cid}, headers=B).status_code == 404
    assert all(w["id"] != wf["id"] for w in client.get("/api/v1/workflows", headers=B).json())


def test_prompt_injection_cannot_modify_contract(client):
    wf = start(client)
    echo = next(e for e in wf["evaluations"] if e["candidate"]["id"] == "hp-echobeat")
    assert echo["terms"]["injection_flags"]
    assert echo["status"] == "FAIL"
    assert wf["contract"]["terms"]["max_total"] == "110.00"
    assert wf["contract"]["terms"]["product_id"] != "hp-echobeat"
    assert wf["contract"]["approved"] is False


def test_payment_agent_cannot_capture_without_policy_authorization():
    agent = get_container().payment()
    forged = PaymentAuthorization(allowed=True, reasons=[], contract_id="c", contract_version=1,
                                  contract_hash="h", verification_id="v", amount="105.00", currency="USD",
                                  signature="f" * 64)
    with pytest.raises(PaymentNotAuthorized):
        agent.capture(forged, order_id="X", contract_hash="h", amount="105.00", request_id="r")
    with pytest.raises(PaymentNotAuthorized):
        agent.create_order(None, {"purchase_units": [{"amount": {"value": "1.00"}}]}, "r")


def test_payment_agent_cannot_change_budget_or_contract():
    agent = get_container().payment()
    assert not any(t.startswith(("merchant.", "catalog.", "llm.")) for t in agent.allowed_tools)
    assert not hasattr(agent, "revise_contract") and not hasattr(agent, "approve")


def test_failed_checkout_cannot_be_paid(client):
    wf = start(client)
    approve(client, wf)
    assert verify(client, wf["contract"]["id"], "subscription_addon").json()["status"] == "FAIL"
    r = create_order(client, wf["contract"]["id"])
    assert r.status_code == 403
    assert any("PASS" in reason for reason in r.json()["detail"]["reasons"])


def test_unknown_checkout_cannot_be_paid(client):
    wf = start(client)
    approve(client, wf)
    assert verify(client, wf["contract"]["id"], "hidden_fee").json()["status"] == "UNKNOWN"
    assert create_order(client, wf["contract"]["id"]).status_code == 403


def test_revoked_approval_cannot_authorize_payment(client, paypal):
    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    paypal.buyer_decision(order["paypal_order_id"], True)
    client.post(f"/api/v1/contracts/{wf['contract']['id']}/revoke", headers=A)
    r = client.post(f"/api/v1/payments/{order['id']}/capture", headers=A)
    assert r.status_code in (403, 409)
    assert client.get(f"/api/v1/payments/{order['id']}", headers=A).json()["payment_status"] == "CANCELLED"


def test_expired_contract_cannot_authorize(client):
    from datetime import timedelta

    from app.core.util import utcnow
    from app.database.models import PurchaseContract
    from app.database.session import SessionLocal

    wf = start(client)
    approve(client, wf)
    with SessionLocal() as db:
        row = db.get(PurchaseContract, wf["contract"]["id"])
        row.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
    assert verify(client, wf["contract"]["id"]).status_code == 409
    assert get_wf(client, wf["id"])["state"] == "EXPIRED"
    assert create_order(client, wf["contract"]["id"]).status_code in (403, 409)


def test_monitoring_only_mode_cannot_purchase(client):
    wf = start(client, mode="MONITORING_ONLY")
    approve(client, wf)
    assert verify(client, wf["contract"]["id"]).status_code == 409


def test_duplicate_capture_requests_capture_once(client, paypal):
    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    paypal.buyer_decision(order["paypal_order_id"], True)
    r1 = client.post(f"/api/v1/payments/{order['id']}/capture", headers=A).json()
    r2 = client.post(f"/api/v1/payments/{order['id']}/capture", headers=A).json()
    assert r1["paypal_capture_id"] == r2["paypal_capture_id"]
    po = paypal.get_order(order["paypal_order_id"])
    assert len(po["purchase_units"][0]["payments"]["captures"]) == 1


def test_stale_verification_cannot_be_captured(client, paypal):
    """Re-verifying (e.g. merchant changed checkout) supersedes the old order."""
    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    paypal.buyer_decision(order["paypal_order_id"], True)
    verify(client, wf["contract"]["id"], "subscription_addon")
    assert client.post(f"/api/v1/payments/{order['id']}/capture", headers=A).status_code == 403


def test_secrets_not_exposed(client, caplog):
    s = get_container().settings
    body = client.get("/api/v1/system").text + client.get("/api/v1/workflows", headers=A).text
    for secret in (s.authorization_secret, s.mock_webhook_secret, "tok-alice"):
        assert secret not in body
    import logging

    from app.security.auth import RedactSecretsFilter

    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "auth Bearer tok-alice secret %s", (s.authorization_secret,),
                            None)
    RedactSecretsFilter().filter(rec)
    assert "tok-alice" not in rec.getMessage() and s.authorization_secret not in rec.getMessage()
