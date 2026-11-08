"""Deterministic policy engine. Agents may interpret information; this module alone
decides whether a candidate qualifies, whether a checkout passes, and whether a
payment may be authorised."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.core.util import canonical_json, constant_time_equals, hmac_hex, money, money_str
from app.policies.cost import line_sign
from app.schemas.common import (
    EVIDENCE_RANK,
    ChargeKind,
    ChargeStatus,
    ConditionResult,
    Tri,
    aggregate,
)
from app.schemas.domain import (
    CheckoutSnapshot,
    ContractTerms,
    CostAssessment,
    ProductCandidate,
    PurchaseTerms,
    SellerAssessment,
    ShoppingIntent,
)

P, F, U = Tri.PASS, Tri.FAIL, Tri.UNKNOWN


def _c(key, label, approved, observed, result, reason, mandatory=True) -> ConditionResult:
    return ConditionResult(key=key, label=label, approved=None if approved is None else str(approved),
                           observed=None if observed is None else str(observed), result=result,
                           reason=reason, mandatory=mandatory)


def _same(a: str | None, b: str | None) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


# ======================================================= candidate stage ====
def evaluate_candidate(
    intent: ShoppingIntent,
    cand: ProductCandidate,
    seller: SellerAssessment | None,
    terms: PurchaseTerms | None,
    cost: CostAssessment | None,
) -> list[ConditionResult]:
    out: list[ConditionResult] = []
    if cand.relevance_issues:
        out.append(_c("product_type", "Right product", intent.product_description, "; ".join(cand.relevance_issues), F,
                      "Probably not what you asked for: " + "; ".join(cand.relevance_issues) + "."))
    wanted = ", ".join(f"{k}={v}" for k, v in intent.specifications.items()) or "any"
    if intent.preferred_variant:
        wanted += f", colour={intent.preferred_variant}"
    if cand.gaps:
        out.append(_c("specifications", "Specifications", wanted, "; ".join(cand.gaps), F, "; ".join(cand.gaps)))
    elif cand.unverified_specs:
        out.append(_c("specifications", "Specifications", wanted, "not stated", U,
                      "Not stated in the listing: " + "; ".join(cand.unverified_specs)))
    else:
        out.append(_c("specifications", "Specifications", wanted, "all matched", P,
                      "Matches the requested specifications."))

    # seller evidence
    req = intent.seller_preferences.min_evidence_level
    if seller is None:
        out.append(_c("seller", "Seller evidence", req.value, None, U, "Seller assessment unavailable."))
    elif cand.seller_id in intent.seller_preferences.excluded_seller_ids:
        out.append(_c("seller", "Seller evidence", req.value, seller.seller_name, F, "Seller excluded by user."))
    elif intent.seller_preferences.allowed_seller_ids and cand.seller_id not in intent.seller_preferences.allowed_seller_ids:
        out.append(_c("seller", "Seller evidence", req.value, seller.seller_name, F, "Seller not in allowed list."))
    else:
        ok = EVIDENCE_RANK[seller.evidence_level] >= EVIDENCE_RANK[req]
        out.append(_c("seller", "Seller evidence", f">= {req.value}", seller.evidence_level.value, P if ok else F,
                      seller.summary if ok else f"Requires {req.value}; available evidence is {seller.evidence_level.value}."))

    # warranty
    w = intent.warranty_requirement
    if w.required or w.min_months:
        need = w.min_months or 1
        have = terms.warranty_months if terms else None
        if have is None:
            out.append(_c("warranty", "Warranty", f">= {need} months", "UNKNOWN", U, "Warranty not disclosed."))
        else:
            out.append(_c("warranty", "Warranty", f">= {need} months", f"{have} months", P if have >= need else F,
                          "Meets warranty requirement." if have >= need else f"Warranty is only {have} months."))

    # returns
    r = intent.return_requirement
    if r.required or r.free_returns or r.min_days:
        days = terms.return_window_days if terms else None
        free = terms.free_returns if terms else None
        approved = ("free " if r.free_returns else "") + "returns" + (f" >= {r.min_days} days" if r.min_days else "")
        observed = f"{days if days is not None else 'UNKNOWN'} days, free={free if free is not None else 'UNKNOWN'}"
        if days is None or (r.free_returns and free is None):
            res, why = U, "Return terms not fully disclosed."
        elif r.min_days and days < r.min_days:
            res, why = F, f"Return window {days} days is shorter than {r.min_days}."
        elif r.free_returns and not free:
            res, why = F, "Returns are not free."
        elif days <= 0:
            res, why = F, "Returns not accepted."
        else:
            res, why = P, "Return terms satisfied."
        out.append(_c("returns", "Returns", approved, observed, res, why))

    # subscription
    if not intent.subscription_allowed:
        sub = terms.subscription_required if terms else None
        recurring = (terms.recurring_charges if terms else []) + [
            l for l in (cost.lines if cost else []) if l.recurring]
        if sub or recurring:
            out.append(_c("subscription", "Subscription", "not allowed", "required", F,
                          "Offer requires a subscription or recurring charge."))
        elif sub is None:
            out.append(_c("subscription", "Subscription", "not allowed", "UNKNOWN", U,
                          "Subscription requirements not disclosed."))
        else:
            out.append(_c("subscription", "Subscription", "not allowed", "none", P, "No subscription required."))

    # delivery
    if intent.delivery_preferences.max_days:
        d = terms.delivery_days if terms else None
        lim = intent.delivery_preferences.max_days
        out.append(_c("delivery", "Delivery", f"<= {lim} days", d if d is not None else "UNKNOWN",
                      U if d is None else (P if d <= lim else F),
                      "Delivery time not disclosed." if d is None else ("Delivery within limit." if d <= lim
                                                                        else f"Delivery takes {d} days.")))

    # currency + budget
    if intent.currency and cand.currency != intent.currency:
        out.append(_c("currency", "Currency", intent.currency, cand.currency, F, "Offer currency differs."))
    else:
        out.append(_c("currency", "Currency", intent.currency, cand.currency, P if intent.currency else U,
                      "Currency matches." if intent.currency else "Currency not specified."))
    if intent.max_budget is None:
        out.append(_c("budget", "Total cost", None, None, U, "No maximum budget set."))
    elif cost is None:
        out.append(_c("budget", "Total cost", f"<= {intent.max_budget}", None, U, "Cost assessment unavailable."))
    elif cost.error:
        out.append(_c("budget", "Total cost", f"<= {intent.max_budget}", None, U, cost.error))
    elif not cost.is_complete:
        if cost.known_subtotal is not None and cost.known_subtotal > intent.max_budget:
            out.append(_c("budget", "Total cost", f"<= {intent.max_budget}", f">= {cost.known_subtotal}", F,
                          f"Known charges alone ({cost.known_subtotal}) already exceed the budget {intent.max_budget}."))
        else:
            out.append(_c("budget", "Total cost", f"<= {intent.max_budget}", f">= {cost.known_subtotal} + unknown", U,
                          f"Not shown on the listing: {', '.join(cost.unknown_items)}."))
    else:
        ok = cost.total <= intent.max_budget
        est = " (includes estimated tax)" if cost.includes_estimates else ""
        out.append(_c("budget", "Total cost", f"<= {intent.max_budget}", f"{cost.total}{est}", P if ok else F,
                      "Within budget." if ok else f"Total {cost.total} exceeds budget {intent.max_budget}."))

    # informational: listing integrity (never auto-passes anything)
    flags = (terms.injection_flags if terms else [])
    if flags:
        out.append(_c("listing_integrity", "Listing content", "data only", "instructions detected", F,
                      "Merchant content contained instructions aimed at AI agents; they were ignored.",
                      mandatory=False))
    if terms and terms.inconsistencies:
        out.append(_c("terms_consistency", "Terms consistency", "consistent", "conflicting", U,
                      "; ".join(terms.inconsistencies), mandatory=False))
    if seller and seller.listing_consistent != P:
        out.append(_c("seller_identity", "Seller identity consistency", "consistent", seller.listing_consistent.value,
                      seller.listing_consistent, "Listing seller name does not match the seller registry."
                      if seller.listing_consistent == F else "Seller identity could not be confirmed.", mandatory=False))
    return out


# ======================================================== checkout stage ====
def checkout_line_sum(snapshot: CheckoutSnapshot) -> Decimal | None:
    if any(l.amount is None or l.status == ChargeStatus.UNKNOWN for l in snapshot.line_items):
        return None
    return money(sum((line_sign(l) * l.amount for l in snapshot.line_items), Decimal("0")))


def evaluate_checkout(contract: ContractTerms, snap: CheckoutSnapshot, now: datetime) -> list[ConditionResult]:
    out: list[ConditionResult] = []
    expired = now >= contract.expires_at
    out.append(_c("contract_valid", "Contract validity", f"valid until {contract.expires_at.isoformat()}",
                  "expired" if expired else "valid", F if expired else P,
                  "Contract has expired." if expired else "Contract is within its validity period."))
    out.append(_c("product", "Product", contract.product_id, snap.product_id,
                  P if snap.product_id == contract.product_id else F,
                  "Same product." if snap.product_id == contract.product_id else "Checkout product differs."))
    vmatch = _same(snap.variant, contract.exact_variant)
    out.append(_c("variant", "Variant", contract.exact_variant, snap.variant, P if vmatch else F,
                  "Same variant." if vmatch else "Checkout variant differs from the approved variant."))
    smatch = snap.seller_id == contract.seller_id
    out.append(_c("seller", "Seller", contract.seller_name, snap.seller_name, P if smatch else F,
                  "Approved seller." if smatch else "Seller differs from the approved seller."))
    qmatch = snap.quantity == contract.quantity
    out.append(_c("quantity", "Quantity", contract.quantity, snap.quantity, P if qmatch else F,
                  "Same quantity." if qmatch else "Quantity differs."))
    cmatch = snap.currency == contract.currency
    out.append(_c("currency", "Currency", contract.currency, snap.currency, P if cmatch else F,
                  "Same currency." if cmatch else "Checkout currency differs; totals are not comparable."))

    unknown_lines = [l.label for l in snap.line_items if l.amount is None or l.status == ChargeStatus.UNKNOWN]
    computed = checkout_line_sum(snap)
    if unknown_lines or snap.stated_total is None:
        out.append(_c("total_known", "All charges disclosed", "all amounts known",
                      f"unknown: {', '.join(unknown_lines) or 'total'}", U,
                      "Checkout contains charges with undisclosed amounts."))
    else:
        consistent = computed == snap.stated_total
        out.append(_c("total_consistent", "Total arithmetic", money_str(computed), money_str(snap.stated_total),
                      P if consistent else F,
                      "Line items sum to the stated total." if consistent else
                      f"Line items sum to {computed} but merchant states {snap.stated_total}."))

    effective = None if (unknown_lines or snap.stated_total is None) else max(computed, snap.stated_total)
    if not cmatch:
        out.append(_c("budget", "Total", f"<= {contract.max_total} {contract.currency}",
                      f"{snap.stated_total} {snap.currency}", F, "Currency mismatch."))
    elif effective is None:
        out.append(_c("budget", "Total", f"<= {contract.max_total}", "UNKNOWN", U, "Final total is not fully known."))
    else:
        ok = effective <= contract.max_total
        out.append(_c("budget", "Total", f"<= {contract.max_total}", money_str(effective), P if ok else F,
                      "Within the approved maximum." if ok else
                      f"Total {effective} exceeds approved maximum {contract.max_total}."))

    recurring = [l for l in snap.line_items if l.recurring or l.kind == ChargeKind.SUBSCRIPTION]
    if not contract.subscription_allowed:
        out.append(_c("subscription", "Subscription", "not allowed",
                      ", ".join(f"{l.label} {l.amount}/{l.interval}" for l in recurring) or "none",
                      F if recurring else P,
                      "Prohibited subscription/recurring charge present"
                      + (" (pre-selected by merchant)." if any(l.preselected for l in recurring) else ".")
                      if recurring else "No subscription."))

    addons = [l for l in snap.line_items if l.kind == ChargeKind.ADDON and (l.sku not in contract.approved_addons)]
    out.append(_c("addons", "Add-ons", ", ".join(contract.approved_addons) or "none",
                  ", ".join(l.label for l in addons) or "none", F if addons else P,
                  "Unapproved add-ons present." if addons else "No unapproved add-ons."))

    if contract.warranty_required or contract.warranty_min_months:
        need = contract.warranty_min_months or 1
        have = snap.terms.warranty_months
        out.append(_c("warranty", "Warranty", f">= {need} months", f"{have} months" if have is not None else "UNKNOWN",
                      U if have is None else (P if have >= need else F),
                      "Warranty not stated at checkout." if have is None else
                      ("Warranty satisfied." if have >= need else f"Warranty only {have} months.")))
    if contract.return_required or contract.free_returns_required:
        days, free = snap.terms.return_window_days, snap.terms.free_returns
        if days is None or (contract.free_returns_required and free is None):
            res, why = U, "Return terms not stated at checkout."
        elif contract.min_return_days and days < contract.min_return_days:
            res, why = F, f"Return window {days} days below {contract.min_return_days}."
        elif contract.free_returns_required and not free:
            res, why = F, "Returns are not free."
        else:
            res, why = P, "Return terms satisfied."
        out.append(_c("returns", "Returns", ("free " if contract.free_returns_required else "") + "returns",
                      f"{days} days, free={free}", res, why))
    return out


def summarise(conditions: list[ConditionResult]) -> dict[str, Tri]:
    def pick(*keys: str) -> Tri:
        sub = [c for c in conditions if c.key in keys]
        return aggregate(sub) if sub else P

    return {
        "product_match": pick("product", "variant", "quantity"),
        "seller_match": pick("seller"),
        "total_match": pick("budget", "total_known", "total_consistent", "subscription", "addons"),
        "currency_match": pick("currency"),
        "terms_match": pick("warranty", "returns", "contract_valid"),
    }


# ===================================================== payment authorization ==
@dataclass
class AuthorizationContext:
    workflow_state: str
    autonomy_mode: str
    contract_id: str
    contract_version: int
    contract_hash: str
    contract_expires_at: datetime
    contract_max_total: Decimal
    contract_currency: str
    approval_status: str | None
    approval_hash: str | None
    approval_version: int | None
    verification_id: str | None
    verification_status: str | None
    verification_contract_hash: str | None
    verification_contract_version: int | None
    is_latest_verification: bool
    amount: Decimal
    currency: str
    now: datetime


@dataclass
class PaymentAuthorization:
    allowed: bool
    reasons: list[str]
    contract_id: str
    contract_version: int
    contract_hash: str
    verification_id: str | None
    amount: str
    currency: str
    signature: str | None = None
    checks: list[str] = field(default_factory=list)

    def signing_payload(self) -> str:
        return canonical_json({"c": self.contract_id, "v": self.contract_version, "h": self.contract_hash,
                               "ver": self.verification_id, "a": self.amount, "cur": self.currency})


def authorize_payment(ctx: AuthorizationContext, secret: str) -> PaymentAuthorization:
    reasons: list[str] = []
    checks: list[str] = []

    def need(ok: bool, passed: str, failed: str) -> None:
        (checks if ok else reasons).append(passed if ok else failed)

    need(ctx.autonomy_mode != "MONITORING_ONLY", "autonomy mode permits purchases",
         "Workflow is in monitoring-only mode; purchases are disabled.")
    need(ctx.workflow_state in ("PAYMENT_PENDING_APPROVAL", "PAYMENT_FAILED"), "workflow awaiting payment",
         f"Workflow state {ctx.workflow_state} does not permit payment.")
    need(ctx.now < ctx.contract_expires_at, "contract unexpired", "Contract has expired.")
    need(ctx.approval_status == "ACTIVE" and ctx.approval_hash == ctx.contract_hash
         and ctx.approval_version == ctx.contract_version,
         "user approval matches current contract version",
         "No active user approval for the current contract version and hash.")
    need(ctx.verification_status == "PASS", "checkout verification PASS",
         f"Checkout verification is {ctx.verification_status or 'missing'}; only PASS may proceed.")
    need(ctx.verification_contract_hash == ctx.contract_hash and ctx.verification_contract_version == ctx.contract_version,
         "verification bound to current contract", "Verification was performed against a different contract version.")
    need(ctx.is_latest_verification, "verification is the latest", "A newer checkout verification supersedes this one.")
    need(ctx.currency == ctx.contract_currency, "currency matches", "Payment currency differs from contract.")
    need(ctx.amount <= ctx.contract_max_total, "amount within maximum", "Payment amount exceeds contract maximum.")
    if ctx.verification_status == "PASS":
        need(ctx.amount > 0, "amount positive", "Payment amount must be positive.")

    auth = PaymentAuthorization(
        allowed=not reasons, reasons=reasons, checks=checks, contract_id=ctx.contract_id,
        contract_version=ctx.contract_version, contract_hash=ctx.contract_hash,
        verification_id=ctx.verification_id, amount=money_str(ctx.amount), currency=ctx.currency,
    )
    if auth.allowed:
        auth.signature = hmac_hex(secret, auth.signing_payload())
    return auth


def verify_authorization(auth: PaymentAuthorization | None, secret: str) -> bool:
    if auth is None or not auth.allowed or not auth.signature:
        return False
    return constant_time_equals(auth.signature, hmac_hex(secret, auth.signing_payload()))
