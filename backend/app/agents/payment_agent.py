"""Agent 8 - Payment. Executes PayPal operations only when handed a valid,
policy-engine-signed PaymentAuthorization. It cannot alter contracts, approve
failed checkouts or override restrictions."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.agents.base import BaseAgent
from app.core.util import money, money_str
from app.integrations.paypal.client import PayPalError, PayPalTimeout, approve_link, extract_capture
from app.policies.engine import PaymentAuthorization, verify_authorization
from app.schemas.common import ChargeKind
from app.schemas.domain import CheckoutSnapshot, ContractTerms


class PaymentNotAuthorized(PermissionError):
    pass


@dataclass
class PaymentOutcome:
    status: str  # APPROVAL_PENDING | CAPTURED | PENDING | NOT_APPROVED | UNCERTAIN | FAILED | ALREADY_CAPTURED
    order_id: str | None = None
    approve_url: str | None = None
    capture_id: str | None = None
    amount: str | None = None
    currency: str | None = None
    paypal_status: str | None = None
    error: str | None = None


def build_order_payload(contract: ContractTerms, snap: CheckoutSnapshot, *, contract_id: str, version: int,
                        invoice_id: str, return_url: str, cancel_url: str) -> dict:
    cur = snap.currency

    def amt(v: Decimal) -> dict:
        return {"currency_code": cur, "value": money_str(v)}

    def total(kind: ChargeKind) -> Decimal:
        return sum((l.amount for l in snap.line_items if l.kind == kind), Decimal("0"))

    item_kinds = (ChargeKind.PRODUCT, ChargeKind.ADDON, ChargeKind.SUBSCRIPTION)
    product_lines = [l for l in snap.line_items if l.kind in item_kinds]
    if snap.stated_total is None or any(l.amount is None for l in snap.line_items):
        raise ValueError("Checkout has unknown amounts; it cannot be sent to PayPal")
    items = []
    for l in product_lines:
        qty = max(l.quantity, 1)
        unit = money(l.amount / qty)
        if money(unit * qty) != l.amount:  # not evenly divisible: send as one line to keep totals exact
            unit, qty = l.amount, 1
        items.append({"name": l.label[:127], "quantity": str(qty), "unit_amount": amt(unit),
                      "sku": (l.sku or contract.product_id)[:127],
                      "category": "DIGITAL_GOODS" if l.kind == ChargeKind.SUBSCRIPTION else "PHYSICAL_GOODS"})
    item_total = sum((money(Decimal(i["unit_amount"]["value"]) * int(i["quantity"])) for i in items), Decimal("0"))
    shipping, tax, fees, discount = (total(ChargeKind.SHIPPING), total(ChargeKind.TAX), total(ChargeKind.FEE),
                                     total(ChargeKind.DISCOUNT))
    grand = money(item_total + shipping + tax + fees - discount)
    if grand != snap.stated_total:
        raise ValueError(f"Order breakdown {grand} does not equal verified total {snap.stated_total}")
    breakdown = {"item_total": amt(item_total), "shipping": amt(shipping), "tax_total": amt(tax)}
    if fees:
        breakdown["handling"] = amt(fees)
    if discount:
        breakdown["discount"] = amt(discount)
    return {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": contract_id[:256],
            "custom_id": f"{contract_id}:v{version}"[:127],
            "invoice_id": invoice_id[:127],
            "description": f"{contract.product_name} ({contract.exact_variant})"[:127],
            "amount": {"currency_code": cur, "value": money_str(grand), "breakdown": breakdown},
            "items": items,
        }],
        "payment_source": {"paypal": {"experience_context": {
            "brand_name": "CHECKMATE Demo",
            "user_action": "PAY_NOW",
            "shipping_preference": "NO_SHIPPING",
            "return_url": return_url,
            "cancel_url": cancel_url,
        }}},
    }


class PaymentAgent(BaseAgent):
    agent_id = "payment_agent"
    allowed_tools = frozenset({"paypal.create_order", "paypal.get_order", "paypal.capture_order"})

    def __init__(self, registry, authorization_secret: str):
        super().__init__(registry)
        self._secret = authorization_secret

    def _require(self, auth: PaymentAuthorization | None, contract_hash: str | None, amount: str) -> None:
        if auth is None or not verify_authorization(auth, self._secret):
            raise PaymentNotAuthorized("Missing or invalid policy-engine authorization")
        if auth.contract_hash != contract_hash or auth.amount != amount:
            raise PaymentNotAuthorized("Authorization does not cover this contract version or amount")

    def create_order(self, auth: PaymentAuthorization, payload: dict, request_id: str) -> PaymentOutcome:
        self._require(auth, auth.contract_hash if auth else None, payload["purchase_units"][0]["amount"]["value"])
        try:
            order = self.tools.call("paypal.create_order", payload, request_id)
        except PayPalTimeout as e:
            return PaymentOutcome(status="UNCERTAIN", error=str(e))
        except PayPalError as e:
            return PaymentOutcome(status="FAILED", error=f"{e.name}: {e.message}")
        return PaymentOutcome(status="APPROVAL_PENDING", order_id=order["id"], approve_url=approve_link(order),
                              paypal_status=order.get("status"))

    def capture(self, auth: PaymentAuthorization, *, order_id: str, contract_hash: str, amount: str,
                request_id: str) -> PaymentOutcome:
        self._require(auth, contract_hash, amount)
        try:
            order = self.tools.call("paypal.capture_order", order_id, request_id)
        except PayPalTimeout as e:
            # Never assume failure: ask PayPal what actually happened.
            return self._resolve_uncertain(order_id, str(e))
        except PayPalError as e:
            issue = e.issue
            if issue == "ORDER_NOT_APPROVED":
                return PaymentOutcome(status="NOT_APPROVED", order_id=order_id, error="Buyer has not approved yet.")
            if issue == "ORDER_ALREADY_CAPTURED":
                return self._resolve_uncertain(order_id, "already captured")
            if e.status >= 500:
                return self._resolve_uncertain(order_id, str(e))
            return PaymentOutcome(status="FAILED", order_id=order_id, error=f"{issue or e.name}: {e.message}")
        return self._from_order(order)

    def _resolve_uncertain(self, order_id: str, why: str) -> PaymentOutcome:
        try:
            order = self.tools.call("paypal.get_order", order_id)
        except (PayPalTimeout, PayPalError):
            return PaymentOutcome(status="UNCERTAIN", order_id=order_id, error=why)
        if order.get("status") == "COMPLETED":
            return self._from_order(order)
        return PaymentOutcome(status="UNCERTAIN", order_id=order_id, paypal_status=order.get("status"), error=why)

    def get_order(self, order_id: str) -> dict:
        return self.tools.call("paypal.get_order", order_id)

    @staticmethod
    def _from_order(order: dict) -> PaymentOutcome:
        cap = extract_capture(order)
        if not cap:
            return PaymentOutcome(status="UNCERTAIN", order_id=order.get("id"), paypal_status=order.get("status"),
                                  error="No capture found on order")
        status = {"COMPLETED": "CAPTURED", "PENDING": "PENDING"}.get(cap.get("status"), "FAILED")
        return PaymentOutcome(status=status, order_id=order.get("id"), capture_id=cap.get("id"),
                              amount=cap["amount"]["value"], currency=cap["amount"]["currency_code"],
                              paypal_status=cap.get("status"))
