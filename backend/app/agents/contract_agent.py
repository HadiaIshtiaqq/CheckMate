"""Agent 6 - Purchase Contract. Combines intent, seller evidence, terms and cost
into an enforceable, hashed contract. It has no external tools."""
from __future__ import annotations

from datetime import datetime

from app.agents.base import AgentOutput, BaseAgent
from app.schemas.common import EvidenceRef
from app.schemas.domain import CandidateEvaluation, ContractTerms, ShoppingIntent


def build_contract_terms(intent: ShoppingIntent, ev: CandidateEvaluation, workflow_id: str,
                         expires_at: datetime, unit_price=None) -> ContractTerms:
    c = ev.candidate
    price = unit_price if unit_price is not None else c.price
    expected = ev.cost.total if ev.cost and ev.cost.is_complete else None
    if unit_price is not None and expected is not None:
        expected = expected + (unit_price - c.price) * intent.quantity
    return ContractTerms(
        workflow_id=workflow_id,
        intent_id=intent.id,
        product_id=c.id,
        product_name=c.product_name,
        brand=c.brand,
        model=c.model,
        exact_variant=c.variant,
        seller_id=c.seller_id,
        seller_name=ev.seller.seller_name if ev.seller and ev.seller.seller_name else c.listing_seller_name,
        quantity=intent.quantity,
        unit_price=price,
        expected_total=expected,
        max_total=intent.max_budget,
        currency=intent.currency,
        warranty_min_months=intent.warranty_requirement.min_months,
        warranty_required=intent.warranty_requirement.required,
        return_required=intent.return_requirement.required,
        free_returns_required=intent.return_requirement.free_returns,
        min_return_days=intent.return_requirement.min_days,
        subscription_allowed=intent.subscription_allowed,
        approved_addons=[],
        seller_min_evidence=intent.seller_preferences.min_evidence_level,
        max_delivery_days=intent.delivery_preferences.max_days,
        source_reference=c.product_url,
        expires_at=expires_at,
    )


def rank_eligible(evaluations: list[CandidateEvaluation]) -> list[CandidateEvaluation]:
    from app.schemas.common import Tri

    eligible = [e for e in evaluations if e.status == Tri.PASS and e.cost and e.cost.total is not None]
    return sorted(eligible, key=lambda e: (e.cost.total, -e.candidate.match_score))


class ContractAgent(BaseAgent):
    agent_id = "contract_agent"
    allowed_tools = frozenset()

    def run(self, *, intent: ShoppingIntent, evaluations: list[CandidateEvaluation], for_workflow: str,
            expires_at: datetime) -> AgentOutput:
        ranked = rank_eligible(evaluations)
        if not ranked:
            return AgentOutput(value=None, missing=["No candidate satisfied every mandatory condition."],
                               confidence=0.0)
        best = ranked[0]
        terms = build_contract_terms(intent, best, for_workflow, expires_at)
        return AgentOutput(
            value=terms,
            evidence=[EvidenceRef(source="policy engine", reference=f"evaluation:{best.candidate.id}",
                                  description="All mandatory candidate conditions PASS")],
            confidence=1.0,
        )
