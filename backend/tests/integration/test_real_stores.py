"""Real-store discovery through Google Shopping (SerpAPI), faked at the HTTP layer.
Nothing here is invented by CHECKMATE: every offer comes from the (fake) API response."""
import httpx
import pytest

from app.database.session import SessionLocal
from app.integrations.merchants.router import CatalogRouter
from app.integrations.merchants.serpapi_shopping import SerpApiShoppingAdapter, parse_shipping, specs_from_text
from app.services.container import get_container

from tests.conftest import A, approve, create_order, start, verify

HEADPHONES_RESULTS = {"shopping_results": [
    {"position": 1, "title": "Sony WH-CH720N Wireless Noise Canceling Headphones, Black", "product_id": "111",
     "product_link": "https://www.google.com/shopping/product/111", "source": "Best Buy", "price": "$89.99",
     "extracted_price": 89.99, "rating": 4.5, "reviews": 1200, "delivery": "Free delivery",
     "extensions": ["Free 30-day returns"], "thumbnail": "https://img.example/111.jpg"},
    {"position": 2, "title": "JBL Tune 760NC Wireless Over-Ear Headphones", "product_id": "222",
     "product_link": "https://www.google.com/shopping/product/222", "source": "Target", "price": "$79.99",
     "extracted_price": 79.99, "rating": 4.3, "reviews": 800, "delivery": "$5.99 delivery"},
    {"position": 3, "title": "Wireless Headphones Music Bundle", "product_id": "333",
     "product_link": "https://www.google.com/shopping/product/333", "source": "Gadget Hub", "price": "$69.00",
     "extracted_price": 69.0, "snippet": "Subscription required: $9.99/month streaming plan."},
    {"position": 4, "title": "Bose QuietComfort Ultra Wireless Headphones", "product_id": "444",
     "product_link": "https://www.google.com/shopping/product/444", "source": "Amazon.com", "price": "$329.00",
     "extracted_price": 329.0},
    {"position": 5, "title": "Generic Wireless Headphones", "product_id": "555",
     "product_link": "https://www.google.com/shopping/product/555", "source": "eBay - cheapdeals",
     "price": "$19.99", "extracted_price": 19.99, "delivery": "$4.99 delivery"},
    {"position": 6, "title": "Listing with no price", "source": "Somewhere"},
]}
CHAIR_RESULTS = {"shopping_results": [
    {"position": 1, "title": "HON Ergonomic Office Chair", "product_id": "c1", "source": "Staples",
     "product_link": "https://www.google.com/shopping/product/c1", "price": "$99.99", "extracted_price": 99.99,
     "delivery": "Free delivery", "extensions": ["Free 30-day returns"]},
]}
HEADPHONES = ("Find wireless headphones under $110. They must have at least a one-year warranty, free returns, "
              "no subscription, and come from a reliable seller.")


def handler(request: httpx.Request) -> httpx.Response:
    q = request.url.params.get("q", "").lower()
    if request.url.params.get("engine") != "google_shopping":  # Amazon/eBay/Walmart: nothing in these fixtures
        return httpx.Response(200, json={"organic_results": []})
    if "explode" in q:
        return httpx.Response(401, json={"error": "Invalid API key. Your API key should be here: test-serp-key"})
    if "zzzz" in q:
        return httpx.Response(200, json={"error": "Google hasn't returned any results for this query."})
    if "chair" in q:
        return httpx.Response(200, json=CHAIR_RESULTS)
    return httpx.Response(200, json=HEADPHONES_RESULTS)


def install(monkeypatch, router):
    c = get_container()
    for name, fn in {"catalog.search": router.search, "catalog.product_details": router.product_details,
                     "merchant.seller_info": router.seller_info, "merchant.terms": router.terms,
                     "merchant.quote_tax": router.quote_tax, "merchant.create_checkout": router.create_checkout,
                     "merchant.receipt": router.receipt}.items():
        monkeypatch.setitem(c.registry._tools, name, fn)
    monkeypatch.setattr(c, "catalog", router)


@pytest.fixture
def real_stores(monkeypatch):
    adapter = SerpApiShoppingAdapter("test-serp-key", SessionLocal, transport=httpx.MockTransport(handler))
    install(monkeypatch, CatalogRouter(None, adapter))
    return adapter


def report(client, wf, amount, confirmations):
    return client.post(f"/api/v1/contracts/{wf['contract']['id']}/report-purchase",
                       json={"amount": amount, "currency": "USD", "confirmations": confirmations,
                             "order_reference": "BBY01-123"}, headers=A)


def test_listing_parsing(real_stores):
    listings = real_stores.search_products("wireless headphones", None, currency="USD")
    assert len(listings) == 5  # the listing without a price is skipped, never guessed
    sony = listings[0]
    assert sony["id"].startswith("ext-") and sony["currency"] == "USD" and sony["price"] == "89.99"
    assert sony["listing_seller_name"] == "Best Buy" and sony["external"] is True
    assert sony["terms"] == {"shipping_cost": "0.00"}
    assert listings[1]["terms"] == {"shipping_cost": "5.99"}
    assert real_stores.get_seller_information("store:Best Buy")["established_retailer"] is True
    assert real_stores.get_seller_information("store:eBay - cheapdeals")["marketplace_seller"] is True
    real_stores.search_products("wireless headphones", None, currency="USD")
    assert real_stores.searches_made == 1  # cached: no second paid search


