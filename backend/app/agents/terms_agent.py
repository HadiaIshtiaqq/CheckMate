"""Agent 4 - Terms Intelligence. Extracts purchase terms from structured merchant
data and free-text policies. Merchant text is untrusted data: it can only fill
typed fields, and instructions found inside it are flagged and ignored.
Anything not found stays UNKNOWN (None), never zero or 'pass'."""
from __future__ import annotations

import re

from pydantic import BaseModel

from app.agents.base import AgentOutput, BaseAgent
from app.core.util import money
from app.integrations.llm.client import wrap_untrusted
from app.schemas.common import Charge, ChargeKind, ChargeStatus, EvidenceRef
from app.schemas.domain import ProductCandidate, PurchaseTerms

INJECTION_PATTERNS = [
    r"ignore (?:all |any )?(?:previous|prior|above|earlier) instructions",
    r"\b(?:system|assistant|developer)\s*:",
    r"\byou are now\b",
    r"(?:set|change|raise|increase|update) (?:the )?(?:budget|max(?:imum)? total|spending limit|limit)",
    r"approve (?:the |this )?(?:purchase|payment|order|transaction)",
    r"(?:ai|llm) (?:shopping )?(?:agents?|assistants?)\b",
    r"treat this (?:listing|product) as (?:fully )?verified",
]

FIELDS = ["warranty_months", "return_window_days", "free_returns", "shipping_cost", "delivery_days",
          "subscription_required"]


RESTRICTIVE_BOOLS = {("subscription_required", True), ("free_returns", False)}


def _number_in_text(v, low: str) -> bool:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return re.search(rf"(?<![\d.]){re.escape(str(v))}(?![\d])", low) is not None


def detect_injection(*texts: str) -> list[str]:
    flags = []
    for t in texts:
        for p in INJECTION_PATTERNS:
            for m in re.finditer(p, t or "", re.I):
                flags.append(m.group(0))
    return list(dict.fromkeys(flags))


def extract_from_text(text: str) -> dict:
    low = (text or "").lower()
    out: dict = {}
    w = re.search(r"(\d+)[\s-]*(month|year)s?[\s-]+(?:[a-z]+[\s-]+){0,3}warranty", low) or \
        re.search(r"warranty[^.]{0,20}?(\d+)[\s-]*(month|year)s?", low)
    if w:
        out["warranty_months"] = int(w.group(1)) * (12 if w.group(2) == "year" else 1)
    elif re.search(r"\bno warranty\b", low):
        out["warranty_months"] = 0
    r = re.search(r"returns?\s+(?:accepted\s+)?within\s+(\d+)\s+days", low) or re.search(r"(\d+)[\s-]*day returns?", low)
    if r:
        out["return_window_days"] = int(r.group(1))
    elif re.search(r"no returns|final sale|non-returnable", low):
        out["return_window_days"] = 0
    if re.search(r"free\s+(?:\d+[\s-]*day\s+)?returns?", low):
        out["free_returns"] = True
    elif re.search(r"restocking fee|buyer pays return|return shipping is paid by the buyer", low):
        out["free_returns"] = False
    rf = re.search(r"(\d+(?:\.\d+)?)%\s*restocking fee", low)
    if rf:
        out["restocking_fee_pct"] = rf.group(1)
    s = re.search(r"shipping[^.$]{0,20}\$\s?(\d+(?:\.\d{1,2})?)", low)
    if s:
        out["shipping_cost"] = s.group(1)
    elif re.search(r"free shipping", low):
        out["shipping_cost"] = "0.00"
    d = re.search(r"(\d+)\s*(?:-|to)\s*(\d+)\s*business days", low) or re.search(r"within (\d+) business days", low)
    if d and re.search(r"deliver|ship", low):
        out["delivery_days"] = int(d.group(d.lastindex))
    if re.search(r"no subscription", low):
        out["subscription_required"] = False
    elif re.search(r"subscription[^.]{0,30}required|requires? (?:a |an )?[a-z ]{0,20}subscription", low):
        out["subscription_required"] = True
    m = re.search(r"refunds?[^.]{0,120}\.", text or "", re.I)
    if m:
        out["refund_conditions"] = m.group(0).strip()
    return out


