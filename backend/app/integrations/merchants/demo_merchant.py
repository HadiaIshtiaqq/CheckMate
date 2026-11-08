"""Controlled demo merchant. It exposes the exact product, seller, add-ons, fees
and final total of a checkout, which makes checkout verification demonstrable
without pretending CHECKMATE can inspect arbitrary retailer websites."""
from __future__ import annotations

import copy
import hashlib
import re
import threading
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from app.core.util import money, money_str, new_id, utcnow
from app.database.models import DemoMerchantCheckout
from app.integrations.merchants.base import OPERATIONS, MerchantAdapter
from app.integrations.merchants.catalog import PRODUCTS, SCENARIOS, SELLERS

SOURCE_LABEL = "Controlled demo merchant checkout API (simulated)"
RECEIPT_LABEL = "SIMULATED merchant receipt issued by the controlled demo merchant"

# ------------------------------------------------- simulated listing generator
GEN_BRANDS = ["Northfield", "Aster", "Kestrel", "Lumio", "Orbis", "Vanta", "Halden", "Pinecrest", "Solace",
              "Tidewell", "Corvo", "Meridian"]
GEN_MODELS = ["Classic", "Pro", "Lite", "Plus", "Essential", "Max", "Air", "Prime"]
ALL_COLOURS = ["Black", "White", "Grey", "Blue", "Red", "Silver", "Brown", "Green", "Graphite", "Stainless"]
GEN_ID_RE = re.compile(r"^gen-(?P<slug>[a-z0-9-]+)-(?P<cents>\d+)-(?P<cur>[a-z]{3})-(?P<i>[0-4])$")


