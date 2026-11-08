"""Deterministic financial arithmetic. No LLM ever produces a monetary total."""
from __future__ import annotations

from decimal import Decimal

from app.core.util import money
from app.schemas.common import Charge, ChargeKind, ChargeStatus
from app.schemas.domain import CostAssessment


def line_sign(line: Charge) -> int:
    return -1 if line.kind == ChargeKind.DISCOUNT else 1


def assess_cost(product_id: str, currency: str, lines: list[Charge]) -> CostAssessment:
    """total = product + shipping + tax + disclosed fees + mandatory add-ons - confirmed discounts.

    * Lines in a different currency make the total invalid (never silently combined).
    * Any UNKNOWN line makes the total incomplete (``total`` is None).
    * Only CONFIRMED discounts are subtracted.
    * Recurring charges contribute their first billing period and are listed separately.
    """
    mixed = sorted({l.currency for l in lines if l.currency != currency})
    unknown = [l.label for l in lines if l.mandatory and (l.amount is None or l.status == ChargeStatus.UNKNOWN)]
    estimated = [l.label for l in lines if l.status == ChargeStatus.ESTIMATED and l.amount is not None]
    recurring = [f"{l.label} ({l.amount}/{l.interval or 'period'})" for l in lines if l.recurring]
    if mixed:
        return CostAssessment(
            product_id=product_id, currency=currency, lines=lines, total=None, known_subtotal=None,
            is_complete=False, includes_estimates=bool(estimated), unknown_items=unknown,
            estimated_items=estimated, recurring_items=recurring,
            error=f"Lines in other currencies ({', '.join(mixed)}) cannot be combined into a {currency} total.",
        )
    subtotal = Decimal("0")
    for l in lines:
        if not l.mandatory or l.amount is None or l.status == ChargeStatus.UNKNOWN:
            continue
        if l.kind == ChargeKind.DISCOUNT and l.status != ChargeStatus.CONFIRMED:
            continue
        subtotal += line_sign(l) * l.amount
    subtotal = money(subtotal)
    complete = not unknown
    return CostAssessment(
        product_id=product_id, currency=currency, lines=lines,
        total=subtotal if complete else None, known_subtotal=subtotal, is_complete=complete,
        includes_estimates=bool(estimated), unknown_items=unknown, estimated_items=estimated,
        recurring_items=recurring,
    )
