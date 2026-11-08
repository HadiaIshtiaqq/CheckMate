"""Agent 10 - Monitoring. Detects contract expiry and offer changes (price,
seller, availability, key terms) for an active contract."""
from __future__ import annotations

from datetime import datetime

from app.agents.base import AgentOutput, BaseAgent
from app.core.util import money
from app.schemas.common import EvidenceRef
from app.schemas.domain import ContractTerms, MonitoringReport


class MonitoringAgent(BaseAgent):
    agent_id = "monitoring_agent"
    allowed_tools = frozenset({"catalog.product_details", "merchant.terms"})

    def run(self, *, contract: ContractTerms, contract_id: str, now: datetime) -> AgentOutput:
        changes: list[str] = []
        expired = now >= contract.expires_at
        if contract.purchase_channel == "external_store":
            # Re-searching costs a paid API call; the live price is confirmed by the shopper at the store.
            report = MonitoringReport(
                contract_id=contract_id, checked_at=now, expired=expired, offer_available=True,
                changes=["Contract has expired."] if expired else [], material_change=False,
                current_price=contract.unit_price,
                note=f"Price as listed on Google Shopping at {contract.price_as_of}; confirm the live price at "
                     f"{contract.store_name}.")
            return AgentOutput(value=report, evidence=[EvidenceRef(source="Google Shopping listing",
                                                                   reference=contract.source_reference,
                                                                   description="Listing snapshot")])
        if expired:
            changes.append("Contract has expired.")
        details = self.tools.call("catalog.product_details", contract.product_id)
        price = None
        if not details:
            changes.append("Offer is no longer available.")
        else:
            price = money(details["price"])
            if price != contract.unit_price:
                changes.append(f"Price changed from {contract.unit_price} to {price}.")
            if details["seller_id"] != contract.seller_id:
                changes.append("Seller changed.")
            if contract.exact_variant and contract.exact_variant not in details["variants"]:
                changes.append(f"Variant {contract.exact_variant} no longer offered.")
            terms = (self.tools.call("merchant.terms", contract.product_id) or {}).get("structured", {})
            w = terms.get("warranty_months")
            if contract.warranty_min_months and w is not None and w < contract.warranty_min_months:
                changes.append(f"Warranty reduced to {w} months.")
        report = MonitoringReport(contract_id=contract_id, checked_at=now, expired=expired,
                                  offer_available=bool(details), changes=changes,
                                  material_change=bool(changes) and not (expired and len(changes) == 1),
                                  current_price=price)
        return AgentOutput(value=report, evidence=[EvidenceRef(source="demo-merchant catalogue",
                                                               reference=contract.source_reference,
                                                               description="Current offer")])
