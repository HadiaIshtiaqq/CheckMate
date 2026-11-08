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
from app.integrations.paypal.client import MockPayPalClient, PayPalGateway, PayPalSandboxClient


class Container:
    def __init__(self) -> None:
        s = get_settings()
        self.settings = s
        self.merchant = DemoMerchantAdapter(SessionLocal)
        self.llm = build_llm(s)
        if s.paypal_mode == "sandbox":
            self.paypal: PayPalGateway = PayPalSandboxClient(s.paypal_client_id, s.paypal_client_secret,
                                                             s.paypal_webhook_id)
        else:
            self.paypal = MockPayPalClient(SessionLocal, s.backend_public_url, s.mock_webhook_secret)
        m, p = self.merchant, self.paypal
        self.registry = ToolRegistry({
            "llm.structured": self.llm.structured,
            "catalog.search": m.search_products,
            "catalog.product_details": m.get_product_details,
            "merchant.seller_info": m.get_seller_information,
            "merchant.terms": m.get_purchase_terms,
            "merchant.quote_tax": m.quote_tax,
            "merchant.create_checkout": m.create_checkout,
            "merchant.receipt": m.get_receipt,
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
