"""Agent 5 - Total Cost Auditor. Builds the cost lines; arithmetic is done by the
deterministic policies.cost module."""
from __future__ import annotations

from app.agents.base import AgentOutput, BaseAgent
from app.policies.cost import assess_cost
from app.schemas.common import Charge, ChargeKind, ChargeStatus, EvidenceRef
from app.schemas.domain import ProductCandidate, PurchaseTerms


class CostAuditorAgent(BaseAgent):
    agent_id = "cost_auditor"
    allowed_tools = frozenset({"merchant.quote_tax"})

    def run(self, *, candidate: ProductCandidate, terms: PurchaseTerms | None, quantity: int) -> AgentOutput:
        cur = candidate.currency
        lines = [Charge(label=f"{candidate.product_name} x{quantity}", kind=ChargeKind.PRODUCT,
                        amount=candidate.price * quantity, currency=cur, status=ChargeStatus.CONFIRMED,
                        sku=candidate.id, quantity=quantity)]
        ship = terms.shipping_cost if terms else None
        lines.append(Charge(label="Shipping", kind=ChargeKind.SHIPPING, amount=ship, currency=cur,
                            status=ChargeStatus.CONFIRMED if ship is not None else ChargeStatus.UNKNOWN))
        tax = self.tools.call("merchant.quote_tax", candidate.id, candidate.variant, quantity)
        lines.append(Charge(label="Sales tax", kind=ChargeKind.TAX, amount=tax.get("amount"), currency=cur,
                            status=ChargeStatus(tax.get("status", "UNKNOWN"))))
        if terms:
            lines.extend(terms.additional_fees)
            lines.extend(terms.recurring_charges)
        cost = assess_cost(candidate.id, cur, lines)
        return AgentOutput(
            value=cost,
            evidence=[EvidenceRef(source="demo-merchant tax quote", reference=f"{candidate.product_url}#tax",
                                  description=f"Tax quote ({tax.get('status')})")],
            missing=cost.unknown_items, confidence=1.0 if cost.is_complete else 0.5,
        )
