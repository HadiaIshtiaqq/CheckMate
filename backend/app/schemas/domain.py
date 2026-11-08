"""Typed objects exchanged between agents."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field

from app.core.util import money
from app.schemas.common import (
    Charge,
    ConditionResult,
    EvidenceLevel,
    EvidenceRef,
    Tri,
)


Money = Annotated[Decimal, BeforeValidator(money)]


# ---------------------------------------------------------------- intent ----
class SellerPreferences(BaseModel):
    min_evidence_level: EvidenceLevel = EvidenceLevel.LIMITED
    allowed_seller_ids: list[str] = Field(default_factory=list)
    excluded_seller_ids: list[str] = Field(default_factory=list)


class WarrantyRequirement(BaseModel):
    required: bool = False
    min_months: int | None = None


class ReturnRequirement(BaseModel):
    required: bool = False
    free_returns: bool = False
    min_days: int | None = None


class DeliveryPreferences(BaseModel):
    max_days: int | None = None
    free_shipping_preferred: bool = False


class ShoppingIntent(BaseModel):
    id: str
    user_id: str
    raw_text: str
    category: str | None = None
    product_description: str
    keywords: list[str] = Field(default_factory=list)
    specifications: dict[str, str] = Field(default_factory=dict)
    preferred_brands: list[str] = Field(default_factory=list)
    preferred_variant: str | None = None
    max_budget: Money | None = None
    currency: str | None = None
    quantity: int = 1
    seller_preferences: SellerPreferences = Field(default_factory=SellerPreferences)
    warranty_requirement: WarrantyRequirement = Field(default_factory=WarrantyRequirement)
    return_requirement: ReturnRequirement = Field(default_factory=ReturnRequirement)
    subscription_allowed: bool = False
    delivery_preferences: DeliveryPreferences = Field(default_factory=DeliveryPreferences)
    conflicts: list[str] = Field(default_factory=list)
    clarifications_needed: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    parser: str = "rules"

    @property
    def needs_clarification(self) -> bool:
        return bool(self.conflicts or self.clarifications_needed)


# --------------------------------------------------------------- product ----
class ProductCandidate(BaseModel):
    id: str
    source: str
    product_name: str
    brand: str
    model: str
    category: str
    variant: str | None
    available_variants: list[str]
    price: Money
    currency: str
    seller_id: str
    listing_seller_name: str
    product_url: str
    specifications: dict[str, str]
    description: str  # merchant-provided, UNTRUSTED
    match_score: int
    gaps: list[str] = Field(default_factory=list)
    simulated: bool = False
    evidence_references: list[EvidenceRef] = Field(default_factory=list)


# ---------------------------------------------------------------- seller ----
class SellerSignal(BaseModel):
    name: str
    value: str | None
    status: str  # PRESENT | MISSING
    source: str


class SellerAssessment(BaseModel):
    seller_id: str
    seller_name: str | None
    marketplace: str | None
    evidence_level: EvidenceLevel
    signals: list[SellerSignal]
    listing_consistent: Tri
    missing_evidence: list[str]
    summary: str
    disclaimer: str = (
        "This is an assessment of available evidence, not a guarantee that the seller is trustworthy."
    )


# ----------------------------------------------------------------- terms ----
class PurchaseTerms(BaseModel):
    product_id: str
    warranty_months: int | None = None
    return_window_days: int | None = None
    free_returns: bool | None = None
    restocking_fee_pct: Money | None = None
    refund_conditions: str | None = None
    shipping_cost: Money | None = None
    delivery_days: int | None = None
    subscription_required: bool | None = None
    recurring_charges: list[Charge] = Field(default_factory=list)
    additional_fees: list[Charge] = Field(default_factory=list)
    cancellation_conditions: str | None = None
    field_sources: dict[str, str] = Field(default_factory=dict)
    unknown_fields: list[str] = Field(default_factory=list)
    inconsistencies: list[str] = Field(default_factory=list)
    injection_flags: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ cost ----
class CostAssessment(BaseModel):
    product_id: str
    currency: str
    lines: list[Charge]
    total: Money | None  # only set when every line is known
    known_subtotal: Money | None
    is_complete: bool
    includes_estimates: bool
    unknown_items: list[str]
    estimated_items: list[str]
    recurring_items: list[str]
    error: str | None = None


# ------------------------------------------------------------ evaluation ----
class CandidateEvaluation(BaseModel):
    candidate: ProductCandidate
    seller: SellerAssessment | None
    terms: PurchaseTerms | None
    cost: CostAssessment | None
    conditions: list[ConditionResult]
    status: Tri
    agent_errors: list[str] = Field(default_factory=list)


# -------------------------------------------------------------- contract ----
class ContractTerms(BaseModel):
    """Every field here is material: any change produces a new version and hash."""

    workflow_id: str
    intent_id: str
    product_id: str
    product_name: str
    brand: str
    model: str
    exact_variant: str | None
    seller_id: str
    seller_name: str
    quantity: int
    unit_price: Money
    expected_total: Money | None
    max_total: Money
    currency: str
    warranty_min_months: int | None
    warranty_required: bool
    return_required: bool
    free_returns_required: bool
    min_return_days: int | None
    subscription_allowed: bool
    approved_addons: list[str]
    seller_min_evidence: EvidenceLevel
    max_delivery_days: int | None
    source_reference: str
    expires_at: datetime


# -------------------------------------------------------------- checkout ----
class CheckoutTerms(BaseModel):
    warranty_months: int | None = None
    return_window_days: int | None = None
    free_returns: bool | None = None


class CheckoutSnapshot(BaseModel):
    checkout_id: str
    merchant_id: str
    product_id: str
    product_name: str
    variant: str | None
    seller_id: str
    seller_name: str
    quantity: int
    currency: str
    line_items: list[Charge]
    stated_total: Money | None
    terms: CheckoutTerms
    captured_at: datetime
    source_label: str


class CheckoutVerificationResult(BaseModel):
    contract_id: str
    contract_version: int
    contract_hash: str
    checkout_id: str
    conditions: list[ConditionResult]
    product_match: Tri
    seller_match: Tri
    total_match: Tri
    currency_match: Tri
    terms_match: Tri
    status: Tri
    reasons: list[str]
    explanation: str


# -------------------------------------------------------- reconciliation ----
class Discrepancy(BaseModel):
    code: str
    severity: str  # HIGH | MEDIUM | LOW
    description: str
    sources: list[str]


class ReconciliationReport(BaseModel):
    status: str  # RECONCILED | DISCREPANCY | INCOMPLETE
    checks: list[ConditionResult]
    discrepancies: list[Discrepancy]
    paypal_record: dict[str, Any]
    receipt: dict[str, Any] | None
    notes: list[str] = Field(default_factory=list)


class MonitoringReport(BaseModel):
    contract_id: str
    checked_at: datetime
    expired: bool
    offer_available: bool
    changes: list[str]
    material_change: bool
    current_price: Money | None = None
