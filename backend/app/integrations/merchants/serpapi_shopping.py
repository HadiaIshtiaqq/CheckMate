"""Real product discovery through SerpAPI's Google Shopping engine.

One search returns live listings from many real stores (Google Shopping's
index). Only what the listing actually states is used: title, store, price,
rating, delivery text and listing extensions. Warranty, return policy and tax
are usually NOT in a listing; they stay UNKNOWN and become items the shopper
confirms on the store's page. CHECKMATE cannot inspect or pay through a real
store's checkout, so these offers end in a 'buy at the store' hand-off.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from decimal import Decimal

import httpx
from sqlalchemy.orm import sessionmaker

from app.core.util import money, money_str, utcnow
from app.database.models import ExternalOffer
from app.integrations.merchants.base import MerchantAdapter

log = logging.getLogger("checkmate.serpapi")
SERPAPI_URL = "https://serpapi.com/search.json"
SOURCE_LABEL = "Google Shopping via SerpAPI"
GL_BY_CURRENCY = {"USD": "us", "GBP": "uk", "EUR": "de", "CAD": "ca", "AUD": "au", "INR": "in"}
DOLLAR_BY_GL = {"us": "USD", "ca": "CAD", "au": "AUD"}
SYMBOL_CURRENCY = {"£": "GBP", "€": "EUR", "₹": "INR"}

# Large, long-established retailers. Matching a listing's store name against this
# list is the only seller evidence a Google Shopping listing provides; it is shown
# to the user as exactly that ("name matched"), never as a verified identity.
ESTABLISHED_RETAILERS = [
    "amazon.com", "amazon", "walmart", "best buy", "target", "costco", "the home depot", "home depot", "lowe's",
    "apple", "newegg", "b&h photo-video-audio", "b&h photo", "dell", "hp", "lenovo", "samsung", "microsoft",
    "wayfair", "ikea", "macy's", "kohl's", "nordstrom", "staples", "office depot", "dick's sporting goods", "rei",
    "nike", "adidas", "sephora", "ulta beauty", "walgreens", "cvs", "kroger", "sam's club", "bjs wholesale club",
    "argos", "currys", "john lewis", "tesco", "sainsbury's", "boots", "very", "ao.com", "mediamarkt", "saturn",
    "zalando", "otto", "fnac", "darty", "boulanger", "el corte ingles", "canadian tire", "the source",
    "jb hi-fi", "harvey norman", "flipkart", "croma", "reliance digital",
]
MARKETPLACE_HINTS = re.compile(r"\s[-–|]\s|marketplace|\bseller\b|ebay|etsy|aliexpress|temu|wish\.com|mercari", re.I)


class ProductSourceError(RuntimeError):
    """A product source failed. The message is safe to show to the user (no keys)."""


PREFIX_CURRENCY = {"US$": "USD", "CA$": "CAD", "C$": "CAD", "AU$": "AUD", "A$": "AUD", "NZ$": "NZD", "HK$": "HKD",
                   "S$": "SGD", "Rs": "INR"}


def currency_from_price(price_text: str | None, gl: str) -> str | None:
    if not price_text:
        return None
    for prefix, cur in PREFIX_CURRENCY.items():
        if price_text.strip().startswith(prefix):
            return cur
    for sym, cur in SYMBOL_CURRENCY.items():
        if sym in price_text:
            return cur
    if "$" in price_text:
        return DOLLAR_BY_GL.get(gl, "USD")
    m = re.search(r"\b(USD|EUR|GBP|CAD|AUD|INR)\b", price_text)
    return m.group(1) if m else None


def parse_shipping(delivery: str | None) -> str | None:
    """'Free delivery' -> 0.00, '$4.99 delivery' -> 4.99, anything else -> None (unknown)."""
    d = (delivery or "").lower()
    if not d:
        return None
    if re.search(r"\bfree\b[^.]{0,20}\b(delivery|shipping)\b", d):
        return "0.00"
    m = re.search(r"[$£€₹]\s?(\d+(?:\.\d{1,2})?)\s*(?:delivery|shipping)", d)
    return money_str(m.group(1)) if m else None


def specs_from_text(text: str) -> dict[str, str]:
    t = text.lower()
    specs: dict[str, str] = {}
    if (r := re.search(r"(\d+)\s*gb\s*(?:of\s*)?(?:ram|memory|ddr\d?|lpddr\d?\w*)", t)):
        specs["ram_gb"] = r.group(1)
    if (s := re.search(r"(\d+)\s*(gb|tb)\s*(?:pcie\s*)?(?:nvme\s*)?(?:ssd|storage|hdd|emmc|ufs)", t)):
        specs["storage_gb"] = str(int(s.group(1)) * (1000 if s.group(2) == "tb" else 1))
    if re.search(r"\bwireless\b|\bbluetooth\b|\btrue wireless\b", t):
        specs["wireless"] = "yes"
    if re.search(r"noise[\s-]*cancel+(?:ing|ation)|\banc\b", t):
        specs["noise_cancelling"] = "yes"
    if re.search(r"\bmechanical\b", t):
        specs["mechanical"] = "yes"
    if re.search(r"over[\s-]ear", t):
        specs["form"] = "over-ear"
    return specs


def is_established(store: str) -> bool:
    s = store.strip().lower()
    return any(s == r or s == f"{r}.com" or s.startswith(f"{r} ") for r in ESTABLISHED_RETAILERS)


def safe_url(url: str | None) -> str | None:
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else None


class SerpApiShoppingAdapter(MerchantAdapter):
    adapter_id = "google-shopping"
    supported_operations = frozenset({"search_products", "get_product_details", "get_seller_information",
                                      "get_purchase_terms", "quote_tax"})

    def __init__(self, api_key: str, session_factory: sessionmaker, cache_ttl: float = 3600, max_results: int = 12,
                 timeout: float = 20.0, transport: httpx.BaseTransport | None = None):
        self._key = api_key
        self._sessions = session_factory
        self._ttl = cache_ttl
        self._max = max_results
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()
        self.searches_made = 0

    # ---------------------------------------------------------------- search
    def search_products(self, query: str | None, category: str | None, budget: str | None = None,
                        currency: str | None = None, specs: dict[str, str] | None = None,
                        color: str | None = None) -> list[dict]:
        q = " ".join(x for x in [(query or "").strip(), (color or "").lower()] if x).strip()
        if not q:
            return []
        gl = GL_BY_CURRENCY.get((currency or "USD").upper(), "us")
        key = (q.lower(), gl)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < self._ttl:
                return [dict(x) for x in hit[1]]
        try:
            r = self._http.get(SERPAPI_URL, params={"engine": "google_shopping", "q": q, "gl": gl, "hl": "en",
                                                    "api_key": self._key})
        except httpx.HTTPError as e:
            raise ProductSourceError(f"Google Shopping search failed ({e.__class__.__name__}). Try again.") from e
        self.searches_made += 1
        try:
            data = r.json()
        except ValueError as e:
            raise ProductSourceError(f"Google Shopping returned an unreadable response (HTTP {r.status_code}).") from e
        err = data.get("error") if isinstance(data, dict) else None
        if err:
            if "hasn't returned any results" in str(err).lower() or "no results" in str(err).lower():
                return []
            raise ProductSourceError(f"Google Shopping (SerpAPI): {str(err).replace(self._key, '[key]')[:200]}")
        if r.status_code != 200:
            raise ProductSourceError(f"Google Shopping search failed (HTTP {r.status_code}).")
        retrieved = utcnow().isoformat()
        listings = []
        for item in data.get("shopping_results") or []:
            listing = self._to_listing(item, gl, q, retrieved)
            if listing:
                listings.append(listing)
            if len(listings) >= self._max:
                break
        self._persist(listings)
        with self._lock:
            self._cache[key] = (time.monotonic(), [dict(x) for x in listings])
        return listings

    def _to_listing(self, it: dict, gl: str, query: str, retrieved: str) -> dict | None:
        price = it.get("extracted_price")
        title = (it.get("title") or "").strip()
        store = (it.get("source") or "").strip()
        if price is None or not title:
            return None  # a listing without a stated price can't be evaluated
        try:
            price_dec = money(Decimal(str(price)))
        except ArithmeticError:
            return None
        cur = currency_from_price(it.get("price"), gl)
        ident = str(it.get("product_id") or f"{title}|{store}")
        oid = "ext-" + hashlib.sha256(f"{ident}|{store}|{gl}".encode()).hexdigest()[:20]
        snippet = (it.get("snippet") or "").strip()
        extensions = [str(x) for x in (it.get("extensions") or []) if isinstance(x, (str, int, float))]
        delivery = (it.get("delivery") or "").strip()
        return {
            "id": oid, "name": title[:200], "brand": "", "model": "", "category": query, "variants": [],
            "price": money_str(price_dec), "currency": cur, "seller_id": f"store:{store or 'unknown store'}",
            "listing_seller_name": store or "Unknown store", "specs": specs_from_text(f"{title} {snippet}"),
            "description": " ".join(x for x in [title, snippet] if x),
            "url": safe_url(it.get("product_link") or it.get("link")), "simulated": False, "external": True,
            "thumbnail": safe_url(it.get("thumbnail")), "rating": it.get("rating"), "reviews": it.get("reviews"),
            "multiple_sources": bool(it.get("multiple_sources")), "delivery_text": delivery,
            "extensions": extensions, "retrieved_at": retrieved, "source_label": SOURCE_LABEL, "gl": gl,
            "terms": {"shipping_cost": parse_shipping(delivery)} if parse_shipping(delivery) is not None else {},
            "policy_text": ". ".join(x for x in [*extensions, delivery, snippet] if x),
        }

    def _persist(self, listings: list[dict]) -> None:
        with self._sessions() as db:
            for l in listings:
                row = db.get(ExternalOffer, l["id"])
                if row:
                    row.data, row.retrieved_at = l, utcnow()
                else:
                    db.add(ExternalOffer(id=l["id"], data=l, retrieved_at=utcnow()))
            db.commit()

    def refresh_listing(self, product_id: str) -> dict | None:
        """Re-runs the original search without the cache (one paid search) and returns
        the same listing if it still appears."""
        stored = self._stored(product_id)
        if not stored:
            return None
        q, gl = stored.get("category") or "", stored.get("gl") or "us"
        with self._lock:
            self._cache.pop((q.lower(), gl), None)
        currency = {v: k for k, v in GL_BY_CURRENCY.items()}.get(gl, "USD")
        for listing in self.search_products(q, None, currency=currency):
            if listing["id"] == product_id:
                return listing
        return None

    # ------------------------------------------------- lookups (stored listings)
    def _stored(self, product_id: str) -> dict | None:
        with self._sessions() as db:
            row = db.get(ExternalOffer, product_id)
            return dict(row.data) if row else None

    def get_product_details(self, product_id: str) -> dict | None:
        return self._stored(product_id)

    def get_purchase_terms(self, product_id: str) -> dict | None:
        l = self._stored(product_id)
        if not l:
            return None
        return {"product_id": product_id, "structured": dict(l.get("terms") or {}),
                "policy_text": l.get("policy_text") or "", "source": l.get("url") or SOURCE_LABEL}

    def get_seller_information(self, seller_id: str) -> dict | None:
        if not seller_id.startswith("store:"):
            return None
        store = seller_id[len("store:"):]
        marketplace = bool(MARKETPLACE_HINTS.search(store))
        return {
            "seller_id": seller_id, "name": store, "marketplace": "Listed on Google Shopping",
            "identity_verified": None, "business_registration": None, "years_active": None, "rating": None,
            "rating_count": None, "dispute_rate_pct": None, "return_policy_published": None,
            "established_retailer": is_established(store) and not marketplace, "marketplace_seller": marketplace,
            "source": "Store name on the Google Shopping listing, matched against CHECKMATE's list of "
                      "established retailers",
        }

    def quote_tax(self, product_id: str, variant: str | None, quantity: int) -> dict:
        return {"amount": None, "status": "UNKNOWN", "rate": None,
                "note": "Sales tax is calculated by the store at checkout."}
