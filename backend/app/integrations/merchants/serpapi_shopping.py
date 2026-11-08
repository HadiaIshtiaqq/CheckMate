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
from sqlalchemy.exc import IntegrityError
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



# ======================================================== markets & engines ===
MARKETS: dict[str, dict] = {
    "us": {"name": "United States", "currency": "USD", "gl": "us", "amazon": "amazon.com", "ebay": "ebay.com",
           "walmart": True},
    "uk": {"name": "United Kingdom", "currency": "GBP", "gl": "uk", "amazon": "amazon.co.uk", "ebay": "ebay.co.uk"},
    "ca": {"name": "Canada", "currency": "CAD", "gl": "ca", "amazon": "amazon.ca", "ebay": "ebay.ca"},
    "au": {"name": "Australia", "currency": "AUD", "gl": "au", "amazon": "amazon.com.au", "ebay": "ebay.com.au"},
    "de": {"name": "Germany", "currency": "EUR", "gl": "de", "amazon": "amazon.de", "ebay": "ebay.de"},
    "fr": {"name": "France", "currency": "EUR", "gl": "fr", "amazon": "amazon.fr", "ebay": "ebay.fr"},
    "in": {"name": "India", "currency": "INR", "gl": "in", "amazon": "amazon.in"},
}
MARKET_BY_CURRENCY = {"USD": "us", "GBP": "uk", "CAD": "ca", "AUD": "au", "EUR": "de", "INR": "in"}
ENGINE_LABELS = {"google_shopping": "Google Shopping", "amazon": "Amazon", "ebay": "eBay", "walmart": "Walmart"}
DEPTHS = ("quick", "standard", "wide")
USED_CONDITION = re.compile(r"\b(pre-?owned|used|refurbished|renewed|open box|for parts|seller refurbished)\b", re.I)


def market_for(market: str | None, currency: str | None) -> str:
    if market in MARKETS:
        return market
    return MARKET_BY_CURRENCY.get((currency or "USD").upper(), "us")


def search_plan(market: str, depth: str, query: str, alt_query: str | None = None) -> list[tuple[str, str]]:
    """(engine, query) pairs for one request. Each pair is one SerpAPI search."""
    m = MARKETS[market]
    plan = [("google_shopping", query)]
    if depth in ("standard", "wide"):
        if m.get("amazon"):
            plan.append(("amazon", query))
        if m.get("ebay"):
            plan.append(("ebay", query))
    if depth == "wide":
        if m.get("walmart"):
            plan.append(("walmart", query))
        if alt_query and alt_query.strip().lower() != query.strip().lower():
            plan.append(("google_shopping", alt_query))
    return plan


def search_options() -> dict:
    return {"markets": [{"code": k, "name": v["name"], "currency": v["currency"],
                         "sources": [ENGINE_LABELS[e] for e, _ in search_plan(k, "wide", "q")]}
                        for k, v in MARKETS.items()],
            "depths": {d: {k: len({e for e, _ in search_plan(k, d, "a", "b")}) +
                           (1 if d == "wide" else 0) for k in MARKETS} for d in DEPTHS}}


def _num(v) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return money(Decimal(str(v)))
    except ArithmeticError:
        return None


