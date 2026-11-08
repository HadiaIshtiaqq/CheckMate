"""Agent 1 - Intent Understanding. Deterministic rule parser, optionally enriched by
an LLM with structured outputs. Numeric and restrictive fields are cross-checked
against the user's own text so the LLM cannot invent a budget or loosen a rule."""
from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel

from app.agents.base import AgentOutput, BaseAgent
from app.core.util import money
from app.integrations.merchants.catalog import CATEGORY_SYNONYMS, KNOWN_BRANDS
from app.schemas.common import EvidenceLevel, EvidenceRef
from app.schemas.domain import (
    DeliveryPreferences,
    ReturnRequirement,
    SellerPreferences,
    ShoppingIntent,
    WarrantyRequirement,
)

NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}
CODES = {"usd": "USD", "dollars": "USD", "dollar": "USD", "eur": "EUR", "euro": "EUR", "euros": "EUR",
         "gbp": "GBP", "pounds": "GBP", "cad": "CAD", "aud": "AUD"}
WORDNUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "ten": 10,
           "twelve": 12, "eighteen": 18}
COLORS = ["black", "white", "silver", "blue", "red", "graphite", "stainless", "grey", "gray"]
NUMWORD = r"(\d+|a|an|one|two|three|four|five|six|ten|twelve|eighteen)"
UNIT_LOOKAHEAD = r"(?!\s*(?:-|\s)?(?:months?|years?|yrs?|days?|gb|tb|hours?|inch|in\b|%))"

CODES.update({"bucks": "USD", "buck": "USD", "quid": "GBP"})
AMT = (r"(?P<sym>[$€£])?\s*(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*(?P<k>k\b)?\s*"
       r"(?P<code>usd|eur|gbp|cad|aud|dollars?|bucks?|euros?|pounds|quid)?\b" + UNIT_LOOKAHEAD)
MAX_WORDS = (r"(?:under|below|less than|cheaper than|lower than|max(?:imum)?(?: of)?|up to|no more than|not more than|"
             r"not over|no higher than|at most|within|budget(?: of| is)?|cap(?:ped)? at|spend(?:ing)? (?:up to )?|<=?)")
APPROX_WORDS = r"(?:around|about|roughly|approximately|approx\.?|~)"
MAX_RE = re.compile(MAX_WORDS + r"\s*" + AMT, re.I)
APPROX_RE = re.compile(APPROX_WORDS + r"\s*" + AMT, re.I)
SUFFIX_RE = re.compile(AMT + r"\s*(?:budget|max(?:imum)?|or less|or under|tops|limit|at most|or cheaper)", re.I)
BETWEEN_RE = re.compile(r"between\s+([^.;]{1,30}?)\s+(?:and|to|-)\s+([^.;,]{1,30})", re.I)
AMT_RE = re.compile(AMT, re.I)


def _amount(m: re.Match) -> tuple[Decimal, str | None]:
    value = Decimal(m.group("num").replace(",", "")) * (1000 if m.group("k") else 1)
    sym, code = m.group("sym"), (m.group("code") or "").lower()
    return money(value), SYMBOLS.get(sym) if sym else CODES.get(code)


def extract_budget(t: str) -> tuple[Decimal | None, str | None, list[str]]:
    """Maximum total budget from many phrasings: 'under $110', '$110 max', 'up to 1.2k EUR',
    'between $50 and $100', 'around $100', '100 bucks', or a single price mentioned on its own."""
    notes: list[str] = []
    b = BETWEEN_RE.search(t)
    if b:
        hi, lo = AMT_RE.match(b.group(2).strip()), AMT_RE.search(b.group(1))
        if hi and hi.group("num"):
            value, cur = _amount(hi)
            if cur is None and lo:
                cur = _amount(lo)[1]
            notes.append(f"Used the top of your range ({value}) as the maximum total.")
            return value, cur, notes
    for rx in (MAX_RE, SUFFIX_RE):
        m = rx.search(t)
        if m:
            return (*_amount(m), notes)
    m = APPROX_RE.search(t)
    if m:
        value, cur = _amount(m)
        notes.append(f"Treated 'around {value}' as a strict maximum of {value}.")
        return value, cur, notes
    priced = [x for x in AMT_RE.finditer(t) if x.group("num") and (x.group("sym") or x.group("code"))]
    values = {_amount(x) for x in priced}
    if len(values) == 1:
        value, cur = values.pop()
        notes.append(f"Treated {value} as your maximum total.")
        return value, cur, notes
    return None, None, notes


