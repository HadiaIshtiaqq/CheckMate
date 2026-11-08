"""Agent 2 - Discovery. Searches the connected product sources (real stores via
Google Shopping; the fictional demo store only when explicitly enabled) and
ranks candidates against the intent. It never invents listings."""
from __future__ import annotations

from decimal import Decimal

from app.agents.base import AgentOutput, BaseAgent
from app.schemas.common import EvidenceRef
from app.schemas.domain import ProductCandidate, ShoppingIntent

NUMERIC_SPECS = {"ram_gb", "storage_gb", "battery_hours", "cups"}
SPEC_QUERY = {"ram_gb": "{}GB RAM", "storage_gb": "{}GB SSD", "noise_cancelling": "noise cancelling",
              "mechanical": "mechanical", "form": "{}"}


def spec_gaps(required: dict[str, str], offered: dict[str, str]) -> tuple[list[str], list[str]]:
    """(gaps, unverified): a stated value that contradicts a requirement is a gap (FAIL);
    a requirement the listing simply doesn't mention is unverified (UNKNOWN), never a pass."""
    gaps, unverified = [], []
    for key, want in required.items():
        have = offered.get(key)
        if have is None:
            unverified.append(f"{key.replace('_', ' ')} {want} not stated in the listing")
        elif key in NUMERIC_SPECS:
            try:
                if Decimal(have) < Decimal(want):
                    gaps.append(f"{key} is {have}, required at least {want}")
            except ArithmeticError:
                unverified.append(f"{key} value '{have}' could not be compared")
        elif have.lower() != want.lower():
            gaps.append(f"{key} is {have}, required {want}")
    return gaps, unverified


def search_query(intent: ShoppingIntent) -> str:
    parts = [intent.product_description or " ".join(intent.keywords)]
    for k, v in intent.specifications.items():
        tmpl = SPEC_QUERY.get(k)
        if tmpl and tmpl.format(v).lower() not in parts[0].lower():
            parts.append(tmpl.format(v))
    return " ".join(p for p in parts if p).strip()


class DiscoveryAgent(BaseAgent):
    agent_id = "discovery_agent"
    allowed_tools = frozenset({"catalog.search"})

    def run(self, *, intent: ShoppingIntent, exclude_product_ids: list[str] | None = None) -> AgentOutput:
        exclude = set(exclude_product_ids or [])
        listings = self.tools.call(
            "catalog.search", search_query(intent), intent.category,
            budget=str(intent.max_budget) if intent.max_budget is not None else None,
            currency=intent.currency, specs=intent.specifications, color=intent.preferred_variant)
        candidates: list[ProductCandidate] = []
        for l in listings:
            if l["id"] in exclude or l.get("currency") is None:
                continue
            gaps, unverified = spec_gaps(intent.specifications, l["specs"])
            variant = None
            if intent.preferred_variant:
                want = intent.preferred_variant.lower()
                match = [v for v in l["variants"] if v.lower() == want]
                if match:
                    variant = match[0]
                elif not l["variants"]:  # real listings rarely enumerate variants
                    if want in l["description"].lower():
                        variant = intent.preferred_variant
                    else:
                        unverified.append(f"colour {intent.preferred_variant} not stated in the listing")
                else:
                    gaps.append(f"variant {intent.preferred_variant} not offered ({', '.join(l['variants'])})")
            else:
                variant = l["variants"][0] if l["variants"] else None
            score = 100 - 30 * len(gaps) - 5 * len(unverified)
            if intent.preferred_brands and l["brand"] in intent.preferred_brands:
                score += 10
            if intent.max_budget is not None and Decimal(l["price"]) * intent.quantity > intent.max_budget:
                score -= 20
            external = bool(l.get("external"))
            source = l.get("source_label") or ("Demo store (fictional)" if l.get("demo") else "catalogue")
            candidates.append(ProductCandidate(
                id=l["id"], source=source, product_name=l["name"], brand=l["brand"], model=l["model"],
                category=l["category"], variant=variant, available_variants=l["variants"], price=l["price"],
                currency=l["currency"], seller_id=l["seller_id"], listing_seller_name=l["listing_seller_name"],
                product_url=l.get("url") or "", specifications=l["specs"], description=l["description"],
                match_score=max(score, 0), gaps=gaps, unverified_specs=unverified, external=external,
                demo=bool(l.get("demo")), thumbnail=l.get("thumbnail"), rating=l.get("rating"),
                reviews=l.get("reviews"), multiple_sources=bool(l.get("multiple_sources")),
                delivery_text=l.get("delivery_text"), retrieved_at=l.get("retrieved_at"),
                evidence_references=[EvidenceRef(source=source, reference=l.get("url") or l["id"],
                                                 description="Product listing (merchant-provided)",
                                                 untrusted_content=True)],
            ))
        candidates.sort(key=lambda c: (-c.match_score, c.price))
        missing = [] if candidates else [f"No listings found for '{intent.product_description}'."]
        return AgentOutput(value=candidates[:20], evidence=[e for c in candidates[:20] for e in c.evidence_references],
                           missing=missing, confidence=0.8 if candidates else 0.0)
