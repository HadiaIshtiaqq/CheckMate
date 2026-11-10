"""Amazon / eBay / Walmart through SerpAPI, search depth, and country selection.
Response shapes follow SerpAPI's documented examples for each engine."""
import httpx
import pytest

from app.database.session import SessionLocal
from app.integrations.merchants.router import CatalogRouter
from app.integrations.merchants.serpapi_shopping import SerpApiShoppingAdapter, search_plan

from tests.conftest import A, get_wf
from tests.integration import test_real_stores as rs

GOOGLE = {"shopping_results": [
    {"title": "Keychron K8 Mechanical Keyboard Black", "product_id": "g1", "source": "Best Buy", "price": "$79.99",
     "extracted_price": 79.99, "rating": 4.6, "reviews": 900, "delivery": "Free delivery",
     "extensions": ["Free 30-day returns"], "product_link": "https://www.google.com/shopping/product/g1"},
]}
AMAZON = {"organic_results": [
    {"title": "Redragon K552 Mechanical Gaming Keyboard Black", "asin": "B016MAK38U", "price": "$34.99",
     "extracted_price": 34.99, "rating": 4.5, "reviews": 72000, "link_clean": "https://www.amazon.com/dp/B016MAK38U/",
     "delivery": ["FREE delivery Sat, Jan 17"], "thumbnail": "https://m.media-amazon.com/images/x.jpg"},
    {"title": "Sponsored Keyboard Thing", "asin": "B000SPONS1", "price": "$20.00", "extracted_price": 20.0,
     "sponsored": True, "link": "https://www.amazon.com/sspa/click?x=1"},
]}
EBAY = {"organic_results": [
    {"title": "Ducky One 3 Mechanical Keyboard Black", "product_id": "e1", "link": "https://www.ebay.com/itm/e1",
     "price": {"raw": "$89.00", "extracted": 89.0}, "condition": "Brand New", "shipping": "Free delivery",
     "seller": {"username": "keyboardhub", "reviews": 37100, "positive_feedback_in_percentage": 99.6}},
    {"title": "Logitech G Pro Mechanical Keyboard Black", "product_id": "e2", "link": "https://www.ebay.com/itm/e2",
     "price": {"raw": "$45.00", "extracted": 45.0}, "condition": "Pre-Owned", "shipping": "$6.50 delivery",
     "seller": {"username": "secondhandsam", "reviews": 120, "positive_feedback_in_percentage": 97.1}},
    {"title": "Mechanical Keyboard Variants Black", "product_id": "e3", "link": "https://www.ebay.com/itm/e3",
     "price": {"from": {"raw": "$30.00", "extracted": 30.0}, "to": {"raw": "$60.00", "extracted": 60.0}},
     "condition": "Brand New", "seller": {"username": "newbie", "reviews": 3, "positive_feedback_in_percentage": 100}},
]}
WALMART = {"organic_results": [
    {"title": "onn. Mechanical Keyboard Black", "product_id": "w1", "us_item_id": "111",
     "product_page_url": "https://www.walmart.com/ip/111", "primary_offer": {"offer_price": 29.88},
     "seller_name": "Walmart.com", "rating": 4.2, "reviews": 310, "free_shipping": True},
    {"title": "Out of stock Mechanical Keyboard", "product_id": "w2", "primary_offer": {"offer_price": 19.0},
     "seller_name": "Walmart.com", "out_of_stock": True},
]}
BY_ENGINE = {"google_shopping": GOOGLE, "amazon": AMAZON, "ebay": EBAY, "walmart": WALMART}


class Recorder:
    def __init__(self, fail=()):
        self.calls: list[dict] = []
        self.fail = set(fail)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p = dict(request.url.params)
        self.calls.append(p)
        if p["engine"] in self.fail:
            return httpx.Response(500, json={"error": "Upstream failure"})
        return httpx.Response(200, json=BY_ENGINE[p["engine"]])


@pytest.fixture
def stores(monkeypatch):
    rec = Recorder()
    adapter = SerpApiShoppingAdapter("k", SessionLocal, transport=httpx.MockTransport(rec))
    rs.install(monkeypatch, CatalogRouter(None, adapter))
    return adapter, rec


def start_with(client, text, **opts):
    r = client.post("/api/v1/shopping/intents", json={"text": text, **opts}, headers=A)
    assert r.status_code == 202, r.text
    return get_wf(client, r.json()["workflow_id"])


KB = "Find a black mechanical keyboard under $90 with free returns and no subscription."