def test_helpers():
    assert parse_shipping("Free delivery by Fri") == "0.00"
    assert parse_shipping("$4.99 delivery") == "4.99"
    assert parse_shipping("Delivery in 3 days") is None
    assert specs_from_text("Laptop 16GB RAM 512GB SSD")["storage_gb"] == "512"


def test_real_offers_produce_a_store_purchase_plan(client, real_stores):
    wf = start(client, HEADPHONES)
    assert wf["state"] == "CONTRACT_CREATED", wf["blocked_reason"]
    t = wf["contract"]["terms"]
    assert t["purchase_channel"] == "external_store" and t["store_name"] == "Best Buy"
    assert t["product_name"].startswith("Sony WH-CH720N")
    keys = {i["key"] for i in t["checklist"]}
    assert {"warranty", "subscription", "budget"} <= keys and "returns" not in keys  # returns shown on listing
    status = {e["candidate"]["listing_seller_name"]: e["status"] for e in wf["evaluations"]}
    assert status["Amazon.com"] == "FAIL"          # $329 alone exceeds $110
    assert status["Gadget Hub"] == "FAIL"          # subscription required
    assert status["eBay - cheapdeals"] == "FAIL"   # marketplace seller, reliable seller requested
    assert all(e["candidate"]["external"] and not e["candidate"]["demo"] for e in wf["evaluations"])


def test_real_offers_cannot_use_demo_checkout_or_paypal(client, real_stores):
    wf = start(client, HEADPHONES)
    approve(client, wf)
    assert verify(client, wf["contract"]["id"]).status_code == 409
    assert create_order(client, wf["contract"]["id"]).status_code == 403


def test_report_purchase_reconciles_and_issues_passport(client, real_stores):
    wf = start(client, HEADPHONES)
    assert report(client, wf, "97.50", {"warranty": True, "subscription": True}).status_code == 409  # not approved
    approve(client, wf)
    r = report(client, wf, "97.50", {"warranty": True, "subscription": True})
    assert r.status_code == 200 and r.json()["state"] == "COMPLETED" and not r.json()["has_discrepancy"]
    body = client.get(f"/api/v1/passports/{r.json()['passport_id']}", headers=A).json()["body"]
    assert body["purchase_channel"] == "external_store"
    assert body["reconciliation"]["status"] == "RECONCILED"
    assert body["payment"]["captured_amount"] == "97.50" and body["payment"]["paypal_order_id"] is None
    assert "Self-reported" in body["payment"]["source"]
    assert body["checkout_verification"]["result"] == "CONFIRMED_BY_YOU"


@pytest.mark.parametrize("amount,conf,code", [
    ("125.00", {"warranty": True, "subscription": True}, "OVER_APPROVED_MAXIMUM"),
    ("97.50", {"warranty": False, "subscription": True}, "NOT_MET_WARRANTY"),
])
def test_report_purchase_flags_discrepancies(client, real_stores, amount, conf, code):
    wf = start(client, HEADPHONES)
    approve(client, wf)
    r = report(client, wf, amount, conf).json()
    assert r["has_discrepancy"] is True and r["state"] == "COMPLETED"
    body = client.get(f"/api/v1/passports/{r['passport_id']}", headers=A).json()["body"]
    assert code in {d["code"] for d in body["discrepancies"]}


def test_unconfirmed_items_leave_reconciliation_incomplete(client, real_stores):
    wf = start(client, HEADPHONES)
    approve(client, wf)
    r = report(client, wf, "97.50", {"warranty": True}).json()
    body = client.get(f"/api/v1/passports/{r['passport_id']}", headers=A).json()["body"]
    assert body["reconciliation"]["status"] == "INCOMPLETE"


def test_choose_another_real_offer(client, real_stores):
    wf = start(client, HEADPHONES)
    jbl = next(e for e in wf["evaluations"] if e["candidate"]["listing_seller_name"] == "Target")
    r = client.post(f"/api/v1/workflows/{wf['id']}/alternatives/{jbl['candidate']['id']}/select", headers=A)
    assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["terms"]["store_name"] == "Target"
    amazon = next(e for e in wf["evaluations"] if e["candidate"]["listing_seller_name"] == "Amazon.com")
    assert client.post(f"/api/v1/workflows/{wf['id']}/alternatives/{amazon['candidate']['id']}/select",
                       headers=A).status_code == 409


def test_any_product_uses_real_listings(client, real_stores):
    wf = start(client, "Find an office chair under $150 with free returns from a reliable seller.")
    assert wf["state"] == "CONTRACT_CREATED"
    assert wf["contract"]["terms"]["store_name"] == "Staples"
    assert len(wf["evaluations"]) == 1  # exactly what the store search returned, nothing added


def test_no_results_is_reported_honestly(client, real_stores):
    wf = start(client, "Find zzzz under $50")
    assert wf["state"] == "EVIDENCE_INCOMPLETE" and wf["contract"] is None
    assert "No listings were found" in wf["blocked_reason"]


def test_source_error_shown_without_leaking_key(client, real_stores):
    wf = start(client, "Find explode gadget under $50")
    assert wf["state"] == "EVIDENCE_INCOMPLETE"
    assert "Invalid API key" in wf["blocked_reason"] and "test-serp-key" not in wf["blocked_reason"]


def test_no_source_configured(client, monkeypatch):
    install(monkeypatch, CatalogRouter(None, None))
    wf = start(client, HEADPHONES)
    assert wf["state"] == "EVIDENCE_INCOMPLETE" and "SERPAPI_KEY" in wf["blocked_reason"]
    assert wf["evaluations"] == []
