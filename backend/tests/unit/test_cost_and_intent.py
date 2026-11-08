from decimal import Decimal

import pytest

from app.agents.intent_agent import parse_rules
from app.policies.cost import assess_cost
from app.schemas.common import Charge, ChargeKind, ChargeStatus, EvidenceLevel

from tests.conftest import HEADPHONES


def ch(kind, amount, status=ChargeStatus.CONFIRMED, currency="USD", **kw):
    return Charge(label=kind, kind=ChargeKind(kind), amount=amount, currency=currency, status=status, **kw)


# ----------------------------------------------------------- cost auditor ---
def test_cost_basic():
    c = assess_cost("p", "USD", [ch("product", "90"), ch("shipping", "10"), ch("tax", "5")])
    assert c.total == Decimal("105.00") and c.is_complete


def test_cost_with_subscription():
    c = assess_cost("p", "USD", [ch("product", "90"), ch("shipping", "10"), ch("tax", "5"),
                                 ch("subscription", "15", recurring=True, interval="month")])
    assert c.total == Decimal("120.00")
    assert c.recurring_items


def test_unknown_tax_marks_total_incomplete():
    c = assess_cost("p", "USD", [ch("product", "90"), ch("shipping", "10"), ch("tax", None, ChargeStatus.UNKNOWN)])
    assert c.total is None and not c.is_complete
    assert c.known_subtotal == Decimal("100.00")
    assert "tax" in c.unknown_items


def test_confirmed_discount_applied_estimated_ignored():
    c = assess_cost("p", "USD", [ch("product", "90"), ch("discount", "10"),
                                 ch("discount", "5", ChargeStatus.ESTIMATED)])
    assert c.total == Decimal("80.00")


def test_different_currencies_never_combined():
    c = assess_cost("p", "USD", [ch("product", "90"), ch("shipping", "10", currency="EUR")])
    assert c.total is None and c.error


def test_float_input_is_exact():
    c = assess_cost("p", "USD", [ch("product", 0.1), ch("shipping", 0.2)])
    assert c.total == Decimal("0.30")


# ------------------------------------------------------------ intent agent ---
def test_complete_request():
    i = parse_rules(HEADPHONES, "i1", "u1")
    assert i.category == "headphones"
    assert i.max_budget == Decimal("110.00") and i.currency == "USD"
    assert i.warranty_requirement.min_months == 12
    assert i.return_requirement.required and i.return_requirement.free_returns
    assert i.subscription_allowed is False
    assert i.seller_preferences.min_evidence_level == EvidenceLevel.STRONG
    assert not i.needs_clarification


def test_missing_currency_requests_clarification():
    i = parse_rules("Find headphones under 110 with free returns", "i", "u")
    assert i.max_budget == Decimal("110.00") and i.currency is None
    assert any("currency" in q.lower() for q in i.clarifications_needed)


def test_missing_budget_requests_clarification():
    i = parse_rules("Find me some wireless headphones", "i", "u")
    assert i.max_budget is None and i.needs_clarification


def test_conflicting_requirements_detected():
    i = parse_rules("Find headphones under $50 but over $100, no subscription, subscription is fine", "i", "u")
    assert len(i.conflicts) == 2


def test_no_subscription_default_and_explicit():
    assert parse_rules("headphones under $90, no subscription", "i", "u").subscription_allowed is False
    assert parse_rules("headphones under $90", "i", "u").subscription_allowed is False


@pytest.mark.parametrize("text,budget", [("laptop under $900", "900.00"), ("laptop max $1,250.50", "1250.50"),
                                         ("laptop, budget of €700", "700.00"), ("laptop no more than £450", "450.00")])
def test_budget_values(text, budget):
    assert parse_rules(text, "i", "u").max_budget == Decimal(budget)


def test_warranty_months_not_mistaken_for_budget():
    i = parse_rules("Find a laptop under $900 with 16GB RAM, 512GB SSD and at least a one-year warranty.", "i", "u")
    assert i.max_budget == Decimal("900.00")
    assert i.specifications["ram_gb"] == "16" and i.specifications["storage_gb"] == "512"
    assert i.warranty_requirement.min_months == 12
