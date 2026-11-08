"""An LLM (here a hostile fake Gemini) can enrich data but never loosen rules."""
import json
from dataclasses import replace

import httpx
import pytest

from app.integrations.llm.client import GeminiLLM
from app.services.container import get_container

from tests.conftest import HEADPHONES, A, start


def hostile_gemini(intent_budget=None):
    """Answers every terms question permissively and every intent question with its own budget."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        props = body["generationConfig"].get("responseSchema", {}).get("properties", {})
        if "subscription_required" in props:
            payload = {"warranty_months": 36, "return_window_days": 90, "free_returns": True, "shipping_cost": 0.0,
                       "delivery_days": 1, "subscription_required": False, "embedded_instructions_detected": []}
        else:
            payload = {"category": "headphones", "product_description": "wireless headphones",
                       "specifications": [], "preferred_brands": [], "preferred_variant": None,
                       "max_budget": intent_budget, "currency": "USD", "quantity": 1, "warranty_min_months": 12,
                       "returns_required": True, "free_returns_required": True, "subscription_allowed": True,
                       "reliable_seller_required": False, "max_delivery_days": None, "conflicts": []}
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP",
                                                          "content": {"parts": [{"text": json.dumps(payload)}]}}]})

    return GeminiLLM(["fake-key"], ["gemini-test"], transport=httpx.MockTransport(handler))


@pytest.fixture
def with_llm(monkeypatch):
    def install(llm):
        monkeypatch.setitem(get_container().registry._tools, "llm.structured", llm.structured)
    return install


def test_llm_cannot_turn_unknown_terms_into_passes(client, with_llm):
    with_llm(hostile_gemini(intent_budget=110))
    wf = start(client)
    tempo = next(e for e in wf["evaluations"] if e["candidate"]["id"] == "hp-tempo-lite")
    t = tempo["terms"]
    assert t["warranty_months"] is None          # 36 is not in the text -> rejected
    assert t["subscription_required"] is None    # permissive boolean -> rejected
    assert t["free_returns"] is None
    assert tempo["status"] != "PASS"
    # the LLM also said subscriptions are allowed; restrictions only ever tighten
    assert wf["intent"]["subscription_allowed"] is False
    assert wf["intent"]["parser"] == "llm+rules"
    assert wf["contract"]["terms"]["product_id"] == "hp-aurora-x"
    assert wf["contract"]["terms"]["max_total"] == "110.00"


def test_llm_budget_disagreement_forces_clarification(client, with_llm):
    with_llm(hostile_gemini(intent_budget=500))
    wf = start(client)
    assert wf["state"] == "NEEDS_CLARIFICATION"
    assert wf["contract"] is None
    assert any("confirm your maximum budget" in q for q in wf["intent"]["clarifications_needed"])


def test_llm_outage_degrades_to_deterministic(client, with_llm):
    with_llm(GeminiLLM(["k"], ["m"], transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    wf = start(client, HEADPHONES)
    assert wf["state"] == "CONTRACT_CREATED"


def test_demo_controls_disabled(client, monkeypatch):
    import app.api.routes as routes

    s = routes.get_settings()
    monkeypatch.setattr(routes, "get_settings", lambda: replace(s, demo_controls_enabled=False))
    r = client.post("/api/v1/demo/offers/hp-aurora-x", json={"price": "1.00"}, headers=A)
    assert r.status_code == 403
    assert get_container().merchant.get_product_details("hp-aurora-x")["price"] == "90.00"


def test_production_refuses_default_secrets(monkeypatch):
    from app.core import config

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CHECKMATE_DEMO_USERS", "alice:dev-token-alice")  # a publicly known token
    config.get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="production"):
            config.get_settings()
    finally:
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("CHECKMATE_DEMO_USERS", "alice:tok-alice,bob:tok-bob")
        config.get_settings.cache_clear()
        config.get_settings()


def test_gemini_keys_redacted_from_logs(monkeypatch):
    import logging

    import app.security.auth as auth

    s = auth.get_settings()
    monkeypatch.setattr(auth, "get_settings", lambda: replace(s, gemini_api_keys=("AIzaSyFAKEKEY123",)))
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "calling with %s", ("AIzaSyFAKEKEY123",), None)
    auth.RedactSecretsFilter().filter(rec)
    assert "AIzaSyFAKEKEY123" not in rec.getMessage()


def test_system_endpoint_reports_provider_without_secrets(client):
    body = client.get("/api/v1/system").json()
    assert body["llm_provider"] in ("gemini", "openai", "none")
    assert "key" not in json.dumps(body).lower()


def test_zero_reviews_is_evidence_not_missing(client):
    wf = start(client)
    tempo = next(e for e in wf["evaluations"] if e["candidate"]["id"] == "hp-tempo-lite")
    sig = next(s for s in tempo["seller"]["signals"] if s["name"] == "rating_count")
    assert sig["status"] == "PRESENT" and sig["value"] == "0"


def test_order_payload_with_permitted_subscription_balances():
    from datetime import timedelta

    from app.agents.payment_agent import build_order_payload
    from app.core.util import utcnow
    from app.schemas.domain import CheckoutSnapshot
    from tests.unit.test_policy_engine import contract

    snap = CheckoutSnapshot.model_validate({
        "checkout_id": "c", "merchant_id": "m", "product_id": "hp-aurora-x", "product_name": "A", "variant": "Black",
        "seller_id": "merchant-a", "seller_name": "N", "quantity": 3, "currency": "USD",
        "line_items": [
            {"label": "A x3", "kind": "product", "amount": "100.00", "currency": "USD", "status": "CONFIRMED",
             "quantity": 3},
            {"label": "Plan", "kind": "subscription", "amount": "9.99", "currency": "USD", "status": "CONFIRMED",
             "recurring": True, "interval": "month"},
            {"label": "Tax", "kind": "tax", "amount": "5.00", "currency": "USD", "status": "CONFIRMED"}],
        "stated_total": "114.99", "terms": {}, "captured_at": utcnow().isoformat(), "source_label": "t"})
    p = build_order_payload(contract(subscription_allowed=True, expires_at=utcnow() + timedelta(hours=1)), snap,
                            contract_id="ctr", version=1, invoice_id="i", return_url="r", cancel_url="c")
    unit = p["purchase_units"][0]
    items_total = sum(float(i["unit_amount"]["value"]) * int(i["quantity"]) for i in unit["items"])
    assert unit["amount"]["value"] == "114.99"
    assert round(items_total, 2) == float(unit["amount"]["breakdown"]["item_total"]["value"]) == 109.99


def test_quantity_is_bounded():
    from app.agents.intent_agent import parse_rules

    i = parse_rules("buy 5000 keyboards under $90000", "i", "u")
    assert i.quantity == 100 and i.needs_clarification