class SerpApiShoppingAdapter(MerchantAdapter):
    """Real listings from Google Shopping, Amazon, eBay and Walmart through SerpAPI."""

    adapter_id = "serpapi"
    supported_operations = frozenset({"search_products", "get_product_details", "get_seller_information",
                                      "get_purchase_terms", "quote_tax"})

    def __init__(self, api_key: str, session_factory: sessionmaker, cache_ttl: float = 3600, max_results: int = 20,
                 timeout: float = 25.0, transport: httpx.BaseTransport | None = None):
        self._key = api_key
        self._sessions = session_factory
        self._ttl = cache_ttl
        self._max = max_results
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._cache: dict[tuple[str, str, str], tuple[float, list[dict]]] = {}
        self._sellers: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.searches_made = 0
        self.last_errors: list[str] = []

    # ---------------------------------------------------------------- search
    def search_products(self, query: str | None, category: str | None, budget: str | None = None,
                        currency: str | None = None, specs: dict[str, str] | None = None,
                        color: str | None = None, market: str | None = None, depth: str = "quick",
                        alt_query: str | None = None) -> list[dict]:
        q = " ".join(x for x in [(query or "").strip(), (color or "").lower()] if x).strip()
        if not q:
            return []
        mk = market_for(market, currency)
        alt = " ".join(x for x in [(alt_query or "").strip(), (color or "").lower()] if x).strip() or None
        plan = search_plan(mk, depth if depth in DEPTHS else "standard", q, alt)
        merged: dict[str, dict] = {}
        errors: list[str] = []
        for engine, eq in plan:
            try:
                for l in self._engine_search(engine, eq, mk):
                    merged.setdefault(l["id"], l)
            except ProductSourceError as e:
                errors.append(str(e))
        self.last_errors = errors
        if not merged and errors:
            raise ProductSourceError(errors[0])
        return list(merged.values())

    def _engine_search(self, engine: str, q: str, market: str, use_cache: bool = True) -> list[dict]:
        m = MARKETS[market]
        key = (engine, q.lower(), market)
        if use_cache:
            with self._lock:
                hit = self._cache.get(key)
                if hit and time.monotonic() - hit[0] < self._ttl:
                    return [dict(x) for x in hit[1]]
        params: dict = {"engine": engine, "api_key": self._key}
        if engine == "google_shopping":
            params.update({"q": q, "gl": m["gl"], "hl": "en"})
        elif engine == "amazon":
            params.update({"k": q, "amazon_domain": m["amazon"]})
        elif engine == "ebay":
            params.update({"_nkw": q, "ebay_domain": m["ebay"]})
        elif engine == "walmart":
            params.update({"query": q})
        label = ENGINE_LABELS[engine]
        try:
            r = self._http.get(SERPAPI_URL, params=params)
        except httpx.HTTPError as e:
            raise ProductSourceError(f"{label} search failed ({e.__class__.__name__}). Try again.") from e
        self.searches_made += 1
        try:
            data = r.json()
        except ValueError as e:
            raise ProductSourceError(f"{label} returned an unreadable response (HTTP {r.status_code}).") from e
        err = data.get("error") if isinstance(data, dict) else None
        if err:
            if "hasn't returned any results" in str(err).lower() or "no results" in str(err).lower():
                return []
            raise ProductSourceError(f"{label} (SerpAPI): {str(err).replace(self._key, '[key]')[:200]}")
        if r.status_code != 200:
            raise ProductSourceError(f"{label} search failed (HTTP {r.status_code}).")
        retrieved = utcnow().isoformat()
        parse = {"google_shopping": self._google, "amazon": self._amazon, "ebay": self._ebay,
                 "walmart": self._walmart}[engine]
        items = data.get("shopping_results" if engine == "google_shopping" else "organic_results") or []
        listings, seen = [], set()
        for item in items:
            if not isinstance(item, dict):
                continue
            l = parse(item, market, q, retrieved)
            if l and l["id"] not in seen:  # sources can list the same offer twice
                l.update({"engine": engine, "market": market, "query": q, "gl": m["gl"],
                          "source_label": f"{label} via SerpAPI"})
                seen.add(l["id"])
                listings.append(l)
            if len(listings) >= self._max:
                break
        self._persist(listings)
        with self._lock:
            self._cache[key] = (time.monotonic(), [dict(x) for x in listings])
        return listings

    # ---------------------------------------------------------------- parsers
    @staticmethod
    def _oid(engine: str, ident: str, market: str) -> str:
        return "ext-" + hashlib.sha256(f"{engine}|{ident}|{market}".encode()).hexdigest()[:20]

    @staticmethod
    def _base(oid: str, title: str, price: Decimal, cur: str | None, seller_id: str, store: str, query: str,
              url: str | None, thumbnail: str | None, retrieved: str, delivery: str, notes: list[str],
              snippet: str = "") -> dict:
        ship = parse_shipping(delivery)
        return {
            "id": oid, "name": title[:200], "brand": "", "model": "", "category": query, "variants": [],
            "price": money_str(price), "currency": cur, "seller_id": seller_id, "listing_seller_name": store,
            "specs": specs_from_text(f"{title} {snippet}"), "description": " ".join(x for x in [title, snippet] if x),
            "url": safe_url(url), "simulated": False, "external": True, "thumbnail": safe_url(thumbnail),
            "rating": None, "reviews": None, "multiple_sources": False, "delivery_text": delivery,
            "extensions": notes, "retrieved_at": retrieved,
            "terms": {"shipping_cost": ship} if ship is not None else {},
            "policy_text": ". ".join(x for x in [*notes, delivery, snippet] if x), "condition": None,
            "sponsored": False,
        }

    def _google(self, it: dict, market: str, query: str, retrieved: str) -> dict | None:
        price, title = _num(it.get("extracted_price")), (it.get("title") or "").strip()
        store = (it.get("source") or "").strip()
        if price is None or not title:
            return None  # a listing without a stated price can't be evaluated
        gl = MARKETS[market]["gl"]
        oid = "ext-" + hashlib.sha256(f"{it.get('product_id') or title + '|' + store}|{store}|{gl}".encode()).hexdigest()[:20]
        notes = [str(x) for x in (it.get("extensions") or []) if isinstance(x, (str, int, float))]
        l = self._base(oid, title, price, currency_from_price(it.get("price"), gl), f"store:{store or 'unknown store'}",
                       store or "Unknown store", query, it.get("product_link") or it.get("link"), it.get("thumbnail"),
                       retrieved, (it.get("delivery") or "").strip(), notes, (it.get("snippet") or "").strip())
        l.update({"rating": it.get("rating"), "reviews": it.get("reviews"),
                  "multiple_sources": bool(it.get("multiple_sources"))})
        return l

    def _amazon(self, it: dict, market: str, query: str, retrieved: str) -> dict | None:
        price, title, asin = _num(it.get("extracted_price")), (it.get("title") or "").strip(), it.get("asin")
        if price is None or not title or not asin:
            return None
        delivery = it.get("delivery")
        delivery = " · ".join(str(x) for x in delivery) if isinstance(delivery, list) else str(delivery or "")
        store = "Amazon (marketplace - seller not shown)"
        l = self._base(self._oid("amazon", str(asin), market), title, price,
                       currency_from_price(it.get("price"), MARKETS[market]["gl"]), f"store:{store}", store, query,
                       it.get("link_clean") or it.get("link"), it.get("thumbnail"), retrieved, delivery,
                       [str(b) for b in (it.get("badges") or []) if isinstance(b, str)])
        l.update({"rating": it.get("rating"), "reviews": it.get("reviews"), "sponsored": bool(it.get("sponsored"))})
        return l

    def _ebay(self, it: dict, market: str, query: str, retrieved: str) -> dict | None:
        title = (it.get("title") or "").strip()
        p = it.get("price") or {}
        price_obj = p.get("from") if isinstance(p, dict) and "from" in p else p
        price = _num(price_obj.get("extracted")) if isinstance(price_obj, dict) else None
        if price is None or not title:
            return None
        seller = it.get("seller") or {}
        username = (seller.get("username") or "").strip() or "unknown seller"
        sid = f"ebay:{username}"
        with self._lock:
            self._sellers[sid] = {"username": username, "reviews": seller.get("reviews"),
                                  "positive_pct": seller.get("positive_feedback_in_percentage")}
        notes = [x for x in [it.get("condition"), it.get("buying_format_text"), it.get("location"),
                             "price range (several variants)" if isinstance(p, dict) and "from" in p else None] if x]
        raw = price_obj.get("raw") if isinstance(price_obj, dict) else None
        l = self._base(self._oid("ebay", str(it.get("product_id") or it.get("link") or title), market), title, price,
                       currency_from_price(raw, MARKETS[market]["gl"]), sid, f"eBay - {username}", query,
                       it.get("link"), it.get("thumbnail"), retrieved, str(it.get("shipping") or ""),
                       [str(x) for x in notes], (it.get("subtitle") or "").strip())
        l.update({"condition": it.get("condition"), "sponsored": bool(it.get("sponsored")),
                  "seller_feedback": self._sellers[sid]})
        return l

    def _walmart(self, it: dict, market: str, query: str, retrieved: str) -> dict | None:
        offer = it.get("primary_offer") or {}
        price, title = _num(offer.get("offer_price")), (it.get("title") or "").strip()
        if price is None or not title or it.get("out_of_stock"):
            return None  # out-of-stock items can't be bought
        store = (it.get("seller_name") or "Walmart.com").strip()
        delivery = "Free shipping" if it.get("free_shipping") else ""
        if it.get("two_day_shipping"):
            delivery = (delivery + " · 2-day shipping").strip(" ·")
        l = self._base(self._oid("walmart", str(it.get("product_id") or it.get("us_item_id") or title), market),
                       title, price, "USD", f"store:{store}", store, query, it.get("product_page_url"),
                       it.get("thumbnail"), retrieved, delivery, [])
        if it.get("free_shipping"):
            l["terms"] = {"shipping_cost": "0.00"}
        l.update({"rating": it.get("rating"), "reviews": it.get("reviews"), "sponsored": bool(it.get("sponsored"))})
        return l

    def _persist(self, listings: list[dict]) -> None:
        with self._sessions() as db:
            for l in {x["id"]: x for x in listings}.values():
                db.merge(ExternalOffer(id=l["id"], data=l, retrieved_at=utcnow()))  # insert-or-update
            try:
                db.commit()
            except IntegrityError:  # a concurrent search stored the same offer first
                db.rollback()

    def refresh_listing(self, product_id: str) -> dict | None:
        """Re-runs the search that found this listing, bypassing the cache (one paid
        search), and returns the same listing if it still appears."""
        stored = self._stored(product_id)
        if not stored:
            return None
        engine = stored.get("engine") or "google_shopping"
        market = stored.get("market") or {v["gl"]: k for k, v in MARKETS.items()}.get(stored.get("gl") or "us", "us")
        q = stored.get("query") or stored.get("category") or ""
        for listing in self._engine_search(engine, q, market, use_cache=False):
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
        if seller_id.startswith("ebay:"):
            fb = self._sellers.get(seller_id) or {"username": seller_id[5:], "reviews": None, "positive_pct": None}
            n, pct = fb.get("reviews"), fb.get("positive_pct")
            return {
                "seller_id": seller_id, "name": f"eBay - {fb['username']}", "marketplace": "eBay",
                "identity_verified": None, "business_registration": None, "years_active": None,
                "rating": f"{pct}% positive" if pct is not None else None, "rating_count": n,
                "dispute_rate_pct": None, "return_policy_published": None,
                "feedback_score": n, "positive_pct": pct,
                "source": "eBay seller feedback shown on the listing (via SerpAPI)",
            }
        if not seller_id.startswith("store:"):
            return None
        store = seller_id[len("store:"):]
        marketplace = bool(MARKETPLACE_HINTS.search(store))
        return {
            "seller_id": seller_id, "name": store, "marketplace": "Listed on a shopping search",
            "identity_verified": None, "business_registration": None, "years_active": None, "rating": None,
            "rating_count": None, "dispute_rate_pct": None, "return_policy_published": None,
            "established_retailer": is_established(store) and not marketplace, "marketplace_seller": marketplace,
            "source": "Store name on the listing, matched against CHECKMATE's list of established retailers",
        }

    def quote_tax(self, product_id: str, variant: str | None, quantity: int) -> dict:
        return {"amount": None, "status": "UNKNOWN", "rate": None,
                "note": "Sales tax is calculated by the store at checkout."}