MIN_PRICE_RE = re.compile(r"(?:over|above|more than|at least|minimum(?: of)?)\s*[$€£]\s*" + NUM, re.I)


LEAD_RE = re.compile(
    r"^(?:please\s+)?(?:(?:can|could)\s+you\s+)?(?:i\s+(?:want|need|would like)(?:\s+to\s+buy)?|find|buy|get|order|"
    r"purchase|look(?:ing)?\s+for|search\s+for|show\s+me|need|want)?\s*(?:me\s+)?"
    r"(?:(?:a|an|some|the|new|good|cheap|nice|best|decent)\s+)*", re.I)
STOP_RE = re.compile(
    r"\s*(?:\b(?:under|below|less|cheaper|lower|for|with|that|which|from|at most|at least|up to|no more|not more|"
    r"between|around|about|roughly|max|maximum|budget|within|costing|priced|must|should|and)\b|[$€£~]|\b\d).*$", re.I)
RULE_START_RE = re.compile(r"^(?:deliver|ship|must|it\s|should|needs?\b|warranty|returns?\b|no\s|free\s|from\s|"
                           r"and\s|come|they\s|with\s)", re.I)
FILLER = {"some", "good", "new", "nice", "cheap", "best", "the", "for", "and"}


def product_phrase(t: str) -> str:
    """The thing being bought: first clause that isn't a rule, minus verbs, articles and price talk."""
    for clause in re.split(r"[.,;!?\n]", t):
        c = LEAD_RE.sub("", clause.strip())
        c = STOP_RE.sub("", c).strip(" -:'\"")
        if len(c) >= 2 and not RULE_START_RE.match(c):
            return c[:60]
    return ""


def _n(tok: str) -> int | None:
    tok = tok.lower()
    return int(tok) if tok.isdigit() else WORDNUM.get(tok)


def _months(n: int, unit: str) -> int:
    return n * 12 if unit.lower().startswith(("y", "yr")) else n


