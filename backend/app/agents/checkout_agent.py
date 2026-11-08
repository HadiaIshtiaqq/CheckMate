"""Agent 7 - Checkout Verification. Obtains a supported checkout snapshot and asks
the policy engine for the verdict. It cannot touch payments."""
from __future__ import annotations

from datetime import datetime

from app.agents.base import AgentOutput, BaseAgent
from app.policies import engine
from app.schemas.common import EvidenceRef, Tri, aggregate
from app.schemas.domain import CheckoutSnapshot, CheckoutVerificationResult, ContractTerms


def explain(status: Tri, conditions) -> str:
    failed = [c for c in conditions if c.mandatory and c.result == Tri.FAIL]
    unknown = [c for c in conditions if c.mandatory and c.result == Tri.UNKNOWN]
    if status == Tri.PASS:
        return "Every mandatory condition passed. The checkout matches the approved contract; payment is eligible."
    parts = []
    if failed:
        parts.append("Failed: " + "; ".join(f"{c.label} - {c.reason}" for c in failed))
    if unknown:
        parts.append("Could not verify: " + "; ".join(f"{c.label} - {c.reason}" for c in unknown))
    return f"PAYMENT BLOCKED ({status.value}). " + " ".join(parts)


class CheckoutAgent(BaseAgent):
    agent_id = "checkout_agent"
    allowed_tools = frozenset({"merchant.create_checkout"})

    def run(self, *, contract: ContractTerms, contract_id: str, version: int, contract_hash: str,
            scenario: str, now: datetime) -> AgentOutput:
        raw = self.tools.call("merchant.create_checkout", contract.product_id, contract.exact_variant,
                              contract.quantity, scenario)
        snapshot = CheckoutSnapshot.model_validate(raw)
        conditions = engine.evaluate_checkout(contract, snapshot, now)
        status = aggregate(conditions)
        summary = engine.summarise(conditions)
        result = CheckoutVerificationResult(
            contract_id=contract_id, contract_version=version, contract_hash=contract_hash,
            checkout_id=snapshot.checkout_id, conditions=conditions, status=status,
            reasons=[c.reason for c in conditions if c.mandatory and c.result != Tri.PASS],
            explanation=explain(status, conditions), **summary,
        )
        return AgentOutput(
            value={"snapshot": snapshot, "verification": result},
            evidence=[EvidenceRef(source=snapshot.source_label, reference=f"checkout:{snapshot.checkout_id}",
                                  description="Final checkout snapshot")],
            confidence=1.0,
        )
