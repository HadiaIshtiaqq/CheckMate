"""Agent 2 - Discovery. Searches supported product sources and ranks candidates."""
from __future__ import annotations

from decimal import Decimal

from app.agents.base import AgentOutput, BaseAgent
from app.schemas.common import EvidenceRef
from app.schemas.domain import ProductCandidate, ShoppingIntent

NUMERIC_SPECS = {"ram_gb", "storage_gb", "battery_hours", "cups"}


def spec_gaps(required: dict[str, str], offered: dict[str, str]) -> list[str]:
    gaps = []
    for key, want in required.items():
        have = offered.get(key)
        if have is None:
            gaps.append(f"{key} not listed (required {want})")
        elif key in NUMERIC_SPECS:
            try:
                if Decimal(have) < Decimal(want):
                    gaps.append(f"{key} is {have}, required at least {want}")
            except ArithmeticError:
                gaps.append(f"{key} value '{have}' not comparable")
        elif have.lower() != want.lower():
            gaps.append(f"{key} is {have}, required {want}")
    return gaps


class DiscoveryAgent(BaseAgent):
    agent_id = "discovery_agent"
    allowed_tools = frozenset({"catalog.search"})

    def run(self, *, intent: ShoppingIntent, exclude_product_ids: list[str] | None = None) -> AgentOutput:
        exclude = set(exclude_product_ids or [])
        listings = self.tools.call(
            "catalog.search", " ".join(intent.keywords) or intent.product_description, intent.category,
            budget=str(intent.max_budget) if intent.max_budget is not None else None,
            currency=intent.currency, specs=intent.specifications)
        candidates: list[ProductCandidate] = []
        for l in listings:
            if l["id"] in exclude:
                continue
            gaps = spec_gaps(intent.specifications, l["specs"])
            variant = None
            if intent.preferred_variant:
                match = [v for v in l["variants"] if v.lower() == intent.preferred_variant.lower()]
                if match:
                    variant = match[0]
                else:
                    gaps.append(f"variant {intent.preferred_variant} not offered ({', '.join(l['variants'])})")
            else:
                variant = l["variants"][0] if l["variants"] else None
            score = 100 - 30 * len(gaps)
            if intent.preferred_brands and l["brand"] in intent.preferred_brands:
                score += 10
            if intent.max_budget is not None and Decimal(l["price"]) * intent.quantity > intent.max_budget:
                score -= 20
            candidates.append(ProductCandidate(
                id=l["id"], source="demo-merchant", product_name=l["name"], brand=l["brand"], model=l["model"],
                category=l["category"], variant=variant, available_variants=l["variants"], price=l["price"],
                currency=l["currency"], seller_id=l["seller_id"], listing_seller_name=l["listing_seller_name"],
                product_url=l["url"], specifications=l["specs"], description=l["description"],
                match_score=max(score, 0), gaps=gaps, simulated=bool(l.get("simulated")),
                evidence_references=[EvidenceRef(
                    source="demo-merchant (simulated listing)" if l.get("simulated") else "demo-merchant catalogue",
                    reference=l["url"], description="Product listing (merchant-provided)", untrusted_content=True)],
            ))
        candidates.sort(key=lambda c: (-c.match_score, c.price))
        missing = [] if candidates else [f"No products found in supported sources for '{intent.product_description}'."]
        return AgentOutput(value=candidates[:8], evidence=[e for c in candidates[:8] for e in c.evidence_references],
                           missing=missing, confidence=0.8 if candidates else 0.0)
