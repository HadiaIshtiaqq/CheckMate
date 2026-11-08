"""The three hackathon scenarios (Testing Document 5.7)."""
from tests.conftest import A, approve, create_order, get_wf, paid, start, verify


def test_scenario_a_successful_purchase(client, paypal):
    wf, pay = paid(client, paypal, "clean")
    assert wf["state"] == "COMPLETED" and not wf["has_discrepancy"]
    assert pay["captured_amount"] == "105.00"
    pp = client.get(f"/api/v1/passports/{wf['passport_id']}", headers=A).json()
    body = pp["body"]
    assert body["checkout_verification"]["result"] == "PASS"
    assert body["payment"]["paypal_order_id"] and body["payment"]["paypal_capture_id"]
    assert body["reconciliation"]["status"] == "RECONCILED"
    assert body["contract"]["contract_hash"] == wf["contract"]["contract_hash"]
    assert body["audit_chain"]["verified"] is True
    for key in ("original_intent", "selected_product", "seller_evidence", "purchase_terms", "cost_breakdown",
                "receipt_evidence", "discrepancies", "event_timeline"):
        assert key in body
    exp = client.get(f"/api/v1/passports/{wf['passport_id']}/export", headers=A)
    assert exp.status_code == 200 and "attachment" in exp.headers["content-disposition"]


def test_scenario_b_manipulated_checkout_blocked(client):
    wf = start(client)
    approve(client, wf)
    v = verify(client, wf["contract"]["id"], "subscription_addon").json()
    assert v["status"] == "FAIL"
    failed = {c["key"] for c in v["conditions"] if c["result"] == "FAIL"}
    assert {"subscription", "budget"} <= failed
    assert v["checkout_snapshot"]["stated_total"] == "120.00"
    assert "PAYMENT BLOCKED" in v["explanation"]
    wf2 = get_wf(client, wf["id"])
    assert wf2["state"] == "CHECKOUT_BLOCKED"
    assert create_order(client, wf["contract"]["id"]).status_code == 403
    # alternative search was initiated and found the next eligible offer
    assert wf2["alternatives"] and wf2["alternatives"][0]["product_id"] == "hp-nova-2"
    r = client.post(f"/api/v1/workflows/{wf['id']}/alternatives/hp-nova-2/select", headers=A)
    assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["approved"] is False


def test_scenario_c_post_payment_discrepancy(client, paypal):
    wf, pay = paid(client, paypal, "receipt_discrepancy")
    assert pay["captured_amount"] == "105.00"  # PayPal record stays $105
    assert wf["has_discrepancy"] is True and wf["state"] == "COMPLETED"
    body = client.get(f"/api/v1/passports/{wf['passport_id']}", headers=A).json()["body"]
    assert body["payment"]["captured_amount"] == "105.00"
    assert body["receipt_evidence"]["total"] == "115.00"
    assert "SIMULATED" in body["receipt_evidence"]["source_label"]
    d = body["discrepancies"][0]
    assert d["code"] == "RECEIPT_TOTAL_MISMATCH"
    assert "PayPal record remains the authoritative" in d["description"]
    assert "PayPal captured 115" not in d["description"]
    states = [e["state"] for e in body["event_timeline"]]
    assert "RECONCILIATION_REQUIRED" in states
