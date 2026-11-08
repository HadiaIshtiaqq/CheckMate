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
        if "established_retailer" in info:
            return self._listing_store(candidate, info, src)
        if "feedback_score" in info:
            return self._marketplace_feedback(candidate, info, src)

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

    @staticmethod
    def _listing_store(candidate: ProductCandidate, info: dict, src: str) -> AgentOutput:
        """Real store from a shopping listing: the store name is the only evidence."""
        est, mkt = bool(info.get("established_retailer")), bool(info.get("marketplace_seller"))
        name = info.get("name") or ""
        signals = [
            SellerSignal(name="store_name", value=name or None, status="PRESENT" if name else "MISSING", source=src),
            SellerSignal(name="established_retailer", value="yes" if est else "no", status="PRESENT", source=src),
            SellerSignal(name="marketplace_third_party_seller", value="yes" if mkt else "no", status="PRESENT",
                         source=src),
            SellerSignal(name="identity_verified", value=None, status="MISSING", source=src),
            SellerSignal(name="seller_rating", value=None, status="MISSING", source=src),
            SellerSignal(name="dispute_rate_pct", value=None, status="MISSING", source=src),
        ]
        missing = [x.name for x in signals if x.status == "MISSING"]
        if est:
            level = EvidenceLevel.STRONG
            summary = (f"{name} is an established retailer (store name matches CHECKMATE's list). The listing "
                       f"carries no identity, seller-rating or dispute data.")
        elif name and name.lower() != "unknown store":
            level = EvidenceLevel.LIMITED
            summary = ((f"{name} looks like a marketplace or third-party seller. " if mkt else
                        f"{name} is not on CHECKMATE's list of established retailers. ")
                       + "The listing carries no identity, seller-rating or dispute data.")
        else:
            level = EvidenceLevel.INSUFFICIENT
            summary = "The listing doesn't name the store."
        consistent = Tri.PASS if candidate.listing_seller_name.casefold() == name.casefold() else Tri.UNKNOWN
        return AgentOutput(
            value=SellerAssessment(seller_id=candidate.seller_id, seller_name=name or None,
                                   marketplace=info.get("marketplace"), evidence_level=level, signals=signals,
                                   listing_consistent=consistent, missing_evidence=missing, summary=summary),
            evidence=[EvidenceRef(source=src, reference=candidate.product_url or candidate.id,
                                  description="Store named on the listing")],
            missing=missing, confidence=0.4)

    @staticmethod
    def _marketplace_feedback(candidate: ProductCandidate, info: dict, src: str) -> AgentOutput:
        """eBay seller: real feedback count and positive percentage from the listing."""
        n, pct = info.get("feedback_score"), info.get("positive_pct")
        signals = [
            SellerSignal(name="seller_feedback_count", value=None if n is None else str(n),
                         status="MISSING" if n is None else "PRESENT", source=src),
            SellerSignal(name="positive_feedback_pct", value=None if pct is None else str(pct),
                         status="MISSING" if pct is None else "PRESENT", source=src),
            SellerSignal(name="identity_verified", value=None, status="MISSING", source=src),
        ]
        missing = [x.name for x in signals if x.status == "MISSING"]
        if n is not None and pct is not None and n >= 1000 and float(pct) >= 98:
            level = EvidenceLevel.STRONG
        elif n is not None and pct is not None and n >= 50 and float(pct) >= 95:
            level = EvidenceLevel.LIMITED
        else:
            level = EvidenceLevel.INSUFFICIENT
        summary = (f"eBay seller {info.get('name', '').replace('eBay - ', '')}: "
                   + (f"{n:,} feedback ratings, {pct}% positive." if n is not None and pct is not None
                      else "no feedback data on the listing."))
        return AgentOutput(
            value=SellerAssessment(seller_id=candidate.seller_id, seller_name=info.get("name"),
                                   marketplace="eBay", evidence_level=level, signals=signals,
                                   listing_consistent=Tri.PASS, missing_evidence=missing, summary=summary),
            evidence=[EvidenceRef(source=src, reference=candidate.product_url or candidate.id,
                                  description="eBay seller feedback")],
            missing=missing, confidence=0.7 if level != EvidenceLevel.INSUFFICIENT else 0.3)