def test_search_plans_and_costs():
    assert [e for e, _ in search_plan("us", "quick", "q")] == ["google_shopping"]
    assert [e for e, _ in search_plan("us", "standard", "q")] == ["google_shopping", "amazon", "ebay"]
    assert [e for e, _ in search_plan("us", "wide", "q", "q2")] == ["google_shopping", "amazon", "ebay", "walmart",
                                                                     "google_shopping"]
    assert "walmart" not in [e for e, _ in search_plan("uk", "wide", "q", "q2")]
    assert "ebay" not in [e for e, _ in search_plan("in", "standard", "q")]


def test_wide_search_merges_all_sources(client, stores):
    adapter, rec = stores
    wf = start_with(client, KB, search_depth="wide", market="us")
    engines = [c["engine"] for c in rec.calls]
    assert engines.count("google_shopping") == 2 and {"amazon", "ebay", "walmart"} <= set(engines)
    by_engine = {e["candidate"]["engine"] for e in wf["evaluations"]}
    assert by_engine == {"google_shopping", "amazon", "ebay", "walmart"}
    titles = [e["candidate"]["product_name"] for e in wf["evaluations"]]
    assert "Out of stock Mechanical Keyboard" not in titles  # never offered
    amazon = next(c for c in rec.calls if c["engine"] == "amazon")
    assert amazon["amazon_domain"] == "amazon.com" and amazon["k"].startswith("black mechanical keyboard")


def test_quick_search_uses_one_search(client, stores):
    adapter, rec = stores
    start_with(client, KB, search_depth="quick")
    assert [c["engine"] for c in rec.calls] == ["google_shopping"]


def test_ebay_seller_feedback_is_real_evidence_and_used_items_flagged(client, stores):
    wf = start_with(client, KB + " From a reliable seller.", search_depth="standard")
    ev = {e["candidate"]["product_name"]: e for e in wf["evaluations"]}
    ducky, used, newbie = (ev["Ducky One 3 Mechanical Keyboard Black"], ev["Logitech G Pro Mechanical Keyboard Black"],
                           ev["Mechanical Keyboard Variants Black"])
    assert ducky["seller"]["evidence_level"] == "STRONG_EVIDENCE" and "37,100" in ducky["seller"]["summary"]
    assert newbie["seller"]["evidence_level"] == "INSUFFICIENT_EVIDENCE"
    assert any(c["key"] == "product_type" and "Pre-Owned" in c["reason"] for c in used["conditions"])
    amazon = ev["Redragon K552 Mechanical Gaming Keyboard Black"]
    assert amazon["seller"]["evidence_level"] == "LIMITED_EVIDENCE"  # Amazon marketplace: seller not shown
    assert amazon["candidate"]["listing_seller_name"].startswith("Amazon")


def test_used_items_allowed_when_asked(client, stores):
    wf = start_with(client, "Find a used black mechanical keyboard under $90.", search_depth="standard")
    used = next(e for e in wf["evaluations"] if e["candidate"]["product_name"].startswith("Logitech"))
    assert not any(c["key"] == "product_type" for c in used["conditions"])


def test_country_selection_sets_sites_and_currency(client, stores):
    adapter, rec = stores
    wf = start_with(client, "Find a mechanical keyboard under 80 with free returns.", market="uk",
                    search_depth="standard")
    assert wf["intent"]["currency"] == "GBP" and wf["intent"]["market"] == "uk"
    sent = {c["engine"]: c for c in rec.calls}
    assert sent["google_shopping"]["gl"] == "uk" and sent["amazon"]["amazon_domain"] == "amazon.co.uk"
    assert sent["ebay"]["ebay_domain"] == "ebay.co.uk"


def test_dollar_sign_follows_chosen_country(client, stores):
    wf = start_with(client, "Find a mechanical keyboard under $120.", market="ca", search_depth="quick")
    assert wf["intent"]["currency"] == "CAD"


def test_one_source_failing_does_not_stop_the_others(client, monkeypatch):
    rec = Recorder(fail={"amazon"})
    adapter = SerpApiShoppingAdapter("k", SessionLocal, transport=httpx.MockTransport(rec))
    rs.install(monkeypatch, CatalogRouter(None, adapter))
    wf = start_with(client, KB, search_depth="standard")
    assert wf["state"] in ("CONTRACT_CREATED", "EVIDENCE_INCOMPLETE") and wf["evaluations"]
    assert {e["candidate"]["engine"] for e in wf["evaluations"]} == {"google_shopping", "ebay"}


