"""Controlled demo merchant. It exposes the exact product, seller, add-ons, fees
and final total of a checkout, which makes checkout verification demonstrable
without pretending CHECKMATE can inspect arbitrary retailer websites."""
from __future__ import annotations

import copy
import threading
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from app.core.util import money, money_str, new_id, utcnow
from app.database.models import DemoMerchantCheckout
from app.integrations.merchants.base import OPERATIONS, MerchantAdapter
from app.integrations.merchants.catalog import PRODUCTS, SCENARIOS, SELLERS

SOURCE_LABEL = "Controlled demo merchant checkout API (simulated)"
RECEIPT_LABEL = "SIMULATED merchant receipt issued by the controlled demo merchant"

class DemoMerchantAdapter(MerchantAdapter):
    adapter_id = "demo-merchant"
    supported_operations = frozenset(OPERATIONS)

    def __init__(self, session_factory: sessionmaker):
        self._sessions = session_factory
        self._lock = threading.Lock()
        self._price_overrides: dict[str, str] = {}

    # ------------------------------------------------------------ catalogue
    def _product(self, product_id: str) -> dict | None:
        item = None
        for p in PRODUCTS:
            if p["id"] == product_id:
                item = copy.deepcopy(p)
                break
        if item and product_id in self._price_overrides:
            item["price"] = self._price_overrides[product_id]
        return item

    def search_products(self, query: str | None, category: str | None, **_: object) -> list[dict]:
        """Fictional demo catalogue only (enabled explicitly). Never invents products."""
        words = [w for w in (query or "").lower().split() if len(w) > 2]
        if category:
            hits = [p for p in PRODUCTS if p["category"] == category]
        else:
            hits = [p for p in PRODUCTS if p["brand"].lower() in words]
        return [self._listing(p["id"]) for p in hits]

    def _listing(self, product_id: str) -> dict:
        p = self._product(product_id)
        assert p is not None
        return {k: p.get(k) for k in (
            "id", "name", "brand", "model", "category", "variants", "price", "currency", "seller_id",
            "listing_seller_name", "specs", "description", "url",
        )} | {"demo": True}

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
