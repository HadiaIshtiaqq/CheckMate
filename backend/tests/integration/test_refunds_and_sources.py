"""PayPal refunds and per-request product sources."""
from tests.conftest import A, create_order, get_wf, paid, ready_to_pay


def test_refund_captured_payment_through_paypal(client, paypal):
    wf, pay = paid(client, paypal, "receipt_discrepancy")
    r = client.post(f"/api/v1/payments/{pay['id']}/refund", json={"reason": "Receipt total doesn't match"}, headers=A)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["refund_status"] == "COMPLETED" and body["refund_id"].startswith("MOCKREF")
    assert body["refunded_amount"] == pay["captured_amount"]
    events = client.get(f"/api/v1/workflows/{wf['id']}/events", headers=A).json()
    assert events["chain_verified"] and any(e["type"] == "payment.refunded" for e in events["events"])
    # A refund is appended after the passport was sealed; the passport still verifies.
    pp = get_wf(client, wf["id"])["passport_id"]
    assert client.get(f"/api/v1/passports/{pp}/verify", headers=A).json()["valid"]
    # Refunding twice is refused.
    assert client.post(f"/api/v1/payments/{pay['id']}/refund", json={}, headers=A).status_code == 409


def test_cannot_refund_uncaptured_payment(client, paypal):
    wf, _ = ready_to_pay(client)
    order = create_order(client, wf["contract"]["id"]).json()
    assert client.post(f"/api/v1/payments/{order['id']}/refund", json={}, headers=A).status_code == 409


def test_other_users_cannot_refund(client, paypal):
    _, pay = paid(client, paypal)
    r = client.post(f"/api/v1/payments/{pay['id']}/refund", json={}, headers={"Authorization": "Bearer tok-bob"})
    assert r.status_code == 404


def test_source_partner_only_searches_partner_merchant(client):
    r = client.post("/api/v1/shopping/intents", headers=A, json={
        "text": "Find wireless headphones under $110 with free returns", "source": "partner"})
    assert r.status_code == 202
    assert client.post("/api/v1/shopping/intents", headers=A, json={"text": "headphones", "source": "bogus"}).status_code == 400
    # Real-store search isn't connected in tests.
    assert client.post("/api/v1/shopping/intents", headers=A, json={"text": "headphones", "source": "stores"}).status_code == 400
