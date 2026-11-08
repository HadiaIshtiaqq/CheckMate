"""Duplicate listings, product-type relevance, choosing any offer, ranking and safe errors."""
import httpx
import pytest

from app.database.session import SessionLocal
from app.integrations.merchants.router import CatalogRouter
from app.integrations.merchants.serpapi_shopping import SerpApiShoppingAdapter

from tests.conftest import A, get_wf, start
from tests.integration import test_real_stores as rs

CHAIRS = {"shopping_results": [
    {"title": "Office Chair", "product_id": "fab1", "source": "Fab by Epic Games", "price": "$4.99",
     "extracted_price": 4.99, "product_link": "https://www.google.com/shopping/product/fab1"},
    {"title": "Office Chair Seat Cushion Memory Foam", "product_id": "cu1", "source": "Target", "price": "$19.99",
     "extracted_price": 19.99, "product_link": "https://www.google.com/shopping/product/cu1"},
    {"title": "Mainstays Folding Chair", "product_id": "w1", "source": "Walmart", "price": "$12.94",
     "extracted_price": 12.94, "rating": 4.3, "reviews": 72, "extensions": ["Free 30-day returns"],
     "product_link": "https://www.google.com/shopping/product/w1"},
    {"title": "HON Ergonomic Office Chair", "product_id": "h1", "source": "Staples", "price": "$99.99",
     "extracted_price": 99.99, "rating": 4.6, "reviews": 1500, "extensions": ["Free 30-day returns"],
     "product_link": "https://www.google.com/shopping/product/h1"},
    {"title": "Cheap Chair", "product_id": "x1", "source": "TinyShop", "price": "$9.00", "extracted_price": 9.0,
     "extensions": ["Free 30-day returns"], "product_link": "https://www.google.com/shopping/product/x1"},
    {"title": "Office Chair", "product_id": "fab1", "source": "Fab by Epic Games", "price": "$4.99",
     "extracted_price": 4.99, "product_link": "https://www.google.com/shopping/product/fab1"},  # duplicate
    {"title": "Executive Leather Chair", "product_id": "big", "source": "Wayfair", "price": "$420.00",
     "extracted_price": 420.0, "product_link": "https://www.google.com/shopping/product/big"},
]}


@pytest.fixture
def chair_store(monkeypatch):
    adapter = SerpApiShoppingAdapter("k", SessionLocal,
                                     transport=httpx.MockTransport(lambda r: httpx.Response(200, json=CHAIRS)))
    rs.install(monkeypatch, CatalogRouter(None, adapter))
    return adapter


REQ = "Find an office chair under $150 with free returns."


def by_title(wf, title, store):
    return next(e for e in wf["evaluations"] if e["candidate"]["product_name"] == title
                and e["candidate"]["listing_seller_name"] == store)


def test_duplicate_listings_do_not_crash(client, chair_store):
    wf = start(client, REQ)
    assert wf["state"] == "CONTRACT_CREATED"
    assert len({e["candidate"]["id"] for e in wf["evaluations"]}) == len(wf["evaluations"]) == 6


def test_digital_items_accessories_and_off_topic_listings_fail(client, chair_store):
    wf = start(client, REQ)
    fab = by_title(wf, "Office Chair", "Fab by Epic Games")
    cushion = by_title(wf, "Office Chair Seat Cushion Memory Foam", "Target")
    assert fab["status"] == "FAIL" and any(c["key"] == "product_type" and "digital" in c["reason"]
                                           for c in fab["conditions"])
    assert cushion["status"] == "FAIL" and any("accessory" in c["reason"] for c in cushion["conditions"])


def test_plan_prefers_established_well_reviewed_store_over_cheapest(client, chair_store):
    wf = start(client, REQ)
    t = wf["contract"]["terms"]
    assert t["store_name"] in ("Walmart", "Staples")  # never the $4.99 3D model or the unknown $9 shop
    assert t["store_name"] == "Staples"  # Staples: strong evidence, 4.6 stars x 1500 reviews


def test_choose_any_offer_with_override(client, chair_store):
    wf = start(client, REQ)
    pricey = by_title(wf, "Executive Leather Chair", "Wayfair")
    assert pricey["status"] == "FAIL"
    url = f"/api/v1/workflows/{wf['id']}/alternatives/{pricey['candidate']['id']}/select"
    first = client.post(url, headers=A)
    assert first.status_code == 409 and first.json()["detail"]["reasons"]
    r = client.post(url + "?override=true", headers=A)
    assert r.status_code == 200
    t = r.json()["terms"]
    assert t["store_name"] == "Wayfair" and t["overridden_rules"] and t["max_total"] == "420.00"
    ev = client.get(f"/api/v1/workflows/{wf['id']}/events", headers=A).json()["events"]
    assert any(e["type"] == "contract.rules_overridden" for e in ev)
    # and the overridden plan can be approved and completed
    after = get_wf(client, wf["id"])
    client.post(f"/api/v1/contracts/{after['contract']['id']}/approve",
                json={"contract_hash": after["contract"]["contract_hash"]}, headers=A)
    keys = [i["key"] for i in t["checklist"] if not i.get("auto")]
    rep = client.post(f"/api/v1/contracts/{after['contract']['id']}/report-purchase",
                      json={"amount": "430.00", "currency": "USD", "confirmations": {k: True for k in keys}},
                      headers=A).json()
    body = client.get(f"/api/v1/passports/{rep['passport_id']}", headers=A).json()["body"]
    assert any("overriding" in n for n in body["reconciliation"]["notes"])


def test_demo_store_offers_cannot_be_overridden(client):
    wf = start(client)
    bose = next(e for e in wf["evaluations"] if e["candidate"]["id"] == "hp-sonic-pro")
    r = client.post(f"/api/v1/workflows/{wf['id']}/alternatives/hp-sonic-pro/select?override=true", headers=A)
    assert bose["status"] == "FAIL" and r.status_code == 409


def test_agent_errors_are_safe_for_users():
    from sqlalchemy.exc import IntegrityError

    from app.agents.base import safe_error

    e = IntegrityError("INSERT INTO external_offers (id, data) VALUES (?, ?)", {"secret": "x"}, Exception("UNIQUE"))
    msg = safe_error(e)
    assert "INSERT" not in msg and "storage error" in msg
    assert len(safe_error(ValueError("x" * 500))) < 200


def test_choose_an_offer_when_nothing_qualified(client, chair_store):
    wf = start(client, "Find an office chair under $5 with free returns.")
    assert wf["state"] == "EVIDENCE_INCOMPLETE" and wf["contract"] is None
    hon = by_title(wf, "HON Ergonomic Office Chair", "Staples")
    r = client.post(f"/api/v1/workflows/{wf['id']}/alternatives/{hon['candidate']['id']}/select?override=true",
                    headers=A)
    assert r.status_code == 200 and r.json()["version"] == 1
    assert get_wf(client, wf["id"])["state"] == "CONTRACT_CREATED"