def test_invalid_options_rejected(client):
    assert client.post("/api/v1/shopping/intents", json={"text": KB, "market": "zz"}, headers=A).status_code == 400
    assert client.post("/api/v1/shopping/intents", json={"text": KB, "search_depth": "deep"},
                       headers=A).status_code == 400


def test_search_quota_is_read_from_account_endpoint_and_cached():
    import httpx
    from app.integrations.merchants.serpapi_shopping import SerpApiShoppingAdapter

    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        assert req.url.path == "/account.json"
        return httpx.Response(200, json={"total_searches_left": 42, "searches_per_month": 250,
                                         "this_month_usage": 208, "plan_name": "Free Plan"})

    a = SerpApiShoppingAdapter("k", None, transport=httpx.MockTransport(handler))
    q = a.quota()
    assert q == {"searches_left": 42, "searches_per_month": 250, "used_this_month": 208, "plan": "Free Plan"}
    assert a.quota() == q and len(calls) == 1  # cached; never spends a search


def test_search_quota_endpoint_requires_sign_in(client):
    assert client.get("/api/v1/system/search-quota").status_code == 401


def _counting_adapter(left=100, **kw):
    import httpx
    from app.integrations.merchants.serpapi_shopping import SerpApiShoppingAdapter

    searches = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/account.json":
            return httpx.Response(200, json={"total_searches_left": left})
        searches.append(req.url.params.get("q"))
        return httpx.Response(200, json={"shopping_results": []})

    return SerpApiShoppingAdapter("k", SessionLocal, transport=httpx.MockTransport(handler), **kw), searches


def test_daily_search_cap_blocks_new_searches_but_serves_cache():
    from app.integrations.merchants.serpapi_shopping import ProductSourceError

    a, searches = _counting_adapter(daily_limit=2)
    a.search_products("kettle", None, currency="USD", depth="quick")
    a.search_products("toaster", None, currency="USD", depth="quick")
    with pytest.raises(ProductSourceError, match="allowance"):
        a.search_products("blender", None, currency="USD", depth="quick")
    a.search_products("kettle", None, currency="USD", depth="quick")  # cached: free and allowed
    assert len(searches) == 2


def test_reserve_protects_monthly_quota():
    from app.integrations.merchants.serpapi_shopping import ProductSourceError

    a, searches = _counting_adapter(left=21, reserve=20)
    a.search_products("kettle", None, currency="USD", depth="quick")  # 21 -> 20
    with pytest.raises(ProductSourceError, match="paused"):
        a.search_products("toaster", None, currency="USD", depth="quick")
    assert len(searches) == 1


def test_depth_cap_and_per_user_daily_limit(client, monkeypatch):
    import dataclasses
    import app.orchestrator.service as svc
    from app.core.config import get_settings

    capped = dataclasses.replace(get_settings(), max_search_depth="quick", requests_per_user_per_day=1)
    monkeypatch.setattr(svc, "get_settings", lambda: capped)
    from app.database.models import User
    from app.core.util import new_id

    uid = new_id("u")
    with SessionLocal() as db:
        db.add(User(id=uid, display_name="cap"))
        db.commit()
        wf = svc.create_workflow(db, uid, "a kettle under $40", "ASSISTED", search_depth="wide")
        assert wf.search_depth == "quick"
        with pytest.raises(svc.RateLimited):
            svc.create_workflow(db, uid, "a toaster under $40", "ASSISTED")


def test_shopper_picks_which_stores_to_search(client, stores):
    adapter, rec = stores
    wf = start_with(client, KB, search_depth="quick", market="us", stores=["amazon", "walmart"])
    assert sorted(c["engine"] for c in rec.calls) == ["amazon", "walmart"]  # no Google Shopping, one search each
    assert {e["candidate"]["engine"] for e in wf["evaluations"]} == {"amazon", "walmart"}


def test_store_choice_skips_stores_missing_in_the_country():
    assert [e for e, _ in search_plan("uk", "quick", "q", stores=["walmart", "ebay"])] == ["ebay"]
    assert [e for e, _ in search_plan("in", "quick", "q", stores=["walmart"])] == ["google_shopping"]
    assert [e for e, _ in search_plan("us", "wide", "q", "q2", stores=["google_shopping"])] == [
        "google_shopping", "google_shopping"]


def test_store_choice_validation(client, stores):
    bad = lambda **o: client.post("/api/v1/shopping/intents", json={"text": KB, **o}, headers=A)
    assert bad(stores=["temu"]).status_code == 400
    assert bad(stores=[]).status_code == 400
    r = bad(stores=["walmart"], market="uk")
    assert r.status_code == 400 and "Walmart" not in r.json()["detail"].split("available:")[1]
