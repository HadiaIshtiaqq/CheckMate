"""Wires integrations into a tool registry and builds agents with scoped tools."""
from __future__ import annotations

from functools import lru_cache

from app.agents.base import ToolRegistry
from app.agents.checkout_agent import CheckoutAgent
from app.agents.contract_agent import ContractAgent
from app.agents.cost_auditor_agent import CostAuditorAgent
from app.agents.discovery_agent import DiscoveryAgent
from app.agents.intent_agent import IntentAgent
from app.agents.monitoring_agent import MonitoringAgent
from app.agents.payment_agent import PaymentAgent
from app.agents.reconciliation_agent import ReconciliationAgent
from app.agents.seller_trust_agent import SellerTrustAgent
from app.agents.terms_agent import TermsAgent
from app.core.config import get_settings
from app.database.session import SessionLocal
from app.integrations.llm.client import build_llm
from app.integrations.merchants.demo_merchant import DemoMerchantAdapter
from app.integrations.merchants.router import CatalogRouter
from app.integrations.merchants.serpapi_shopping import SerpApiShoppingAdapter
from app.integrations.paypal.client import MockPayPalClient, PayPalGateway, PayPalSandboxClient


class Container:
    def __init__(self) -> None:
        s = get_settings()
        self.settings = s
        # Real stores via Google Shopping when a SerpAPI key is set. The fictional demo store is only
        # used when explicitly enabled (tests, PayPal checkout demonstration).
        self.merchant = DemoMerchantAdapter(SessionLocal) if s.demo_catalog_enabled else None
        self.external = (SerpApiShoppingAdapter(s.serpapi_key, SessionLocal, cache_ttl=s.serpapi_cache_seconds,
                                                max_results=s.serpapi_max_results)
                         if s.serpapi_key else None)
        self.catalog = CatalogRouter(self.merchant, self.external)
        self.llm = build_llm(s)
        if s.paypal_mode == "sandbox":
            self.paypal: PayPalGateway = PayPalSandboxClient(s.paypal_client_id, s.paypal_client_secret,
                                                             s.paypal_webhook_id)
        else:
            self.paypal = MockPayPalClient(SessionLocal, s.backend_public_url, s.mock_webhook_secret)
        cat, p = self.catalog, self.paypal
        self.registry = ToolRegistry({
            "llm.structured": self.llm.structured,
            "catalog.search": cat.search,
            "catalog.product_details": cat.product_details,
            "catalog.source_available": cat.source_available,
            "merchant.seller_info": cat.seller_info,
            "merchant.terms": cat.terms,
            "merchant.quote_tax": cat.quote_tax,
            "merchant.create_checkout": cat.create_checkout,
            "merchant.receipt": cat.receipt,
            "paypal.create_order": p.create_order,
            "paypal.get_order": p.get_order,
            "paypal.capture_order": p.capture_order,
        })

    # agents are cheap; build per use so tool-call logs are per run
    def intent(self) -> IntentAgent: return IntentAgent(self.registry)
    def discovery(self) -> DiscoveryAgent: return DiscoveryAgent(self.registry)
    def seller(self) -> SellerTrustAgent: return SellerTrustAgent(self.registry)
    def terms(self) -> TermsAgent: return TermsAgent(self.registry)
    def cost(self) -> CostAuditorAgent: return CostAuditorAgent(self.registry)
    def contract(self) -> ContractAgent: return ContractAgent(self.registry)
    def checkout(self) -> CheckoutAgent: return CheckoutAgent(self.registry)
    def payment(self) -> PaymentAgent: return PaymentAgent(self.registry, self.settings.authorization_secret)
    def reconciliation(self) -> ReconciliationAgent: return ReconciliationAgent(self.registry)
    def monitoring(self) -> MonitoringAgent: return MonitoringAgent(self.registry)


@lru_cache
def get_container() -> Container:
    return Container()
