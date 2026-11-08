"""Agent 3 - Seller Trust. Reports available evidence and its gaps. It never
invents ratings and never claims a seller is guaranteed safe."""
from __future__ import annotations

from decimal import Decimal

from app.agents.base import AgentOutput, BaseAgent
from app.schemas.common import EvidenceLevel, EvidenceRef, Tri
from app.schemas.domain import ProductCandidate, SellerAssessment, SellerSignal


class SellerTrustAgent(BaseAgent):
    agent_id = "seller_trust_agent"
    allowed_tools = frozenset({"merchant.seller_info"})

    def run(self, *, candidate: ProductCandidate) -> AgentOutput:
        info = self.tools.call("merchant.seller_info", candidate.seller_id)
        if not info:
            return AgentOutput(
                value=SellerAssessment(
                    seller_id=candidate.seller_id, seller_name=None, marketplace=None,
                    evidence_level=EvidenceLevel.INSUFFICIENT, signals=[], listing_consistent=Tri.UNKNOWN,
                    missing_evidence=["seller registry record"], summary="No seller record found."),
                missing=["seller registry record"], confidence=0.2)
        src = info.get("source", "seller registry")

        def sig(name: str, value) -> SellerSignal:
            return SellerSignal(name=name, value=None if value is None else str(value),
                                status="MISSING" if value is None else "PRESENT", source=src)

        signals = [
            sig("identity_verified", info.get("identity_verified")),
            sig("business_registration", info.get("business_registration")),
            sig("years_active", info.get("years_active")),
            sig("rating", info.get("rating")),
            sig("rating_count", info.get("rating_count")),
            sig("dispute_rate_pct", info.get("dispute_rate_pct")),
            sig("return_policy_published", info.get("return_policy_published")),
        ]
        missing = [s.name for s in signals if s.status == "MISSING"]

        identity = info.get("identity_verified") is True
        count = info.get("rating_count") or 0
        years = info.get("years_active") or 0
        dispute = info.get("dispute_rate_pct")
        if identity and count >= 100 and years >= 2 and dispute is not None and Decimal(dispute) < Decimal("2"):
            level = EvidenceLevel.STRONG
        elif identity or count >= 20:
            level = EvidenceLevel.LIMITED
        else:
            level = EvidenceLevel.INSUFFICIENT

        consistent = Tri.PASS if candidate.listing_seller_name.casefold() == (info.get("name") or "").casefold() \
            else Tri.FAIL
        parts = []
        if identity:
            parts.append("identity verified by marketplace")
        if info.get("rating") is not None:
            parts.append(f"rating {info['rating']} from {count} reviews")
        if years:
            parts.append(f"{years} years active")
        if dispute is not None:
            parts.append(f"{dispute}% dispute rate")
        summary = (", ".join(parts) or "little verifiable information").capitalize() + \
            (f". Missing: {', '.join(missing)}." if missing else ".")
        return AgentOutput(
            value=SellerAssessment(seller_id=candidate.seller_id, seller_name=info.get("name"),
                                   marketplace=info.get("marketplace"), evidence_level=level, signals=signals,
                                   listing_consistent=consistent, missing_evidence=missing, summary=summary),
            evidence=[EvidenceRef(source="demo-merchant seller registry", reference=src,
                                  description="Seller registry record")],
            missing=missing, confidence=round(1 - len(missing) / len(signals), 2),
        )
