"""Standard merchant adapter interface (Architecture 4.7). Each adapter declares the
operations it actually supports; callers must not assume the rest exist."""
from __future__ import annotations

from abc import ABC
from typing import Any

OPERATIONS = (
    "search_products",
    "get_product_details",
    "get_seller_information",
    "get_purchase_terms",
    "quote_tax",
    "get_checkout_snapshot",
    "create_checkout",
    "get_order_status",
    "get_receipt",
)


class UnsupportedOperation(RuntimeError):
    pass


class MerchantAdapter(ABC):
    adapter_id: str = "abstract"
    supported_operations: frozenset[str] = frozenset()

    def supports(self, op: str) -> bool:
        return op in self.supported_operations

    def _unsupported(self, op: str) -> Any:
        raise UnsupportedOperation(f"{self.adapter_id} does not support {op}")

    def search_products(self, query: str | None, category: str | None) -> list[dict]:
        return self._unsupported("search_products")

    def get_product_details(self, product_id: str) -> dict | None:
        return self._unsupported("get_product_details")

    def get_seller_information(self, seller_id: str) -> dict | None:
        return self._unsupported("get_seller_information")

    def get_purchase_terms(self, product_id: str) -> dict | None:
        return self._unsupported("get_purchase_terms")

    def quote_tax(self, product_id: str, variant: str | None, quantity: int) -> dict:
        return self._unsupported("quote_tax")

    def create_checkout(self, product_id: str, variant: str | None, quantity: int, scenario: str) -> dict:
        return self._unsupported("create_checkout")

    def get_checkout_snapshot(self, checkout_id: str) -> dict | None:
        return self._unsupported("get_checkout_snapshot")

    def get_order_status(self, checkout_id: str) -> dict | None:
        return self._unsupported("get_order_status")

    def get_receipt(self, checkout_id: str) -> dict | None:
        return self._unsupported("get_receipt")


class Channel3Adapter(MerchantAdapter):
    """Placeholder for Channel3 product discovery. It declares no supported
    operations until a real integration is written against Channel3's API, so
    the orchestrator never silently relies on it."""

    adapter_id = "channel3"
    supported_operations = frozenset()