def parse_rules(text: str, intent_id: str, user_id: str) -> ShoppingIntent:
    t = " ".join(text.split())
    low = t.lower()
    clar: list[str] = []
    conflicts: list[str] = []
    assumptions: list[str] = []

    # ---- budget + currency
    budget, currency, budget_notes = extract_budget(t)
    assumptions.extend(budget_notes)
    if budget is not None and currency is None:
        for c, v in CODES.items():
            if re.search(rf"\b{c}\b", low):
                currency = v
                break
    if budget is not None and budget <= 0:
        clar.append("The budget must be greater than zero.")
    if budget is None:
        clar.append("What is the maximum total amount you are willing to pay?")
    elif currency is None:
        clar.append(f"Which currency is the {budget} budget in (e.g. USD, EUR, GBP)?")
    mm = MIN_PRICE_RE.search(t)
    if mm and budget is not None and Decimal(mm.group(1).replace(",", "")) > budget:
        conflicts.append(f"Minimum price {mm.group(1)} is higher than the maximum budget {budget}.")

    # ---- category, keywords, brands, variant
    category = None
    for cat, words in CATEGORY_SYNONYMS.items():
        if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words):
            category = cat
            break
    brands = [b for b in KNOWN_BRANDS if re.search(rf"\b{re.escape(b.lower())}\b", low)]
    variant = next((c.capitalize() for c in COLORS if re.search(rf"\b{c}\b", low)), None)
    if variant in ("Gray",):
        variant = "Grey"

    description = product_phrase(t) or (category or "")
    keywords = [w for w in re.findall(r"[a-z0-9-]+", description.lower()) if len(w) > 2 and w not in FILLER]

    # ---- quantity
    qty = 1
    qm = re.search(r"\b(?:qty|quantity)[:\s]*(\d+)\b", low) or re.search(
        r"\b(?:find|buy|get|need|order|purchase)\s+(\d+|two|three|four|five|six|ten)\s+(?!gb|tb|months?|years?)", low)
    if qm:
        qty = _n(qm.group(1)) or 1
    if qty > 100:
        clar.append(f"Please confirm the quantity ({qty}); orders above 100 units need explicit confirmation.")
        qty = 100

    # ---- specifications
    specs: dict[str, str] = {}
    if (r := re.search(r"(\d+)\s*gb\s*(?:of\s*)?(?:ram|memory)", low)):
        specs["ram_gb"] = r.group(1)
    if (s := re.search(r"(\d+)\s*(gb|tb)\s*(?:of\s*)?(?:ssd|storage|hdd|drive)", low)):
        specs["storage_gb"] = str(int(s.group(1)) * (1000 if s.group(2) == "tb" else 1))
    if re.search(r"\bwireless\b|\bbluetooth\b", low):
        specs["wireless"] = "yes"
    if re.search(r"noise[\s-]*cancel+ing|\banc\b", low):
        specs["noise_cancelling"] = "yes"
    if re.search(r"\bmechanical\b", low):
        specs["mechanical"] = "yes"
    if re.search(r"over[\s-]ear", low):
        specs["form"] = "over-ear"

    # ---- warranty
    warranty = WarrantyRequirement()
    wm = re.search(NUMWORD + r"[\s-]*(years?|yrs?|months?)[\s-]+(?:\w+[\s-]+){0,2}warranty", low) or re.search(
        r"warranty\s+(?:of\s+)?(?:at least\s+|minimum(?: of)?\s+)?" + NUMWORD + r"[\s-]*(years?|yrs?|months?)", low)
    if wm and _n(wm.group(1)):
        warranty = WarrantyRequirement(required=True, min_months=_months(_n(wm.group(1)), wm.group(2)))
    elif re.search(r"\bwarrant(y|ied)\b", low) and not re.search(r"no warranty (needed|required)", low):
        warranty = WarrantyRequirement(required=True, min_months=None)
        assumptions.append("Warranty required; no minimum duration given, any disclosed warranty is accepted.")

    # ---- returns
    ret = ReturnRequirement()
    free = bool(re.search(r"free returns?|free return shipping|returns? (?:are |is )?free", low))
    rd = re.search(r"(\d+)[\s-]*days?\s+(?:free\s+)?returns?|returns?\s+(?:within|of|for)\s+(?:at least\s+)?(\d+)\s*days?",
                   low)
    if free or rd or re.search(r"\breturn(s|able| policy)\b", low):
        ret = ReturnRequirement(required=True, free_returns=free,
                                min_days=int(rd.group(1) or rd.group(2)) if rd else None)

    # ---- subscription
    no_sub = re.search(r"\bno (?:subscriptions?|recurring)|without (?:a |any )?subscriptions?|subscription[\s-]free|"
                       r"not? (?:want|need) (?:a |any )?subscriptions?|don'?t want (?:a |any )?subscriptions?", low)
    yes_sub = re.search(r"subscriptions? (?:is |are )?(?:ok|okay|fine|allowed|acceptable)|(?:with|include|including) "
                        r"(?:a )?subscription", low)
    if no_sub and yes_sub:
        conflicts.append("The request both prohibits and allows subscriptions.")
    sub_allowed = bool(yes_sub and not no_sub)
    if not no_sub and not yes_sub:
        assumptions.append("Subscriptions and recurring charges are not allowed unless you explicitly permit them.")

    # ---- seller
    seller_pref = SellerPreferences()
    if re.search(r"(reliable|trusted|reputable|trustworthy|established|verified)\s+(seller|merchant|store|vendor)", low):
        seller_pref = SellerPreferences(min_evidence_level=EvidenceLevel.STRONG)
    else:
        assumptions.append("Sellers with insufficient evidence are excluded by default.")

    # ---- delivery
    delivery = DeliveryPreferences(free_shipping_preferred=bool(re.search(r"free shipping", low)))
    dm = re.search(r"(?:deliver(?:ed|y)?|ship(?:ped)?|arrive[sd]?)\s+(?:with)?in\s+(\d+)\s*(?:business\s+)?days?", low)
    if dm:
        delivery.max_days = int(dm.group(1))

    if category is None and not keywords:
        clar.append("What product are you looking for?")

    return ShoppingIntent(
        id=intent_id, user_id=user_id, raw_text=text, category=category, product_description=description,
        keywords=keywords, specifications=specs, preferred_brands=brands, preferred_variant=variant,
        max_budget=budget, currency=currency, quantity=qty, seller_preferences=seller_pref,
        warranty_requirement=warranty, return_requirement=ret, subscription_allowed=sub_allowed,
        delivery_preferences=delivery, conflicts=conflicts, clarifications_needed=clar,
        assumptions=assumptions, parser="rules",
    )


