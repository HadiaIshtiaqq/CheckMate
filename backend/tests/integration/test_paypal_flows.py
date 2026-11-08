import json
import uuid

from tests.conftest import A, create_order, get_wf, paid, ready_to_pay


def webhook(client, paypal, event: dict, sign=True):
    raw = json.dumps(event).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["X-CheckMate-Mock-Signature"] = paypal.sign_webhook(raw)
    return client.post("/api/v1/webhooks/paypal", content=raw, headers=headers)


def capture_event(order_id: str, capture_id="CAP1", status="COMPLETED") -> dict:
    return {"id": f"WH-{uuid.uuid4().hex}", "event_type": f"PAYMENT.CAPTURE.{status}",
            "resource": {"id": capture_id, "status": status,
                         "supplementary_data": {"related_ids": {"order_id": order_id}}}}


def test_create_order_and_capture(client, paypal):
    wf, pay = paid(client, paypal)
    assert pay["payment_status"] == "CAPTURED" and pay["paypal_capture_id"]
    assert pay["captured_amount"] == "105.00" and pay["captured_currency"] == "USD"
    assert wf["state"] == "COMPLETED" and wf["passport_id"]


def test_order_breakdown_matches_verified_checkout(client, paypal):
    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    po = paypal.get_order(order["paypal_order_id"])
    unit = po["purchase_units"][0]
    assert unit["amount"]["value"] == "105.00"
    b = unit["amount"]["breakdown"]
    assert (b["item_total"]["value"], b["shipping"]["value"], b["tax_total"]["value"]) == ("90.00", "10.00", "5.00")
    assert unit["custom_id"] == f"{wf['contract']['id']}:v1"


def test_capture_before_buyer_approval_rejected(client, paypal):
    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    r = client.post(f"/api/v1/payments/{order['id']}/capture", headers=A)
    assert r.status_code == 409
    assert client.get(f"/api/v1/payments/{order['id']}", headers=A).json()["payment_status"] == "APPROVAL_PENDING"


def test_cancel_before_capture_then_new_order(client, paypal):
    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    paypal.buyer_decision(order["paypal_order_id"], False)
    r = client.post(f"/api/v1/payments/{order['id']}/cancel", headers=A)
    assert r.json()["payment_status"] == "CANCELLED"
    assert client.post(f"/api/v1/payments/{order['id']}/capture", headers=A).status_code == 409
    second = create_order(client, wf["contract"]["id"]).json()
    assert second["id"] != order["id"] and second["idempotency_key"].endswith("-a2")


def test_rejected_payment(client, paypal):
    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    paypal.buyer_decision(order["paypal_order_id"], True)
    paypal.decline_next_capture = True
    r = client.post(f"/api/v1/payments/{order['id']}/capture", headers=A)
    assert r.json()["payment_status"] == "FAILED"
    assert get_wf(client, wf["id"])["state"] == "PAYMENT_FAILED"


def test_create_order_is_idempotent(client, paypal):
    wf, _ = ready_to_pay(client)
    a = create_order(client, wf["contract"]["id"]).json()
    b = create_order(client, wf["contract"]["id"]).json()
    assert a["id"] == b["id"] and a["paypal_order_id"] == b["paypal_order_id"]


def test_timeout_never_assumed_failure_and_no_duplicate_capture(client, paypal):
    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    paypal.buyer_decision(order["paypal_order_id"], True)
    paypal.fail_next_capture_with_timeout = True  # PayPal captures, but the response is lost
    r = client.post(f"/api/v1/payments/{order['id']}/capture", headers=A).json()
    # the agent re-queried PayPal and found the completed capture
    assert r["payment_status"] == "CAPTURED"
    again = client.post(f"/api/v1/payments/{order['id']}/capture", headers=A).json()
    assert again["paypal_capture_id"] == r["paypal_capture_id"]
    po = paypal.get_order(order["paypal_order_id"])
    assert len(po["purchase_units"][0]["payments"]["captures"]) == 1


def test_same_idempotency_key_returns_same_capture(paypal, client):
    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    paypal.buyer_decision(order["paypal_order_id"], True)
    first = paypal.capture_order(order["paypal_order_id"], "req-1")
    second = paypal.capture_order(order["paypal_order_id"], "req-1")
    assert first == second


def test_valid_webhook_processed_and_deduplicated(client, paypal):
    wf, pay = paid(client, paypal)
    ev = capture_event(pay["paypal_order_id"], pay["paypal_capture_id"])
    r1 = webhook(client, paypal, ev)
    assert r1.status_code == 200 and r1.json()["status"] == "processed"
    r2 = webhook(client, paypal, ev)
    assert r2.json()["status"] == "duplicate"


def test_invalid_webhook_signature_rejected(client, paypal):
    ev = capture_event("MOCKX")
    assert webhook(client, paypal, ev, sign=False).status_code == 400
    raw = json.dumps(ev).encode()
    r = client.post("/api/v1/webhooks/paypal", content=raw,
                    headers={"X-CheckMate-Mock-Signature": "0" * 64, "Content-Type": "application/json"})
    assert r.status_code == 400


def test_delayed_webhook_reconciles_uncertain_payment(client, paypal, monkeypatch):
    """Capture response AND the follow-up query are lost; the later webhook settles it."""
    from app.integrations.paypal.client import PayPalTimeout

    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    paypal.buyer_decision(order["paypal_order_id"], True)
    paypal.fail_next_capture_with_timeout = True
    real_get = paypal.get_order
    calls = {"n": 0}

    def flaky_get(order_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PayPalTimeout("lost")
        return real_get(order_id)

    from app.services.container import get_container

    reg = get_container().registry._tools
    monkeypatch.setitem(reg, "paypal.get_order", flaky_get)
    r = client.post(f"/api/v1/payments/{order['id']}/capture", headers=A).json()
    assert r["payment_status"] == "UNCERTAIN"
    assert get_wf(client, wf["id"])["state"] == "PAYMENT_PENDING_APPROVAL"
    res = webhook(client, paypal, capture_event(order["paypal_order_id"]))
    assert "capture recorded" in res.json()["result"]
    final = get_wf(client, wf["id"])
    assert final["state"] == "COMPLETED" and final["payments"][0]["payment_status"] == "CAPTURED"


def test_forged_webhook_cannot_change_payment_status(client, paypal):
    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    r = webhook(client, paypal, capture_event(order["paypal_order_id"]), sign=False)
    assert r.status_code == 400
    assert client.get(f"/api/v1/payments/{order['id']}", headers=A).json()["payment_status"] == "APPROVAL_PENDING"


def test_signed_webhook_cannot_fake_capture_that_paypal_never_made(client, paypal):
    """Even a correctly signed event is confirmed against PayPal's order state."""
    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    res = webhook(client, paypal, capture_event(order["paypal_order_id"])).json()
    assert "not completed" in res["result"]
    assert client.get(f"/api/v1/payments/{order['id']}", headers=A).json()["payment_status"] == "APPROVAL_PENDING"
