"""Agent 2 - Discovery. Searches the connected product sources (real stores via
Google Shopping; the fictional demo store only when explicitly enabled) and
ranks candidates against the intent. It never invents listings."""
from __future__ import annotations

import re
from decimal import Decimal

from app.agents.base import AgentOutput, BaseAgent
from app.integrations.merchants.serpapi_shopping import USED_CONDITION
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


DIGITAL_SOURCES = re.compile(r"\b(fab by epic games|turbosquid|cgtrader|sketchfab|unity asset store|creative market|"
                             r"envato|shutterstock|adobe stock|design bundles|etsy digital)\b", re.I)
DIGITAL_TITLE = re.compile(r"\b(3d model|3d asset|game asset|digital download|instant download|printable|svg|png file|"
                           r"pdf pattern|sewing pattern|template|clip ?art|wallpaper|mockup|stl file|vector)\b", re.I)
ACCESSORY_TITLE = re.compile(r"\b(replacement|keycaps?|key ?switch(?:es)?|switches|cushions?|slipcovers?|seat covers?|"
                             r"skins?|decals?|stickers?|screen protectors?|ear ?pads?|ear ?cushions?|ear ?tips?|"
                             r"wrist rest|repair kit|spare parts?|casters?|armrest pads?)\b", re.I)
TOY_TITLE = re.compile(r"\b(dollhouse|miniature|toy|doll|figurine|keychain|ornament|lego)\b", re.I)
HEAD_NOUNS = {
    "headphones": ["headphone", "headset", "earphone", "earbud", "ear bud", "airpods", "buds"],
    "laptop": ["laptop", "notebook", "chromebook", "macbook", "ultrabook"],
    "keyboard": ["keyboard"],
    "coffee maker": ["coffee maker", "coffeemaker", "coffee machine", "brewer", "espresso"],
}


def _singular(w: str) -> str:
    return w[:-1] if w.endswith("s") and not w.endswith("ss") else w


def relevance_issues(intent: ShoppingIntent, listing: dict) -> list[str]:
    """Why a listing is probably NOT the product asked for (digital good, accessory, toy, other item)."""
    title = listing["name"].lower()
    store = (listing.get("listing_seller_name") or "").lower()
    issues = []
    if DIGITAL_SOURCES.search(store) or DIGITAL_TITLE.search(title):
        issues.append("looks like a digital file or 3D asset, not the physical product")
    wanted = (intent.product_description or "").lower()
    if not ACCESSORY_TITLE.search(wanted) and ACCESSORY_TITLE.search(title):
        issues.append(f"looks like an accessory or part ('{ACCESSORY_TITLE.search(title).group(0)}'), not the product")
    if not TOY_TITLE.search(wanted) and TOY_TITLE.search(title):
        issues.append("looks like a toy or miniature")
    nouns = HEAD_NOUNS.get(intent.category or "", None)
    if nouns is None:
        words = [w for w in re.findall(r"[a-z]+", wanted) if len(w) > 2]
        nouns = [_singular(words[-1])] if words else []
    if nouns and not any(n in title for n in nouns):
        issues.append(f"title doesn't mention {' / '.join(nouns[:3])}")
    cond = listing.get("condition") or ""
    if cond and USED_CONDITION.search(cond) and not USED_CONDITION.search(intent.raw_text or ""):
        issues.append(f"condition is '{cond}', not new")
    return issues


COLOUR_WORDS = {"black", "white", "silver", "blue", "red", "graphite", "stainless", "grey", "gray", "green", "pink"}


def alternate_query(intent: ShoppingIntent) -> str | None:
    """A second, simpler phrasing for 'wide' searches (drops colours and extra adjectives)."""
    words = [w for w in re.findall(r"[a-z0-9-]+", (intent.product_description or "").lower())
             if w not in COLOUR_WORDS and len(w) > 2]
    if len(words) < 2:
        return None
    return " ".join(words[-2:])


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
            currency=intent.currency, specs=intent.specifications, color=intent.preferred_variant,
            market=intent.market, depth=intent.search_depth, alt_query=alternate_query(intent))
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
            issues = relevance_issues(intent, l) if external else []
            score -= 50 * len(issues)
            source = l.get("source_label") or ("Demo store (fictional)" if l.get("demo") else "catalogue")
            candidates.append(ProductCandidate(
                id=l["id"], source=source, product_name=l["name"], brand=l["brand"], model=l["model"],
                category=l["category"], variant=variant, available_variants=l["variants"], price=l["price"],
                currency=l["currency"], seller_id=l["seller_id"], listing_seller_name=l["listing_seller_name"],
                product_url=l.get("url") or "", specifications=l["specs"], description=l["description"],
                match_score=max(score, 0), gaps=gaps, unverified_specs=unverified, external=external,
                relevance_issues=issues, engine=l.get("engine"), condition=l.get("condition"),
                sponsored=bool(l.get("sponsored")),
                demo=bool(l.get("demo")), thumbnail=l.get("thumbnail"), rating=l.get("rating"),
                reviews=l.get("reviews"), multiple_sources=bool(l.get("multiple_sources")),
                delivery_text=l.get("delivery_text"), retrieved_at=l.get("retrieved_at"),
                evidence_references=[EvidenceRef(source=source, reference=l.get("url") or l["id"],
                                                 description="Product listing (merchant-provided)",
                                                 untrusted_content=True)],
            ))
        candidates.sort(key=lambda c: (-c.match_score, c.price))
        missing = [] if candidates else [f"No listings found for '{intent.product_description}'."]
        return AgentOutput(value=candidates[:40], evidence=[e for c in candidates[:40] for e in c.evidence_references],
                           missing=missing, confidence=0.8 if candidates else 0.0)
