"""Agent 6 - Purchase Contract. Combines intent, seller evidence, terms and cost
into an enforceable, hashed contract. It has no external tools.

Two channels:
* demo_checkout  - the fictional demo store, whose checkout CHECKMATE can inspect and
                   pay through PayPal. Requires every mandatory condition to PASS.
* external_store - a real store found on Google Shopping. Its checkout can't be
                   inspected, so an offer qualifies when nothing FAILS; every condition
                   the listing couldn't prove becomes a checklist item the shopper
                   confirms on the store's page before buying."""
from __future__ import annotations

from datetime import datetime

from app.agents.base import AgentOutput, BaseAgent
from app.schemas.common import EvidenceRef, Tri
from app.schemas.domain import CandidateEvaluation, ContractTerms, ShoppingIntent


def checklist_for(ev: CandidateEvaluation, intent: ShoppingIntent) -> list[dict]:
    items = []
    for c in ev.conditions:
        if not c.mandatory or c.result != Tri.UNKNOWN:
            continue
        if c.key == "budget":
            items.append({"key": "budget", "label": "Final total",
                          "requirement": f"Final total at checkout, including tax and shipping, is at most "
                                         f"{intent.max_budget} {intent.currency}",
                          "why": c.reason, "auto": True})
        else:
            items.append({"key": c.key, "label": c.label, "requirement": c.approved or c.label, "why": c.reason})
    return items


def build_contract_terms(intent: ShoppingIntent, ev: CandidateEvaluation, workflow_id: str,
                         expires_at: datetime, unit_price=None, overrides: list[str] | None = None) -> ContractTerms:
    c = ev.candidate
    price = unit_price if unit_price is not None else c.price
    expected = ev.cost.total if ev.cost and ev.cost.is_complete else None
    if unit_price is not None and expected is not None:
        expected = expected + (unit_price - c.price) * intent.quantity
    external = c.external
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
        purchase_channel="external_store" if external else "demo_checkout",
        store_name=c.listing_seller_name if external else None,
        store_url=c.product_url if external else None,
        price_as_of=c.retrieved_at if external else None,
        known_subtotal=ev.cost.known_subtotal if ev.cost else None,
        checklist=checklist_for(ev, intent) if external else [],
        overridden_rules=list(overrides or []),
    )


def rank_eligible(evaluations: list[CandidateEvaluation]) -> list[CandidateEvaluation]:
    eligible = [e for e in evaluations if e.status == Tri.PASS and e.cost and e.cost.total is not None]
    return sorted(eligible, key=lambda e: (e.cost.total, -e.candidate.match_score))


def rank_store_plans(evaluations: list[CandidateEvaluation]) -> list[CandidateEvaluation]:
    """Real-store offers with no failed requirement, fewest unknowns first, then cheapest."""
    ok = [e for e in evaluations if e.candidate.external and e.status != Tri.FAIL and e.cost
          and e.cost.known_subtotal is not None]

    def unknowns(e: CandidateEvaluation) -> int:
        return sum(1 for c in e.conditions if c.mandatory and c.result == Tri.UNKNOWN)

    def trust(e: CandidateEvaluation) -> int:
        level = e.seller.evidence_level.value if e.seller else ""
        return {"STRONG_EVIDENCE": 0, "LIMITED_EVIDENCE": 1}.get(level, 2)

    def popularity(e: CandidateEvaluation) -> float:
        import math

        return (e.candidate.rating or 0) * math.log10(1 + (e.candidate.reviews or 0))

    return sorted(ok, key=lambda e: (trust(e), unknowns(e), -round(popularity(e), 1), e.cost.known_subtotal))


class ContractAgent(BaseAgent):
    agent_id = "contract_agent"
    allowed_tools = frozenset()

    def run(self, *, intent: ShoppingIntent, evaluations: list[CandidateEvaluation], for_workflow: str,
            expires_at: datetime) -> AgentOutput:
        ranked = rank_eligible(evaluations) or rank_store_plans(evaluations)
        if not ranked:
            return AgentOutput(value=None, missing=["No offer satisfied every mandatory condition."],
                               confidence=0.0)
        best = ranked[0]
        terms = build_contract_terms(intent, best, for_workflow, expires_at)
        note = ("All mandatory candidate conditions PASS" if best.status == Tri.PASS else
                f"No requirement fails; {len(terms.checklist)} item(s) to confirm at {terms.store_name}")
        return AgentOutput(
            value=terms,
            evidence=[EvidenceRef(source="policy engine", reference=f"evaluation:{best.candidate.id}", description=note)],
            confidence=1.0 if best.status == Tri.PASS else 0.6,
        )