class LLMTerms(BaseModel):
    warranty_months: int | None
    return_window_days: int | None
    free_returns: bool | None
    shipping_cost: float | None
    delivery_days: int | None
    subscription_required: bool | None
    embedded_instructions_detected: list[str]


class TermsAgent(BaseAgent):
    agent_id = "terms_agent"
    allowed_tools = frozenset({"merchant.terms", "llm.structured"})

    def run(self, *, candidate: ProductCandidate) -> AgentOutput:
        raw = self.tools.call("merchant.terms", candidate.id)
        if not raw:
            return AgentOutput(value=PurchaseTerms(product_id=candidate.id, unknown_fields=FIELDS),
                               missing=["purchase terms unavailable"], confidence=0.0)
        structured = raw.get("structured") or {}
        text = raw.get("policy_text") or ""
        parsed = extract_from_text(text)
        flags = detect_injection(text, candidate.description)

        values: dict = {}
        sources: dict[str, str] = {}
        inconsistencies: list[str] = []
        for f in FIELDS:
            s_val, t_val = structured.get(f), parsed.get(f)
            if f == "shipping_cost":
                s_val, t_val = money(s_val), money(t_val)
            if s_val is not None and t_val is not None and s_val != t_val:
                inconsistencies.append(f"{f}: structured data says {s_val}, policy text says {t_val}")
                values[f], sources[f] = None, "conflicting"
            elif s_val is not None:
                values[f], sources[f] = s_val, "structured"
            elif t_val is not None:
                values[f], sources[f] = t_val, "policy_text"

        # LLM only for fields still unknown; accepted only if the number appears in the text.
        unknown_now = [f for f in FIELDS if values.get(f) is None and sources.get(f) != "conflicting"]
        # Only worth an LLM call when there is substantial policy text (real listings carry a line or two).
        if unknown_now and len(text) >= 200 and self.tools.can("llm.structured"):
            llm = self.tools.call("llm.structured", "Extract purchase terms from this merchant policy.",
                                  wrap_untrusted(text), LLMTerms)
            if llm is not None:
                flags = list(dict.fromkeys(flags + llm.embedded_instructions_detected))
                low = text.lower()
                for f in unknown_now:
                    v = getattr(llm, f)
                    if v is None:
                        continue
                    if isinstance(v, bool):
                        # An LLM may never turn UNKNOWN into a permissive answer without textual proof:
                        # only the restrictive value is accepted (subscription required / returns not free).
                        if (f, v) not in RESTRICTIVE_BOOLS:
                            continue
                    elif not _number_in_text(v, low):
                        continue
                    values[f] = money(v) if f == "shipping_cost" else v
                    sources[f] = "llm (verified against text)"

        recurring = [Charge(label=rc["label"], kind=ChargeKind.SUBSCRIPTION, amount=rc.get("amount"),
                            currency=candidate.currency, status=ChargeStatus.CONFIRMED if rc.get("amount")
                            else ChargeStatus.UNKNOWN, recurring=True, interval=rc.get("interval"))
                     for rc in structured.get("recurring_charges", [])]
        fees = [Charge(label=fe["label"], kind=ChargeKind.FEE, amount=fe.get("amount"), currency=candidate.currency,
                       status=ChargeStatus.CONFIRMED if fe.get("amount") else ChargeStatus.UNKNOWN)
                for fe in structured.get("additional_fees", [])]
        unknown = [f for f in FIELDS if values.get(f) is None]
        terms = PurchaseTerms(
            product_id=candidate.id,
            warranty_months=values.get("warranty_months"),
            return_window_days=values.get("return_window_days"),
            free_returns=values.get("free_returns"),
            restocking_fee_pct=parsed.get("restocking_fee_pct"),
            refund_conditions=parsed.get("refund_conditions"),
            shipping_cost=values.get("shipping_cost"),
            delivery_days=values.get("delivery_days"),
            subscription_required=values.get("subscription_required"),
            recurring_charges=recurring,
            additional_fees=fees,
            field_sources=sources,
            unknown_fields=unknown,
            inconsistencies=inconsistencies,
            injection_flags=flags,
        )
        return AgentOutput(
            value=terms,
            evidence=[EvidenceRef(source="demo-merchant terms", reference=raw.get("source", candidate.product_url),
                                  description="Merchant terms and policy text", untrusted_content=True)],
            missing=unknown, confidence=round(1 - len(unknown) / len(FIELDS), 2),
        )