def _slug(phrase: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", phrase.lower()).strip("-")[:40].strip("-") or "item"


def generated_ids(phrase: str, budget: Decimal, currency: str) -> list[str]:
    cents = int(budget * 100)
    return [f"gen-{_slug(phrase)}-{cents}-{currency.lower()}-{i}" for i in range(5)]


def generate_listing(product_id: str, specs: dict[str, str] | None = None) -> dict | None:
    """Deterministic: the same id always yields the same listing (survives restarts).
    Five offers per request, deliberately mixed so the integrity checks have work to do:
    0 good fit (strong seller), 1 over budget, 2 hidden subscription, 3 undisclosed terms,
    4 short warranty / paid returns."""
    m = GEN_ID_RE.match(product_id)
    if not m:
        return None
    slug, i, cur = m.group("slug"), int(m.group("i")), m.group("cur").upper()
    budget = Decimal(m.group("cents")) / 100
    phrase = slug.replace("-", " ")
    title = " ".join(w.capitalize() for w in phrase.split())
    h = int(hashlib.sha256(slug.encode()).hexdigest()[:8], 16)
    brand = GEN_BRANDS[(h + i * 5) % len(GEN_BRANDS)]
    model = GEN_MODELS[(h // 7 + i) % len(GEN_MODELS)]

    def price(f: str) -> str:
        p = budget * Decimal(f)
        return money_str(p.quantize(Decimal("1")) - Decimal("0.01") if p >= 10 else p)

    def ship(f: str, cap: int) -> str:
        return money_str(min(budget * Decimal(f), Decimal(cap)))

    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(cur, "$")
    if i == 0:
        s = ship("0.05", 15)
        seller, variants, terms = "merchant-d", ALL_COLOURS, {
            "warranty_months": 24, "return_window_days": 30, "free_returns": True, "shipping_cost": s,
            "delivery_days": 4, "subscription_required": False}
        text = (f"24-month manufacturer warranty. Free returns within 30 days. Shipping {sym}{s}, delivered in 2-4 "
                f"business days. No subscription is required.")
        p = price("0.66")
    elif i == 1:
        seller, variants, terms = "merchant-a", ALL_COLOURS, {
            "warranty_months": 24, "return_window_days": 30, "free_returns": True, "shipping_cost": "0.00",
            "delivery_days": 3, "subscription_required": False}
        text = ("24-month warranty. Free returns within 30 days. Free shipping, delivered in 1-3 business days. "
                "No subscription is required.")
        p = price("0.98")
    elif i == 2:
        s, fee = ship("0.04", 10), money_str(max(budget * Decimal("0.09"), Decimal("1.99")))
        seller, variants, terms = "merchant-b", ALL_COLOURS, {
            "warranty_months": 12, "return_window_days": 30, "free_returns": True, "shipping_cost": s,
            "delivery_days": 5, "subscription_required": True,
            "recurring_charges": [{"label": f"{brand} Care membership", "amount": fee, "interval": "month"}]}
        text = (f"12-month warranty. Free returns within 30 days. Shipping {sym}{s}. A {brand} Care subscription is "
                f"required at {sym}{fee} per month and renews automatically.")
        p = price("0.50")
    elif i == 3:
        seller, variants, terms = "merchant-c", ["Black"], {}
        text = "Ships fast! Returns at seller's discretion. Contact seller for warranty details."
        p = price("0.40")
    else:
        s = ship("0.04", 9)
        seller, variants, terms = "merchant-b", ["Black"], {
            "warranty_months": 3, "return_window_days": 14, "free_returns": False, "shipping_cost": s,
            "delivery_days": 6, "subscription_required": False}
        text = (f"3-month warranty. Returns within 14 days; a 15% restocking fee applies. Shipping {sym}{s}, "
                f"delivered in 4-6 business days. No subscription is required.")
        p = price("0.60")
    listing_name = SELLERS[seller]["name"] if i != 3 else "QuickBuy Official Store"
    return {
        "id": product_id, "name": f"{brand} {model} {title}", "brand": brand, "model": model, "category": phrase,
        "variants": variants, "price": p, "currency": cur, "seller_id": seller, "listing_seller_name": listing_name,
        "specs": dict(specs or {}) if i < 3 else {},
        "description": f"Simulated demo listing for '{phrase}'.", "terms": terms, "policy_text": text,
        "url": f"https://demo-merchant.checkmate.test/products/{product_id}", "simulated": True,
    }


class DemoMerchantAdapter(MerchantAdapter):
    adapter_id = "demo-merchant"
    supported_operations = frozenset(OPERATIONS)

    def __init__(self, session_factory: sessionmaker):
        self._sessions = session_factory
        self._lock = threading.Lock()
        self._price_overrides: dict[str, str] = {}
        self._generated_specs: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------ catalogue
    def _product(self, product_id: str) -> dict | None:
        item = None
        if product_id.startswith("gen-"):
            item = generate_listing(product_id, self._generated_specs.get(product_id))
        else:
            for p in PRODUCTS:
                if p["id"] == product_id:
                    item = copy.deepcopy(p)
                    break
        if item and product_id in self._price_overrides:
            item["price"] = self._price_overrides[product_id]
        return item

    def search_products(self, query: str | None, category: str | None, budget: str | None = None,
                        currency: str | None = None, specs: dict[str, str] | None = None) -> list[dict]:
        """Curated catalogue for the categories it stocks; for anything else the
        demo merchant produces clearly-labelled simulated listings for the request."""
        words = [w for w in (query or "").lower().split() if len(w) > 2]
        if category:
            curated = [p for p in PRODUCTS if p["category"] == category]
        else:  # a named curated brand still finds its products
            curated = [p for p in PRODUCTS if p["brand"].lower() in words]
        if curated:
            return [self._listing(p["id"]) for p in curated]
        phrase = " ".join(words) or (query or "").strip()
        if not phrase or budget is None:
            return []
        ids = generated_ids(phrase, money(budget), currency or "USD")
        with self._lock:
            for i, pid in enumerate(ids):
                if i < 3 and specs:  # the stronger offers state the requested specifications
                    self._generated_specs[pid] = dict(specs)
        return [self._listing(pid) for pid in ids]

    def _listing(self, product_id: str) -> dict:
        p = self._product(product_id)
        assert p is not None
        return {k: p.get(k) for k in (
            "id", "name", "brand", "model", "category", "variants", "price", "currency", "seller_id",
            "listing_seller_name", "specs", "description", "url", "simulated",
        )}

    def get_product_details(self, product_id: str) -> dict | None:
        p = self._product(product_id)
        return self._listing(product_id) if p else None

    def get_seller_information(self, seller_id: str) -> dict | None:
        s = SELLERS.get(seller_id)
        if not s:
            return None
        info = {k: v for k, v in s.items() if k != "tax_rate"}
        info["source"] = f"demo-merchant seller registry #{seller_id}"
        return info

    def get_purchase_terms(self, product_id: str) -> dict | None:
        p = self._product(product_id)
        if not p:
            return None
        return {
            "product_id": product_id,
            "structured": copy.deepcopy(p["terms"]),
            "policy_text": p["policy_text"],
            "source": f"{p['url']}#terms",
        }

    def quote_tax(self, product_id: str, variant: str | None, quantity: int) -> dict:
        p = self._product(product_id)
        if not p:
            return {"amount": None, "status": "UNKNOWN", "rate": None}
        rate = SELLERS[p["seller_id"]]["tax_rate"]
        if rate is None:
            return {"amount": None, "status": "UNKNOWN", "rate": None}
        amount = money(Decimal(p["price"]) * quantity * Decimal(rate))
        return {"amount": money_str(amount), "status": "ESTIMATED", "rate": rate}

    # --- demo knob: lets the monitoring agent observe an offer change --------
    def set_price_override(self, product_id: str, price: str | None) -> None:
        with self._lock:
            if price is None:
                self._price_overrides.pop(product_id, None)
            else:
                self._price_overrides[product_id] = money_str(price)

    # ------------------------------------------------------------- checkout
    def create_checkout(self, product_id: str, variant: str | None, quantity: int, scenario: str) -> dict:
        if scenario not in SCENARIOS:
            raise ValueError(f"Unknown demo checkout scenario: {scenario}")
        p = self._product(product_id)
        if not p:
            raise ValueError(f"Unknown product {product_id}")
        seller = SELLERS[p["seller_id"]]
        currency = "EUR" if scenario == "currency_swap" else p["currency"]
        price = Decimal(p["price"])
        terms = p["terms"]
        chosen_variant = variant
        seller_id, seller_name = p["seller_id"], seller["name"]
        if scenario == "variant_swap":
            others = [v for v in p["variants"] if v != variant]
            chosen_variant = others[0] if others else f"{variant or 'Default'} (refurbished)"
        if scenario == "seller_swap":
            seller_id, seller_name = "merchant-c", SELLERS["merchant-c"]["name"]

        lines = [{
            "label": f"{p['name']} ({chosen_variant})" if chosen_variant else p["name"],
            "kind": "product", "amount": money_str(price * quantity), "currency": currency,
            "status": "CONFIRMED", "sku": product_id, "quantity": quantity,
        }]
        shipping = terms.get("shipping_cost")
        lines.append({"label": "Shipping", "kind": "shipping", "amount": shipping, "currency": currency,
                      "status": "CONFIRMED" if shipping is not None else "UNKNOWN"})
        rate = seller["tax_rate"]
        tax = money(price * quantity * Decimal(rate)) if rate else None
        lines.append({"label": "Sales tax", "kind": "tax", "amount": money_str(tax), "currency": currency,
                      "status": "CONFIRMED" if tax is not None else "UNKNOWN"})
        for rc in terms.get("recurring_charges", []):
            lines.append({"label": rc["label"], "kind": "subscription", "amount": rc["amount"],
                          "currency": currency, "status": "CONFIRMED", "recurring": True,
                          "interval": rc["interval"], "preselected": True, "sku": "sub-" + product_id})
        if scenario == "subscription_addon":
            lines.append({"label": "Care+ Protection subscription (pre-selected)", "kind": "subscription",
                          "amount": "15.00", "currency": currency, "status": "CONFIRMED", "recurring": True,
                          "interval": "month", "preselected": True, "sku": "careplus-monthly"})
        if scenario == "hidden_fee":
            lines.append({"label": "Service fee (calculated at payment)", "kind": "fee", "amount": None,
                          "currency": currency, "status": "UNKNOWN"})

        known = [Decimal(l["amount"]) for l in lines if l["amount"] is not None]
        all_known = all(l["amount"] is not None for l in lines)
        stated_total = money_str(sum(known, Decimal("0"))) if all_known else None

        checkout_terms = {
            "warranty_months": None if scenario == "warranty_removed" else terms.get("warranty_months"),
            "return_window_days": terms.get("return_window_days"),
            "free_returns": terms.get("free_returns"),
        }
        snapshot = {
            "checkout_id": new_id("mchk"),
            "merchant_id": self.adapter_id,
            "product_id": product_id,
            "product_name": p["name"],
            "variant": chosen_variant,
            "seller_id": seller_id,
            "seller_name": seller_name,
            "quantity": quantity,
            "currency": currency,
            "line_items": lines,
            "stated_total": stated_total,
            "terms": checkout_terms,
            "captured_at": utcnow().isoformat(),
            "source_label": SOURCE_LABEL,
        }
        with self._sessions() as db:
            db.add(DemoMerchantCheckout(id=snapshot["checkout_id"], scenario=scenario, snapshot=snapshot))
            db.commit()
        return snapshot

    def _load(self, checkout_id: str) -> DemoMerchantCheckout | None:
        with self._sessions() as db:
            return db.get(DemoMerchantCheckout, checkout_id)

    def get_checkout_snapshot(self, checkout_id: str) -> dict | None:
        row = self._load(checkout_id)
        return copy.deepcopy(row.snapshot) if row else None

    def get_order_status(self, checkout_id: str) -> dict | None:
        row = self._load(checkout_id)
        return {"checkout_id": checkout_id, "status": "OPEN"} if row else None

    def get_receipt(self, checkout_id: str) -> dict | None:
        row = self._load(checkout_id)
        if not row:
            return None
        snap = row.snapshot
        lines = copy.deepcopy(snap["line_items"])
        if row.scenario == "receipt_discrepancy":
            lines.append({"label": "Expedited handling", "kind": "fee", "amount": "10.00",
                          "currency": snap["currency"], "status": "CONFIRMED"})
        total = sum((Decimal(l["amount"]) for l in lines if l["amount"] is not None), Decimal("0"))
        return {
            "receipt_id": "rcpt_" + checkout_id.split("_", 1)[-1],
            "checkout_id": checkout_id,
            "merchant": snap["seller_name"],
            "product_id": snap["product_id"],
            "variant": snap["variant"],
            "quantity": snap["quantity"],
            "lines": lines,
            "total": money_str(total),
            "currency": snap["currency"],
            "issued_at": utcnow().isoformat(),
            "source_label": RECEIPT_LABEL,
        }
