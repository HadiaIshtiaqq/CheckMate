from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field

from app.core.util import money


class Tri(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class ChargeStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    ESTIMATED = "ESTIMATED"
    UNKNOWN = "UNKNOWN"


class ChargeKind(str, Enum):
    PRODUCT = "product"
    SHIPPING = "shipping"
    TAX = "tax"
    FEE = "fee"
    ADDON = "addon"
    SUBSCRIPTION = "subscription"
    DISCOUNT = "discount"


class EvidenceLevel(str, Enum):
    STRONG = "STRONG_EVIDENCE"
    LIMITED = "LIMITED_EVIDENCE"
    INSUFFICIENT = "INSUFFICIENT_EVIDENCE"


EVIDENCE_RANK = {EvidenceLevel.INSUFFICIENT: 0, EvidenceLevel.LIMITED: 1, EvidenceLevel.STRONG: 2}


class EvidenceRef(BaseModel):
    source: str
    reference: str
    description: str = ""
    untrusted_content: bool = False


class Charge(BaseModel):
    label: str
    kind: ChargeKind
    amount: Annotated[Decimal | None, BeforeValidator(money)]  # None == UNKNOWN; never zero
    currency: str
    status: ChargeStatus
    recurring: bool = False
    interval: str | None = None
    mandatory: bool = True
    preselected: bool = False
    sku: str | None = None
    quantity: int = 1


class ConditionResult(BaseModel):
    key: str
    label: str
    approved: str | None = None
    observed: str | None = None
    result: Tri
    reason: str
    mandatory: bool = True


def aggregate(conditions: list[ConditionResult]) -> Tri:
    """FAIL dominates, then UNKNOWN. Only an all-PASS set of mandatory conditions passes."""
    mandatory = [c for c in conditions if c.mandatory]
    if any(c.result == Tri.FAIL for c in mandatory):
        return Tri.FAIL
    if any(c.result == Tri.UNKNOWN for c in mandatory):
        return Tri.UNKNOWN
    if not mandatory:
        return Tri.UNKNOWN
    return Tri.PASS


class AgentEnvelope(BaseModel):
    """The standard result wrapper every agent emits (TRD 2.4)."""

    workflow_id: str
    agent_id: str
    timestamp: datetime
    status: str  # OK | ERROR
    subject: str | None = None
    result: Any = None
    evidence_references: list[EvidenceRef] = Field(default_factory=list)
    confidence: float | None = None
    missing_information: list[str] = Field(default_factory=list)
    error: str | None = None