# ------------------------------------------------------------------- LLM ----
class _LLMSpec(BaseModel):
    key: str
    value: str


class LLMIntent(BaseModel):
    category: str | None
    product_description: str
    specifications: list[_LLMSpec]
    preferred_brands: list[str]
    preferred_variant: str | None
    max_budget: float | None
    currency: str | None
    quantity: int | None
    warranty_min_months: int | None
    returns_required: bool
    free_returns_required: bool
    subscription_allowed: bool | None
    reliable_seller_required: bool
    max_delivery_days: int | None
    conflicts: list[str]


SYSTEM_PROMPT = (
    "You convert a shopper's request into structured purchase requirements. Extract only what the shopper "
    "stated. Use null when something is not stated. Do not infer a budget or currency that is not in the text. "
    "List contradictory instructions under conflicts."
)


def merge_llm(rules: ShoppingIntent, llm: LLMIntent) -> ShoppingIntent:
    """The LLM may add descriptive detail; deterministic rules keep authority over
    money, currency, and anything that would loosen a restriction."""
    low = rules.raw_text.lower()
    out = rules.model_copy(deep=True)
    out.parser = "llm+rules"
    if llm.product_description:
        out.product_description = llm.product_description[:200]
    if not out.category and llm.category:
        cat = llm.category.lower()
        for c, words in CATEGORY_SYNONYMS.items():
            if cat == c or cat in words:
                out.category = c
    for s in llm.specifications:
        key = re.sub(r"[^a-z0-9_]", "_", s.key.lower())[:40]
        if key not in out.specifications and s.value and s.value.lower() in low:
            out.specifications[key] = s.value
    out.preferred_brands = sorted(set(out.preferred_brands) | {b for b in llm.preferred_brands if b.lower() in low})
    if llm.max_budget is not None:
        llm_budget = money(llm.max_budget)
        if rules.max_budget is None:
            if re.search(rf"\b{re.escape(format(llm_budget.normalize(), 'f'))}\b", rules.raw_text.replace(",", "")):
                out.max_budget = llm_budget
                out.clarifications_needed = [c for c in out.clarifications_needed if "maximum total" not in c]
                if out.currency is None:
                    out.clarifications_needed.append(f"Which currency is the {llm_budget} budget in?")
        elif llm_budget != rules.max_budget:
            out.clarifications_needed.append(
                f"Please confirm your maximum budget: {rules.max_budget} or {llm_budget}?")
    if llm.warranty_min_months and (not out.warranty_requirement.min_months
                                    or llm.warranty_min_months > out.warranty_requirement.min_months):
        if "warrant" in low:
            out.warranty_requirement = WarrantyRequirement(required=True, min_months=llm.warranty_min_months)
    if llm.returns_required and not out.return_requirement.required:
        out.return_requirement.required = True
    if llm.free_returns_required and "free" in low:
        out.return_requirement.free_returns = True
    if llm.subscription_allowed is False:
        out.subscription_allowed = False  # only ever tightens
    if llm.reliable_seller_required:
        out.seller_preferences.min_evidence_level = EvidenceLevel.STRONG
    if llm.max_delivery_days and not out.delivery_preferences.max_days and str(llm.max_delivery_days) in low:
        out.delivery_preferences.max_days = llm.max_delivery_days
    out.conflicts = list(dict.fromkeys(out.conflicts + [c for c in llm.conflicts if c]))[:5]
    return out


class IntentAgent(BaseAgent):
    agent_id = "intent_agent"
    allowed_tools = frozenset({"llm.structured"})

    def run(self, *, text: str, intent_id: str, user_id: str) -> AgentOutput:
        intent = parse_rules(text, intent_id, user_id)
        llm = self.tools.call("llm.structured", SYSTEM_PROMPT, f"Shopper request:\n{text}", LLMIntent)
        if llm is not None:
            intent = merge_llm(intent, llm)
        missing = list(intent.clarifications_needed)
        return AgentOutput(
            value=intent,
            evidence=[EvidenceRef(source="user", reference=f"intent:{intent_id}", description="Original request text")],
            missing=missing,
            confidence=0.9 if not intent.needs_clarification else 0.5,
        )
