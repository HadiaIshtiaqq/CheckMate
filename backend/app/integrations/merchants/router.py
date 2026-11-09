"""Routes catalogue operations to the configured product sources:
real stores via Google Shopping (ids 'ext-*') and, only when explicitly enabled,
the fictional demo store used for tests and the PayPal checkout demonstration."""
from __future__ import annotations

from app.integrations.merchants.base import UnsupportedOperation
from app.integrations.merchants.demo_merchant import DemoMerchantAdapter
from app.integrations.merchants.serpapi_shopping import ProductSourceError, SerpApiShoppingAdapter

NO_SOURCE = ("No product source is connected. Add a SerpAPI key (SERPAPI_KEY in .env) to search real stores "
             "through Google Shopping.")


class CatalogRouter:
    def __init__(self, demo: DemoMerchantAdapter | None, external: SerpApiShoppingAdapter | None):
        self.demo = demo
        self.external = external

    def sources(self) -> list[str]:
        out = []
        if self.external:
            out.append("Google Shopping, Amazon, eBay and Walmart (real stores, via SerpAPI)")
        if self.demo:
            out.append("Demo store (fictional, for testing)")
        return out

    def _for(self, product_id: str):
        if product_id.startswith("ext-"):
            return self.external
        return self.demo

    def search(self, query: str | None, category: str | None, budget: str | None = None,
               currency: str | None = None, specs: dict | None = None, color: str | None = None,
               market: str | None = None, depth: str = "standard", alt_query: str | None = None,
               source: str | None = None) -> list[dict]:
        """source: 'stores' = real stores only, 'partner' = PayPal partner merchant only, None = all."""
        if not self.external and not self.demo:
            raise ProductSourceError(NO_SOURCE)
        results: list[dict] = []
        errors: list[str] = []
        if self.external and source != "partner":
            try:
                results += self.external.search_products(query, category, budget=budget, currency=currency,
                                                         specs=specs, color=color, market=market, depth=depth,
                                                         alt_query=alt_query)
            except ProductSourceError as e:
                errors.append(str(e))
        if self.demo and source != "stores":
            results += self.demo.search_products(query, category)
        if not results and errors:
            raise ProductSourceError(errors[0])
        return results

    def source_available(self, product_id: str) -> bool:
        return self._for(product_id) is not None

    def product_details(self, product_id: str) -> dict | None:
        a = self._for(product_id)
        return a.get_product_details(product_id) if a else None

    def seller_info(self, seller_id: str) -> dict | None:
        if seller_id.startswith(("store:", "ebay:")):
            return self.external.get_seller_information(seller_id) if self.external else None
        return self.demo.get_seller_information(seller_id) if self.demo else None

    def terms(self, product_id: str) -> dict | None:
        a = self._for(product_id)
        return a.get_purchase_terms(product_id) if a else None

    def quote_tax(self, product_id: str, variant: str | None, quantity: int) -> dict:
        a = self._for(product_id)
        return a.quote_tax(product_id, variant, quantity) if a else {"amount": None, "status": "UNKNOWN"}

    def create_checkout(self, product_id: str, variant: str | None, quantity: int, scenario: str) -> dict:
        if product_id.startswith("ext-") or not self.demo:
            raise UnsupportedOperation("Real stores' checkouts can't be inspected; confirm the order at the store.")
        return self.demo.create_checkout(product_id, variant, quantity, scenario)

    def receipt(self, checkout_id: str) -> dict | None:
        return self.demo.get_receipt(checkout_id) if self.demo else None
