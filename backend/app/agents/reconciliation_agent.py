"""Agent 9 - Reconciliation. Compares intent, contract, verified checkout, the
PayPal capture record and merchant receipt evidence. PayPal's capture record is
authoritative for what PayPal charged; a differing receipt is reported as a
discrepancy, never as a claim about what PayPal captured."""
from __future__ import annotations

from decimal import Decimal

from app.agents.base import AgentOutput, BaseAgent
from app.core.util import money
from app.integrations.paypal.client import extract_capture
from app.schemas.common import ConditionResult, EvidenceRef, Tri
from app.schemas.domain import CheckoutSnapshot, ContractTerms, Discrepancy, ReconciliationReport, ShoppingIntent

P, F, U = Tri.PASS, Tri.FAIL, Tri.UNKNOWN


class ReconciliationAgent(BaseAgent):
    agent_id = "reconciliation_agent"
    allowed_tools = frozenset({"paypal.get_order", "merchant.receipt"})

    def run(self, *, intent: ShoppingIntent, contract: ContractTerms, contract_id: str, version: int,
            snapshot: CheckoutSnapshot | None = None, order_id: str | None = None,
            store_report: dict | None = None) -> AgentOutput:
        if store_report is not None:
            return self._store_purchase(intent, contract, store_report)
        checks: list[ConditionResult] = []
        discrepancies: list[Discrepancy] = []
        notes: list[str] = []

        def check(key, label, approved, observed, ok: Tri, reason):
            checks.append(ConditionResult(key=key, label=label, approved=str(approved), observed=str(observed),
                                          result=ok, reason=reason))

        order = self.tools.call("paypal.get_order", order_id)
        cap = extract_capture(order) or {}
        cap_amount = money(cap.get("amount", {}).get("value")) if cap else None
        cap_cur = cap.get("amount", {}).get("currency_code") if cap else None
        unit = (order.get("purchase_units") or [{}])[0]
        paypal_record = {
            "order_id": order.get("id"), "order_status": order.get("status"), "capture_id": cap.get("id"),
            "capture_status": cap.get("status"), "amount": str(cap_amount) if cap_amount is not None else None,
            "currency": cap_cur, "custom_id": unit.get("custom_id"), "invoice_id": unit.get("invoice_id"),
            "source": "PayPal Orders API (GET /v2/checkout/orders/{id})",
        }

        if cap_amount is None:
            check("capture_present", "PayPal capture record", "present", "missing", U, "No capture found on order.")
        else:
            check("captured_vs_verified", "Captured vs verified total", snapshot.stated_total, cap_amount,
                  P if cap_amount == snapshot.stated_total else F,
                  "PayPal captured exactly the verified checkout total." if cap_amount == snapshot.stated_total
                  else "Captured amount differs from the verified checkout total.")
            if cap_amount != snapshot.stated_total:
                discrepancies.append(Discrepancy(code="CAPTURE_AMOUNT_MISMATCH", severity="HIGH",
                                                 description=f"PayPal captured {cap_amount} {cap_cur}; verified "
                                                             f"checkout total was {snapshot.stated_total}.",
                                                 sources=["paypal", "checkout_snapshot"]))
            check("captured_within_max", "Captured within contract maximum", f"<= {contract.max_total}", cap_amount,
                  P if cap_amount <= contract.max_total else F, "Within approved maximum."
                  if cap_amount <= contract.max_total else "Captured amount exceeds approved maximum.")
            check("currency", "Currency", contract.currency, cap_cur, P if cap_cur == contract.currency else F,
                  "Currency matches." if cap_cur == contract.currency else "Currency differs.")
        expected_ref = f"{contract_id}:v{version}"
        check("order_reference", "Order bound to contract", expected_ref, unit.get("custom_id"),
              P if unit.get("custom_id") == expected_ref else F,
              "PayPal order references this contract version." if unit.get("custom_id") == expected_ref
              else "PayPal order does not reference this contract version.")
        # The contract that was paid must still encode the shopper's original constraints.
        drift = intent_drift(intent, contract)
        check("intent_constraints", "Contract matches original intent",
              f"budget {intent.max_budget} {intent.currency}, qty {intent.quantity}",
              f"max {contract.max_total} {contract.currency}, qty {contract.quantity}",
              F if drift else P, "; ".join(drift) if drift else
              "Budget, currency, quantity, subscription, warranty and return constraints are preserved.")
        if drift:
            discrepancies.append(Discrepancy(code="INTENT_CONTRACT_DRIFT", severity="HIGH",
                                             description="Contract no longer reflects the original intent: "
                                                         + "; ".join(drift), sources=["intent", "contract"]))

        receipt = self.tools.call("merchant.receipt", snapshot.checkout_id)
        if receipt is None:
            notes.append("No merchant receipt evidence available; item-level reconciliation is limited to the "
                         "verified checkout snapshot.")
            check("receipt", "Merchant receipt", "available", "unavailable", U, "No receipt evidence.")
        else:
            r_total = money(receipt["total"])
            same_items = (receipt["product_id"] == contract.product_id and
                          (receipt.get("variant") or "").casefold() == (contract.exact_variant or "").casefold() and
                          receipt["quantity"] == contract.quantity)
            check("receipt_items", "Receipt items", f"{contract.product_id}/{contract.exact_variant} x{contract.quantity}",
                  f"{receipt['product_id']}/{receipt.get('variant')} x{receipt['quantity']}",
                  P if same_items else F, "Receipt items match the contract." if same_items else
                  "Receipt items differ from the contract.")
            if not same_items:
                discrepancies.append(Discrepancy(code="RECEIPT_ITEM_MISMATCH", severity="HIGH",
                                                 description="Merchant receipt lists different items than the "
                                                             "approved contract.", sources=["merchant_receipt"]))
            if cap_amount is not None:
                ok = r_total == cap_amount and receipt["currency"] == cap_cur
                check("receipt_total", "Receipt total vs PayPal capture", cap_amount, r_total, P if ok else F,
                      "Receipt total matches the PayPal capture." if ok else
                      "Merchant receipt total differs from the PayPal capture record.")
                if not ok:
                    extra = [l["label"] for l in receipt["lines"]
                             if l["label"] not in {x.label for x in snapshot.line_items}]
                    discrepancies.append(Discrepancy(
                        code="RECEIPT_TOTAL_MISMATCH", severity="HIGH",
                        description=(f"Merchant receipt reports {r_total} {receipt['currency']}, but the PayPal "
                                     f"capture record shows {cap_amount} {cap_cur}. The PayPal record remains the "
                                     f"authoritative amount charged through PayPal; the receipt difference"
                                     + (f" (extra lines: {', '.join(extra)})" if extra else "")
                                     + " must be resolved with the merchant."),
                        sources=["paypal", "merchant_receipt"]))
            notes.append(f"Receipt source: {receipt.get('source_label')}.")

        if discrepancies:
            status = "DISCREPANCY"
        elif any(c.result == U for c in checks):
            status = "INCOMPLETE"
        else:
            status = "RECONCILED"
        report = ReconciliationReport(status=status, checks=checks, discrepancies=discrepancies,
                                      paypal_record=paypal_record, receipt=receipt, notes=notes)
        evidence = [EvidenceRef(source="PayPal", reference=f"order:{order.get('id')}", description="Order + capture")]
        if receipt:
            evidence.append(EvidenceRef(source=receipt["source_label"], reference=receipt["receipt_id"],
                                        description="Merchant receipt", untrusted_content=True))
        return AgentOutput(value=report, evidence=evidence, missing=[] if receipt else ["merchant receipt"],
                           confidence=1.0 if status == "RECONCILED" else 0.6)

    @staticmethod
    def _store_purchase(intent: ShoppingIntent, contract: ContractTerms, rep: dict) -> AgentOutput:
        """Purchase made on a real store's site and reported by the shopper."""
        from app.core.util import utcnow

        checks: list[ConditionResult] = []
        discrepancies: list[Discrepancy] = []

        def check(key, label, approved, observed, ok: Tri, reason):
            checks.append(ConditionResult(key=key, label=label, approved=str(approved), observed=str(observed),
                                          result=ok, reason=reason))

        amount, cur = money(rep["amount"]), rep["currency"]
        if cur != contract.currency:
            check("currency", "Currency", contract.currency, cur, F, "You paid in a different currency.")
            discrepancies.append(Discrepancy(code="CURRENCY_MISMATCH", severity="HIGH",
                                             description=f"You reported paying in {cur}; the plan was in "
                                                         f"{contract.currency}.", sources=["you"]))
        ok = cur == contract.currency and amount <= contract.max_total
        check("reported_within_max", "Amount paid within your limit", f"<= {contract.max_total} {contract.currency}",
              f"{amount} {cur}", P if ok else F,
              "Within the maximum you approved." if ok else "Above the maximum you approved.")
        if cur == contract.currency and amount > contract.max_total:
            discrepancies.append(Discrepancy(code="OVER_APPROVED_MAXIMUM", severity="HIGH",
                                             description=f"You reported paying {amount} {cur}, above your approved "
                                                         f"maximum of {contract.max_total} {contract.currency}.",
                                             sources=["you", "contract"]))
        confirmations = rep.get("confirmations") or {}
        for item in contract.checklist:
            if item.get("auto"):
                continue
            v = confirmations.get(item["key"])
            if v is True:
                check(f"confirm_{item['key']}", item["label"], item["requirement"], "confirmed", P,
                      "Confirmed by you on the store's page.")
            elif v is False:
                check(f"confirm_{item['key']}", item["label"], item["requirement"], "not met", F,
                      "You reported this requirement was not met.")
                discrepancies.append(Discrepancy(code=f"NOT_MET_{item['key'].upper()}", severity="HIGH",
                                                 description=f"{item['label']}: you reported the purchase did not meet "
                                                             f"'{item['requirement']}'.", sources=["you"]))
            else:
                check(f"confirm_{item['key']}", item["label"], item["requirement"], "not confirmed", U,
                      "Not confirmed.")
        drift = intent_drift(intent, contract)
        check("intent_constraints", "Plan matches original intent", f"budget {intent.max_budget} {intent.currency}",
              f"max {contract.max_total} {contract.currency}", F if drift else P,
              "; ".join(drift) or "Your original constraints are preserved.")
        if drift:
            discrepancies.append(Discrepancy(code="INTENT_CONTRACT_DRIFT", severity="HIGH",
                                             description="; ".join(drift), sources=["intent", "contract"]))
        status = "DISCREPANCY" if discrepancies else ("INCOMPLETE" if any(c.result == U for c in checks)
                                                      else "RECONCILED")
        receipt = {"source_label": "Self-reported by you (CHECKMATE did not process this payment)",
                   "store": contract.store_name, "total": str(amount), "currency": cur,
                   "order_reference": rep.get("order_reference"), "reported_at": utcnow().isoformat(),
                   "store_url": contract.store_url}
        report = ReconciliationReport(status=status, checks=checks, discrepancies=discrepancies, paypal_record={},
                                      receipt=receipt, notes=["Amount and confirmations are as you reported them."])
        return AgentOutput(value=report, evidence=[EvidenceRef(source="You (self-reported)",
                                                               reference=rep.get("order_reference") or "store purchase",
                                                               description="Purchase report")],
                           confidence=0.7)


def intent_drift(intent: ShoppingIntent, contract: ContractTerms) -> list[str]:
    drift = []
    if contract.max_total != intent.max_budget:
        drift.append(f"max total {contract.max_total} vs intent budget {intent.max_budget}")
    if contract.currency != intent.currency:
        drift.append(f"currency {contract.currency} vs {intent.currency}")
    if contract.quantity != intent.quantity:
        drift.append(f"quantity {contract.quantity} vs {intent.quantity}")
    if contract.subscription_allowed and not intent.subscription_allowed:
        drift.append("contract allows a subscription the shopper prohibited")
    if (intent.warranty_requirement.min_months or 0) > (contract.warranty_min_months or 0):
        drift.append("contract warranty requirement is weaker than requested")
    if intent.return_requirement.free_returns and not contract.free_returns_required:
        drift.append("contract dropped the free-returns requirement")
    return drift


def sum_lines(lines: list[dict]) -> Decimal:
    return money(sum((Decimal(l["amount"]) for l in lines if l.get("amount") is not None), Decimal("0")))
