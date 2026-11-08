"""Workflow orchestrator. Coordinates agents, persists every step, and routes all
allow/deny decisions through the deterministic policy engine."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.base import AgentRunResult
from app.agents.contract_agent import build_contract_terms, rank_eligible
from app.agents.payment_agent import PaymentAgent, PaymentOutcome, build_order_payload
from app.core.util import hash_obj, money, money_str, new_id, to_jsonable, utcnow
from app.database.models import (
    AgentRun,
    CheckoutSnapshotRow,
    CheckoutVerificationRow,
    ContractApproval,
    ContractVersion,
    PaymentTransaction,
    PurchaseContract,
    PurchasePassportRow,
    WebhookEvent,
    Workflow,
)
from app.database.session import SessionLocal
from app.integrations.merchants.catalog import SCENARIOS
from app.integrations.paypal.client import PayPalError, PayPalTimeout
from app.orchestrator.events import emit, list_events, verify_chain
from app.orchestrator.state_machine import POST_CAPTURE, TERMINAL, WS, assert_transition, can_transition
from app.policies import engine
from app.policies.contracts import HASH_ALGORITHM, contract_hash, material_changes
from app.schemas.common import AgentEnvelope, ConditionResult, Tri, aggregate
from app.schemas.domain import (
    CandidateEvaluation,
    CheckoutSnapshot,
    ContractTerms,
    ReconciliationReport,
    ShoppingIntent,
)
from app.services.container import Container, get_container

log = logging.getLogger("checkmate.orchestrator")

AUTONOMY_MODES = ("ASSISTED", "MONITORING_ONLY")
ACTIVE_PAYMENT = ("CREATING", "APPROVAL_PENDING", "APPROVED", "CAPTURING", "UNCERTAIN", "PENDING", "CAPTURED")
CONTRACT_STATES = (WS.CONTRACT_CREATED.value, WS.CHECKOUT_BLOCKED.value, WS.PAYMENT_PENDING_APPROVAL.value,
                   WS.PAYMENT_FAILED.value)

AGENT_EVENT = {
    "intent_agent": "intent.parsed",
    "discovery_agent": "product.discovered",
    "seller_trust_agent": "seller.assessed",
    "terms_agent": "terms.extracted",
    "cost_auditor": "cost.calculated",
    "contract_agent": "contract.drafted",
    "checkout_agent": "checkout.snapshot_obtained",
    "reconciliation_agent": "receipt.reconciled",
    "monitoring_agent": "offer.monitored",
}


class ServiceError(Exception):
    status_code = 400

    def __init__(self, detail, status_code: int | None = None):
        super().__init__(str(detail))
        self.detail = detail
        if status_code:
            self.status_code = status_code


class NotFound(ServiceError):
    status_code = 404


class Conflict(ServiceError):
    status_code = 409


class Forbidden(ServiceError):
    status_code = 403


# ================================================================ helpers ====
def set_state(db: Session, wf: Workflow, new: WS, message: str, payload: dict | None = None) -> None:
    assert_transition(wf.state, new.value)
    old = wf.state
    wf.state = new.value
    db.commit()
    emit(db, wf, "workflow.state_changed", message, payload={"from": old, "to": new.value, **(payload or {})})


def record_run(db: Session, wf: Workflow, run: AgentRunResult, message: str | None = None) -> None:
    env = run.envelope
    db.add(AgentRun(id=new_id("run"), workflow_id=wf.id, agent_id=env.agent_id, subject=env.subject,
                    status=env.status, output=env.result, evidence=[e.model_dump() for e in env.evidence_references],
                    missing_information=env.missing_information, confidence=env.confidence, error=env.error,
                    started_at=run.started_at, finished_at=run.finished_at, duration_ms=run.duration_ms))
    db.commit()
    etype = AGENT_EVENT.get(env.agent_id, f"agent.{env.agent_id}") if env.status == "OK" else f"agent.failed"
    emit(db, wf, etype, message or (f"{env.agent_id} completed" if env.status == "OK" else
                                    f"{env.agent_id} failed: {env.error}"),
         agent_id=env.agent_id,
         payload={"subject": env.subject, "status": env.status, "missing_information": env.missing_information,
                  "confidence": env.confidence, "error": env.error, "duration_ms": run.duration_ms})


def _failed_run(workflow_id: str, agent_id: str, subject: str, error: str) -> AgentRunResult:
    now = utcnow()
    env = AgentEnvelope(workflow_id=workflow_id, agent_id=agent_id, timestamp=now, status="ERROR", subject=subject,
                        error=error, missing_information=["agent output unavailable"])
    return AgentRunResult(envelope=env, value=None, started_at=now, finished_at=now, duration_ms=0)


def _collect(fut, workflow_id: str, agent_id: str, subject: str, timeout: float) -> AgentRunResult:
    try:
        return fut.result(timeout=timeout)
    except FutureTimeout:
        return _failed_run(workflow_id, agent_id, subject, f"timed out after {timeout}s")
    except Exception as e:  # noqa: BLE001
        return _failed_run(workflow_id, agent_id, subject, f"{e.__class__.__name__}: {e}")


def owned_workflow(db: Session, user_id: str, workflow_id: str) -> Workflow:
    wf = db.get(Workflow, workflow_id)
    if not wf or wf.user_id != user_id:
        raise NotFound("Workflow not found")
    return wf


def owned_contract(db: Session, user_id: str, contract_id: str) -> tuple[PurchaseContract, Workflow]:
    row = db.get(PurchaseContract, contract_id)
    if not row or row.user_id != user_id:
        raise NotFound("Contract not found")
    return row, db.get(Workflow, row.workflow_id)


def owned_payment(db: Session, user_id: str, payment_id: str) -> PaymentTransaction:
    p = db.get(PaymentTransaction, payment_id)
    if not p or p.user_id != user_id:
        raise NotFound("Payment not found")
    return p


def active_approval(db: Session, row: PurchaseContract) -> ContractApproval | None:
    return db.scalars(select(ContractApproval).where(
        ContractApproval.contract_id == row.id, ContractApproval.status == "ACTIVE",
        ContractApproval.version == row.version, ContractApproval.contract_hash == row.contract_hash)).first()


def latest_verification(db: Session, contract_id: str) -> CheckoutVerificationRow | None:
    return db.scalars(select(CheckoutVerificationRow).where(CheckoutVerificationRow.contract_id == contract_id)
                      .order_by(CheckoutVerificationRow.created_at.desc())).first()


def _ttl() -> timedelta:
    return timedelta(hours=get_container().settings.contract_ttl_hours)


# =============================================================== pipeline ====
def create_workflow(db: Session, user_id: str, text: str, autonomy_mode: str) -> Workflow:
    if autonomy_mode not in AUTONOMY_MODES:
        raise ServiceError(f"autonomy_mode must be one of {AUTONOMY_MODES}")
    wf = Workflow(id=new_id("wf"), user_id=user_id, state=WS.CREATED.value, autonomy_mode=autonomy_mode,
                  raw_request=text)
    db.add(wf)
    db.commit()
    emit(db, wf, "intent.created", "Shopping request received", payload={"autonomy_mode": autonomy_mode})
    return wf


def run_pipeline(workflow_id: str) -> None:
    """Persistent, resumable: picks up from whatever state the workflow is in."""
    c = get_container()
    with SessionLocal() as db:
        wf = db.get(Workflow, workflow_id)
        if wf is None:
            return
        try:
            _pipeline(db, wf, c)
        except Exception as e:  # noqa: BLE001
            log.exception("pipeline failed for %s", workflow_id)
            db.rollback()
            wf = db.get(Workflow, workflow_id)
            wf.error = f"{e.__class__.__name__}: {e}"
            db.commit()
            emit(db, wf, "workflow.error", f"Workflow error: {wf.error}")
            if can_transition(wf.state, WS.EVIDENCE_INCOMPLETE.value):
                set_state(db, wf, WS.EVIDENCE_INCOMPLETE, "Stopped safely after an internal error; no payment "
                                                         "can proceed. Retry is available.")


def _pipeline(db: Session, wf: Workflow, c: Container) -> None:
    state = WS(wf.state)
    if state in (WS.DISCOVERING, WS.EVALUATING):  # interrupted mid-run (e.g. worker restart)
        set_state(db, wf, WS.EVIDENCE_INCOMPLETE, "Resuming interrupted workflow")
        state = WS.EVIDENCE_INCOMPLETE
    if state in (WS.CREATED, WS.NEEDS_CLARIFICATION):
        run = c.intent().execute(wf.id, subject="request", text=wf.raw_request, intent_id=new_id("int"),
                                 user_id=wf.user_id)
        intent: ShoppingIntent | None = run.value
        record_run(db, wf, run, "Requirements extracted" if run.ok else None)
        if not run.ok:
            raise RuntimeError("Intent agent failed")
        wf.intent = intent.model_dump(mode="json")
        db.commit()
        if intent.needs_clarification:
            set_state(db, wf, WS.NEEDS_CLARIFICATION, "Clarification required before searching",
                      payload={"questions": intent.clarifications_needed, "conflicts": intent.conflicts})
            return
        set_state(db, wf, WS.INTENT_PARSED, "Shopping intent parsed")
        state = WS.INTENT_PARSED
    if state not in (WS.INTENT_PARSED, WS.EVIDENCE_INCOMPLETE):
        return
    intent = ShoppingIntent.model_validate(wf.intent)
    set_state(db, wf, WS.DISCOVERING, "Discovering products")
    evaluations = discover_and_evaluate(db, wf, c, intent, exclude=[], mark_states=True)
    if evaluations is None:
        return
    _draft_contract(db, wf, c, intent, evaluations)


def discover_and_evaluate(db: Session, wf: Workflow, c: Container, intent: ShoppingIntent, exclude: list[str],
                          mark_states: bool) -> list[CandidateEvaluation] | None:
    timeout = c.settings.agent_timeout_seconds
    disc = c.discovery().execute(wf.id, subject=intent.category or intent.product_description, intent=intent,
                                 exclude_product_ids=exclude)
    n = len(disc.value or [])
    record_run(db, wf, disc, f"{n} candidate product(s) found" if disc.ok else None)
    if not disc.ok or not disc.value:
        if mark_states:
            wf.blocked_reason = "No matching products were found in supported sources."
            set_state(db, wf, WS.EVIDENCE_INCOMPLETE, wf.blocked_reason)
            return None
        return []
    candidates = disc.value
    if mark_states:
        set_state(db, wf, WS.EVALUATING, "Seller and terms agents evaluating candidates in parallel")

    with ThreadPoolExecutor(max_workers=8) as pool:
        s_futs = {x.id: pool.submit(c.seller().execute, wf.id, x.id, candidate=x) for x in candidates}
        t_futs = {x.id: pool.submit(c.terms().execute, wf.id, x.id, candidate=x) for x in candidates}
        sellers = {k: _collect(f, wf.id, "seller_trust_agent", k, timeout) for k, f in s_futs.items()}
        terms = {k: _collect(f, wf.id, "terms_agent", k, timeout) for k, f in t_futs.items()}
        c_futs = {x.id: pool.submit(c.cost().execute, wf.id, x.id, candidate=x, terms=terms[x.id].value,
                                    quantity=intent.quantity) for x in candidates}
        costs = {k: _collect(f, wf.id, "cost_auditor", k, timeout) for k, f in c_futs.items()}

    evaluations: list[CandidateEvaluation] = []
    for x in candidates:
        s, t, k = sellers[x.id], terms[x.id], costs[x.id]
        record_run(db, wf, s, f"{x.product_name}: {s.value.evidence_level.value}" if s.ok else None)
        tmsg = None
        if t.ok:
            tmsg = f"{x.product_name}: terms extracted ({len(t.value.unknown_fields)} unknown)"
            if t.value.injection_flags:
                tmsg += " - embedded instructions detected and ignored"
        record_run(db, wf, t, tmsg)
        record_run(db, wf, k, (f"{x.product_name}: total {k.value.total} {k.value.currency}" if k.value.is_complete
                               else f"{x.product_name}: total incomplete (unknown: {', '.join(k.value.unknown_items)})")
                   if k.ok else None)
        conds = engine.evaluate_candidate(intent, x, s.value, t.value, k.value)
        evaluations.append(CandidateEvaluation(
            candidate=x, seller=s.value, terms=t.value, cost=k.value, conditions=conds, status=aggregate(conds),
            agent_errors=[r.envelope.error for r in (s, t, k) if not r.ok]))
    summary = {e.candidate.id: e.status.value for e in evaluations}
    emit(db, wf, "evaluation.completed", f"{sum(v == 'PASS' for v in summary.values())} of {len(summary)} "
                                         f"candidates satisfy every mandatory condition", payload=summary)
    if mark_states:
        wf.evaluations = [e.model_dump(mode="json") for e in evaluations]
        db.commit()
    return evaluations


def _draft_contract(db: Session, wf: Workflow, c: Container, intent: ShoppingIntent,
                    evaluations: list[CandidateEvaluation]) -> None:
    run = c.contract().execute(wf.id, subject="contract", intent=intent, evaluations=evaluations,
                               for_workflow=wf.id, expires_at=utcnow() + _ttl())
    record_run(db, wf, run, "Purchase contract drafted" if run.ok and run.value else
               "No offer satisfied every mandatory condition")
    if not run.ok or run.value is None:
        fails = sum(e.status == Tri.FAIL for e in evaluations)
        unknown = sum(e.status == Tri.UNKNOWN for e in evaluations)
        wf.blocked_reason = (f"No offer satisfied every mandatory condition: {fails} failed a requirement and "
                             f"{unknown} had mandatory information that could not be verified (UNKNOWN).")
        set_state(db, wf, WS.EVIDENCE_INCOMPLETE, wf.blocked_reason)
        return
    create_contract(db, wf, run.value)
    set_state(db, wf, WS.CONTRACT_CREATED, "Purchase contract ready for your approval")


# ============================================================== contracts ====
def create_contract(db: Session, wf: Workflow, terms: ContractTerms) -> PurchaseContract:
    h = contract_hash(terms)
    row = PurchaseContract(id=new_id("ctr"), workflow_id=wf.id, user_id=wf.user_id, version=1, contract_hash=h,
                           terms=terms.model_dump(mode="json"), approval_status="PENDING", expires_at=terms.expires_at)
    db.add(row)
    db.flush()
    db.add(ContractVersion(contract_id=row.id, version=1, contract_hash=h, terms=row.terms, change_reason="initial"))
    wf.current_contract_id = row.id
    wf.blocked_reason = None
    db.commit()
    emit(db, wf, "contract.created", f"Contract v1 created for {terms.product_name}", agent_id="contract_agent",
         payload={"contract_id": row.id, "version": 1, "contract_hash": h})
    return row


def _invalidate_approvals(db: Session, wf: Workflow, row: PurchaseContract, status: str, reason: str) -> int:
    n = 0
    for a in db.scalars(select(ContractApproval).where(ContractApproval.contract_id == row.id,
                                                       ContractApproval.status == "ACTIVE")):
        a.status, a.reason, a.ended_at = status, reason, utcnow()
        n += 1
    for p in db.scalars(select(PaymentTransaction).where(
            PaymentTransaction.contract_id == row.id,
            PaymentTransaction.payment_status.in_(("CREATING", "APPROVAL_PENDING", "APPROVED")))):
        p.payment_status, p.error = "CANCELLED", f"Cancelled: {reason}"
    db.commit()
    if n:
        emit(db, wf, f"contract.approval_{status.lower()}", f"Approval {status.lower()}: {reason}",
             payload={"contract_id": row.id, "version": row.version})
    return n


def revise_contract(db: Session, wf: Workflow, row: PurchaseContract, new_terms: ContractTerms,
                    reason: str) -> list[str]:
    old = ContractTerms.model_validate(row.terms)
    changes = material_changes(old, new_terms)
    if not changes:
        return []
    _invalidate_approvals(db, wf, row, "INVALIDATED", f"material change ({', '.join(changes)})")
    row.version += 1
    row.contract_hash = contract_hash(new_terms)
    row.terms = new_terms.model_dump(mode="json")
    row.expires_at = new_terms.expires_at
    row.approval_status = "PENDING"
    db.add(ContractVersion(contract_id=row.id, version=row.version, contract_hash=row.contract_hash, terms=row.terms,
                           change_reason=reason))
    db.commit()
    emit(db, wf, "contract.revised", f"Contract revised to v{row.version}: {reason}", agent_id="contract_agent",
         payload={"contract_id": row.id, "version": row.version, "contract_hash": row.contract_hash,
                  "changed_fields": changes})
    return changes


def approve_contract(db: Session, user_id: str, contract_id: str, presented_hash: str) -> ContractApproval:
    row, wf = owned_contract(db, user_id, contract_id)
    if wf.state not in CONTRACT_STATES:
        raise Conflict(f"Contract cannot be approved while workflow is {wf.state}")
    if utcnow() >= row.expires_at:
        raise Conflict("Contract has expired")
    if presented_hash != row.contract_hash:
        raise Conflict({"message": "The contract changed since you reviewed it. Review the current version.",
                        "current_version": row.version, "current_hash": row.contract_hash})
    existing = active_approval(db, row)
    if existing:
        return existing
    a = ContractApproval(id=new_id("apv"), contract_id=row.id, version=row.version, contract_hash=row.contract_hash,
                         user_id=user_id)
    row.approval_status = "APPROVED"
    db.add(a)
    db.commit()
    emit(db, wf, "contract.approved", f"You approved contract v{row.version}",
         payload={"contract_id": row.id, "version": row.version, "contract_hash": row.contract_hash})
    return a


def revoke_contract(db: Session, user_id: str, contract_id: str) -> PurchaseContract:
    row, wf = owned_contract(db, user_id, contract_id)
    if WS(wf.state) in POST_CAPTURE or WS(wf.state) in TERMINAL:
        raise Conflict(f"Nothing to revoke while workflow is {wf.state}")
    _invalidate_approvals(db, wf, row, "REVOKED", "revoked by user")
    row.approval_status = "REVOKED"
    db.commit()
    if wf.state in (WS.PAYMENT_PENDING_APPROVAL.value, WS.CHECKOUT_BLOCKED.value, WS.PAYMENT_FAILED.value):
        set_state(db, wf, WS.CONTRACT_CREATED, "Approval revoked; payment can no longer proceed")
    return row


# ============================================================= monitoring ====
def monitor(db: Session, wf: Workflow, row: PurchaseContract):
    c = get_container()
    terms = ContractTerms.model_validate(row.terms)
    watched = terms.model_copy(update={"expires_at": min(terms.expires_at, row.expires_at)})  # stricter wins
    run = c.monitoring().execute(wf.id, subject=row.id, contract=watched, contract_id=row.id, now=utcnow())
    report = run.value
    record_run(db, wf, run, ("Offer unchanged" if not report.changes else "; ".join(report.changes)) if run.ok else None)
    if not run.ok:
        return None
    if report.expired:
        _invalidate_approvals(db, wf, row, "INVALIDATED", "contract expired")
        row.approval_status = "EXPIRED"
        db.commit()
        if can_transition(wf.state, WS.EXPIRED.value):
            set_state(db, wf, WS.EXPIRED, "Contract expired")
        return report
    if report.material_change:
        price_only = report.offer_available and all(ch.startswith("Price changed") for ch in report.changes)
        if price_only and report.current_price is not None:
            delta = (report.current_price - terms.unit_price) * terms.quantity
            new_terms = terms.model_copy(update={
                "unit_price": report.current_price,
                "expected_total": money(terms.expected_total + delta) if terms.expected_total is not None else None,
            })
            revise_contract(db, wf, row, new_terms, f"Monitoring agent detected: {report.changes[0]}")
        else:
            _invalidate_approvals(db, wf, row, "INVALIDATED", "; ".join(report.changes))
            row.approval_status = "INVALIDATED"
            wf.blocked_reason = "Offer changed: " + "; ".join(report.changes)
            db.commit()
        if wf.state != WS.CONTRACT_CREATED.value and can_transition(wf.state, WS.CONTRACT_CREATED.value):
            set_state(db, wf, WS.CONTRACT_CREATED, "Offer changed; renewed review and approval required")
    return report


# =============================================================== checkout ====
def verify_checkout(db: Session, user_id: str, contract_id: str, scenario: str) -> CheckoutVerificationRow:
    if scenario not in SCENARIOS:
        raise ServiceError(f"Unknown scenario. Options: {', '.join(SCENARIOS)}")
    row, wf = owned_contract(db, user_id, contract_id)
    if wf.state not in CONTRACT_STATES:
        raise Conflict(f"Checkout cannot be verified while workflow is {wf.state}")
    if wf.autonomy_mode == "MONITORING_ONLY":
        raise Conflict("This workflow is monitoring-only; purchases are disabled.")
    report = monitor(db, wf, row)
    if report is None:
        raise Conflict("Offer monitoring failed; cannot verify against a possibly stale contract.")
    if report.expired:
        raise Conflict("Contract has expired.")
    if report.material_change:
        raise Conflict({"message": "The offer changed after approval. Review and re-approve the contract.",
                        "changes": report.changes, "current_version": row.version})
    if not active_approval(db, row):
        raise Conflict(f"Approve the current contract version (v{row.version}) before checkout verification.")
    set_state(db, wf, WS.CHECKOUT_VERIFICATION, f"Verifying final checkout (demo scenario: {scenario})")
    c = get_container()
    terms = ContractTerms.model_validate(row.terms)
    run = c.checkout().execute(wf.id, subject=row.id, contract=terms, contract_id=row.id, version=row.version,
                               contract_hash=row.contract_hash, scenario=scenario, now=utcnow())
    record_run(db, wf, run, "Checkout snapshot obtained" if run.ok else None)
    snap_row = CheckoutSnapshotRow(id=new_id("snap"), workflow_id=wf.id, contract_id=row.id, scenario=scenario)
    if run.ok:
        snapshot: CheckoutSnapshot = run.value["snapshot"]
        result = run.value["verification"]
        snap_row.merchant_checkout_id = snapshot.checkout_id
        snap_row.body = snapshot.model_dump(mode="json")
        conditions, status, reasons, explanation = result.conditions, result.status, result.reasons, result.explanation
    else:
        conditions = [ConditionResult(key="snapshot", label="Checkout snapshot", approved="available",
                                      observed="unavailable", result=Tri.UNKNOWN,
                                      reason="A supported checkout snapshot could not be obtained.")]
        status, reasons = Tri.UNKNOWN, [conditions[0].reason]
        explanation = "PAYMENT BLOCKED (UNKNOWN). The checkout could not be inspected."
    db.add(snap_row)
    db.flush()
    ver = CheckoutVerificationRow(id=new_id("ver"), workflow_id=wf.id, contract_id=row.id,
                                  contract_version=row.version, contract_hash=row.contract_hash,
                                  snapshot_id=snap_row.id, status=status.value,
                                  conditions=[x.model_dump(mode="json") for x in conditions], reasons=reasons,
                                  explanation=explanation)
    db.add(ver)
    wf.blocked_reason = None if status == Tri.PASS else explanation
    db.commit()
    emit(db, wf, "checkout.verified", f"Checkout verification: {status.value}", agent_id="checkout_agent",
         payload={"verification_id": ver.id, "status": status.value, "reasons": reasons})
    if status == Tri.PASS:
        set_state(db, wf, WS.PAYMENT_PENDING_APPROVAL, "Checkout matches the contract; PayPal approval required")
    else:
        set_state(db, wf, WS.CHECKOUT_BLOCKED, "Payment blocked by policy engine", payload={"status": status.value})
        emit(db, wf, "payment.blocked", explanation)
        find_alternatives(db, wf, terms.product_id)
    return ver


def find_alternatives(db: Session, wf: Workflow, exclude_product_id: str) -> list[dict]:
    c = get_container()
    emit(db, wf, "alternatives.search_started", "Searching for alternative offers that satisfy the contract terms",
         agent_id="discovery_agent")
    intent = ShoppingIntent.model_validate(wf.intent)
    evals = discover_and_evaluate(db, wf, c, intent, exclude=[exclude_product_id], mark_states=False) or []
    ranked = rank_eligible(evals)[:3]
    wf.alternatives = [{
        "product_id": e.candidate.id, "product_name": e.candidate.product_name, "variant": e.candidate.variant,
        "seller_name": e.seller.seller_name if e.seller else e.candidate.listing_seller_name,
        "total": money_str(e.cost.total), "currency": e.cost.currency, "evaluation": e.model_dump(mode="json"),
    } for e in ranked]
    db.commit()
    emit(db, wf, "alternatives.found", f"{len(ranked)} alternative offer(s) satisfy every mandatory condition",
         payload=[{k: v for k, v in a.items() if k != "evaluation"} for a in wf.alternatives])
    return wf.alternatives


def near_miss_suggestions(intent: ShoppingIntent, evaluations: list[CandidateEvaluation]) -> list[dict]:
    """Deterministic 'what would make this work?' hints for offers that fail on
    exactly one relaxable requirement. Never suggests allowing subscriptions or
    accepting unknown information."""
    import math
    import re

    out: dict[tuple, dict] = {}
    cur = intent.currency or ""
    for e in evaluations:
        if e.status == Tri.PASS:
            continue
        failing = [c for c in e.conditions if c.mandatory and c.result != Tri.PASS]
        if not failing or any(c.result == Tri.UNKNOWN for c in failing):
            continue  # unknown evidence is never "fixed" by relaxing the user's rules
        keys = {c.key for c in failing}
        name = e.candidate.product_name
        total = e.cost.total if e.cost else None
        price = f" ({money_str(total)} {cur} total)" if total is not None else ""
        s = None
        if keys == {"budget"} and total is not None and intent.max_budget is not None:
            new = Decimal(math.ceil(total / 10) * 10)
            sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(cur)
            shown = f"{sym}{new:,.0f}" if sym else f"{new:,.0f} {cur}"
            s = {"kind": "raise_budget", "max_budget": money_str(new),
                 "label": f"Raise budget to {shown}",
                 "detail": f"{name} meets every other requirement{price}.", "cost": float(new - intent.max_budget)}
        elif keys == {"specifications"}:
            gaps = e.candidate.gaps
            drop = sorted({m.group(1) for g in gaps if (m := re.match(r"([a-z_]+) (?:is|not listed)", g))})
            variant = any(g.startswith("variant ") for g in gaps)
            if drop or variant:
                parts = [g.split(",")[0] for g in gaps]
                s = {"kind": "relax_specs", "drop_specs": drop, "clear_variant": variant,
                     "label": "Accept " + "; ".join(parts),
                     "detail": f"{name} meets every other requirement{price}.", "cost": 100.0 * len(gaps)}
        elif keys == {"seller"} and e.seller and e.seller.evidence_level.value == "LIMITED_EVIDENCE":
            s = {"kind": "accept_limited_seller", "label": "Accept sellers with limited evidence",
                 "detail": f"{name} from {e.seller.seller_name}{price}. {e.seller.summary}", "cost": 150.0}
        elif keys == {"warranty"} and e.terms and e.terms.warranty_months:
            w = e.terms.warranty_months
            s = {"kind": "lower_warranty", "warranty_min_months": w, "label": f"Accept a {w}-month warranty",
                 "detail": f"{name} meets every other requirement{price}.", "cost": 120.0}
        if s:
            s["product_id"] = e.candidate.id
            s["product_name"] = name
            key = (s["kind"], s.get("max_budget"), tuple(s.get("drop_specs", [])), s.get("warranty_min_months"))
            if key not in out or s["cost"] < out[key]["cost"]:
                out[key] = s
    ranked, seen_budget = [], False
    for x in sorted(out.values(), key=lambda x: x["cost"]):
        if x["kind"] == "raise_budget":
            if seen_budget:
                continue  # only the smallest budget increase is worth suggesting
            seen_budget = True
        ranked.append(x)
    ranked = ranked[:3]
    for r in ranked:
        r.pop("cost", None)
    return ranked


def revise_workflow(db: Session, user_id: str, workflow_id: str, *, max_budget: str | None = None,
                    drop_specs: list[str] | None = None, clear_variant: bool = False,
                    accept_limited_seller: bool = False, warranty_min_months: int | None = None) -> Workflow:
    """Start a NEW workflow from an explicitly relaxed copy of the intent. The
    original workflow and its audit trail are left untouched."""
    from app.schemas.common import EvidenceLevel

    src = owned_workflow(db, user_id, workflow_id)
    if not src.intent:
        raise Conflict("This workflow has no parsed intent to revise")
    intent = ShoppingIntent.model_validate(src.intent)
    changes: list[str] = []
    if max_budget is not None:
        b = money(max_budget)
        if b is None or b <= 0 or b > Decimal("1000000"):
            raise ServiceError("max_budget must be between 0 and 1,000,000")
        changes.append(f"budget {intent.max_budget} -> {b} {intent.currency}")
        intent.max_budget = b
    for k in drop_specs or []:
        if k in intent.specifications:
            changes.append(f"dropped {k}={intent.specifications.pop(k)}")
    if clear_variant and intent.preferred_variant:
        changes.append(f"any variant (was {intent.preferred_variant})")
        intent.preferred_variant = None
    if accept_limited_seller:
        intent.seller_preferences.min_evidence_level = EvidenceLevel.LIMITED
        changes.append("accept sellers with limited evidence")
    if warranty_min_months is not None:
        if not 0 < warranty_min_months <= 120:
            raise ServiceError("warranty_min_months must be between 1 and 120")
        changes.append(f"warranty >= {warranty_min_months} months")
        intent.warranty_requirement.min_months = warranty_min_months
    if not changes:
        raise ServiceError("No change requested")
    intent.id = new_id("int")
    intent.clarifications_needed, intent.conflicts = [], []
    note = "; ".join(changes)
    wf = Workflow(id=new_id("wf"), user_id=user_id, state=WS.CREATED.value, autonomy_mode=src.autonomy_mode,
                  raw_request=f"{src.raw_request.splitlines()[0]} [revised: {note}]",
                  intent=intent.model_dump(mode="json"))
    db.add(wf)
    db.commit()
    emit(db, wf, "intent.created", f"Revised request from {src.id}", payload={"source_workflow": src.id,
                                                                            "changes": changes})
    set_state(db, wf, WS.INTENT_PARSED, f"Intent revised by you: {note}")
    emit(db, src, "intent.revised", f"You started a revised search ({wf.id}): {note}",
         payload={"revised_workflow": wf.id})
    return wf


def select_alternative(db: Session, user_id: str, workflow_id: str, product_id: str) -> PurchaseContract:
    wf = owned_workflow(db, user_id, workflow_id)
    if wf.state not in CONTRACT_STATES:
        raise Conflict(f"Cannot switch offers while workflow is {wf.state}")
    alt = next((a for a in (wf.alternatives or []) if a["product_id"] == product_id), None)
    if not alt:
        raise NotFound("Alternative not found")
    ev = CandidateEvaluation.model_validate(alt["evaluation"])
    intent = ShoppingIntent.model_validate(wf.intent)
    row = db.get(PurchaseContract, wf.current_contract_id)
    new_terms = build_contract_terms(intent, ev, wf.id, utcnow() + _ttl())
    revise_contract(db, wf, row, new_terms, f"You selected alternative offer {ev.candidate.product_name}")
    wf.evaluations = (wf.evaluations or []) + [alt["evaluation"]]
    wf.alternatives = []
    db.commit()
    if wf.state != WS.CONTRACT_CREATED.value:
        set_state(db, wf, WS.CONTRACT_CREATED, "New contract version ready for your approval")
    return row


# =============================================================== payments ====
def _auth(db: Session, wf: Workflow, row: PurchaseContract, ver: CheckoutVerificationRow | None,
          amount: Decimal, currency: str) -> engine.PaymentAuthorization:
    appr = active_approval(db, row)
    latest = latest_verification(db, row.id)
    ctx = engine.AuthorizationContext(
        workflow_state=wf.state, autonomy_mode=wf.autonomy_mode, contract_id=row.id, contract_version=row.version,
        contract_hash=row.contract_hash, contract_expires_at=row.expires_at,
        contract_max_total=money(row.terms["max_total"]), contract_currency=row.terms["currency"],
        approval_status=appr.status if appr else None, approval_hash=appr.contract_hash if appr else None,
        approval_version=appr.version if appr else None, verification_id=ver.id if ver else None,
        verification_status=ver.status if ver else None, verification_contract_hash=ver.contract_hash if ver else None,
        verification_contract_version=ver.contract_version if ver else None,
        is_latest_verification=bool(ver and latest and latest.id == ver.id), amount=amount, currency=currency,
        now=utcnow())
    return engine.authorize_payment(ctx, get_container().settings.authorization_secret)


def create_payment_order(db: Session, user_id: str, contract_id: str) -> PaymentTransaction:
    row, wf = owned_contract(db, user_id, contract_id)
    ver = latest_verification(db, row.id)
    if ver is None:
        raise Conflict("Checkout has not been verified")
    snap = CheckoutSnapshot.model_validate(db.get(CheckoutSnapshotRow, ver.snapshot_id).body or {}) \
        if ver.status == "PASS" else None
    amount = snap.stated_total if snap else Decimal("0")
    currency = snap.currency if snap else row.terms["currency"]
    auth = _auth(db, wf, row, ver, amount, currency)
    if not auth.allowed:
        emit(db, wf, "payment.blocked", "Payment authorization refused: " + "; ".join(auth.reasons))
        raise Forbidden({"message": "Payment not authorized by policy engine", "reasons": auth.reasons})

    existing = db.scalars(select(PaymentTransaction).where(PaymentTransaction.verification_id == ver.id,
                                                           PaymentTransaction.payment_status.in_(ACTIVE_PAYMENT))
                          .order_by(PaymentTransaction.created_at.desc())).first()
    if existing and (existing.paypal_order_id or existing.payment_status not in ("CREATING", "UNCERTAIN")):
        return existing  # idempotent: one live order per verified checkout
    if existing:
        p = existing  # retry an uncertain create with the SAME PayPal-Request-Id
    else:
        attempt = (db.scalar(select(func.count()).select_from(PaymentTransaction)
                             .where(PaymentTransaction.verification_id == ver.id)) or 0) + 1
        p = PaymentTransaction(
            id=new_id("pay"), workflow_id=wf.id, user_id=user_id, contract_id=row.id, contract_version=row.version,
            contract_hash=row.contract_hash, verification_id=ver.id, attempt=attempt,
            idempotency_key=f"cm-order-{ver.id}-a{attempt}", environment=get_container().paypal.environment,
            amount=money_str(amount), currency=currency, payment_status="CREATING")
        db.add(p)
        try:
            db.commit()
        except IntegrityError:
            # A concurrent request created the order row first: return that one (idempotent).
            db.rollback()
            winner = db.scalars(select(PaymentTransaction).where(
                PaymentTransaction.idempotency_key == f"cm-order-{ver.id}-a{attempt}")).first()
            if winner is None:
                raise Conflict("Concurrent payment request; please retry")
            return winner
    c = get_container()
    s = c.settings
    try:
        payload = build_order_payload(
            ContractTerms.model_validate(row.terms), snap, contract_id=row.id, version=row.version,
            invoice_id=f"CM-{p.id}",
            return_url=f"{s.frontend_url}/workflows/{wf.id}?payment={p.id}&paypal=approved",
            cancel_url=f"{s.frontend_url}/workflows/{wf.id}?payment={p.id}&paypal=cancelled")
    except ValueError as e:
        # Never send PayPal an order whose breakdown differs from the verified checkout.
        p.payment_status, p.error = "FAILED", str(e)
        db.commit()
        emit(db, wf, "payment.failed", f"Order not sent to PayPal: {e}", agent_id="payment_agent")
        if can_transition(wf.state, WS.PAYMENT_FAILED.value) and wf.state != WS.PAYMENT_FAILED.value:
            set_state(db, wf, WS.PAYMENT_FAILED, "Order breakdown did not match the verified checkout")
        return p
    out = c.payment().create_order(auth, payload, p.idempotency_key)
    if out.status == "APPROVAL_PENDING":
        p.paypal_order_id, p.approve_url, p.paypal_status = out.order_id, out.approve_url, out.paypal_status
        p.payment_status, p.error = "APPROVAL_PENDING", None
        db.commit()
        emit(db, wf, "payment.order_created", f"PayPal order {out.order_id} created for {p.amount} {p.currency}",
             agent_id="payment_agent", payload={"payment_id": p.id, "paypal_order_id": out.order_id,
                                                "environment": p.environment})
    elif out.status == "UNCERTAIN":
        p.payment_status, p.error = "UNCERTAIN", out.error
        db.commit()
        emit(db, wf, "payment.order_uncertain", "PayPal did not respond; the order will be retried with the same "
                                                "idempotency key", agent_id="payment_agent")
    else:
        p.payment_status, p.error = "FAILED", out.error
        db.commit()
        emit(db, wf, "payment.failed", f"Could not create PayPal order: {out.error}", agent_id="payment_agent")
        if can_transition(wf.state, WS.PAYMENT_FAILED.value) and wf.state != WS.PAYMENT_FAILED.value:
            set_state(db, wf, WS.PAYMENT_FAILED, "PayPal order creation failed")
    return p


def capture_payment(db: Session, user_id: str, payment_id: str) -> PaymentTransaction:
    p = owned_payment(db, user_id, payment_id)
    if p.payment_status == "CAPTURED":
        return p
    if p.payment_status in ("CANCELLED", "FAILED"):
        raise Conflict(f"Payment is {p.payment_status}")
    if not p.paypal_order_id:
        raise Conflict("PayPal order has not been created")
    row, wf = owned_contract(db, user_id, p.contract_id)
    ver = db.get(CheckoutVerificationRow, p.verification_id)
    auth = _auth(db, wf, row, ver, money(p.amount), p.currency)
    if not auth.allowed or row.contract_hash != p.contract_hash:
        reasons = auth.reasons or ["Contract changed after the order was created."]
        emit(db, wf, "payment.blocked", "Capture refused: " + "; ".join(reasons))
        raise Forbidden({"message": "Capture not authorized by policy engine", "reasons": reasons})
    claimed = db.execute(update(PaymentTransaction).where(
        PaymentTransaction.id == p.id,
        (PaymentTransaction.payment_status.in_(("APPROVAL_PENDING", "APPROVED", "UNCERTAIN", "PENDING"))
         | ((PaymentTransaction.payment_status == "CAPTURING")
            & (PaymentTransaction.updated_at < utcnow() - STALE_CAPTURE))))
        .values(payment_status="CAPTURING", updated_at=utcnow())).rowcount
    db.commit()
    db.refresh(p)
    if not claimed:
        if p.payment_status == "CAPTURED":
            return p
        raise Conflict("A capture for this payment is already in progress")
    try:
        out = get_container().payment().capture(auth, order_id=p.paypal_order_id, contract_hash=p.contract_hash,
                                                amount=p.amount, request_id=f"cm-capture-{p.id}")
    except Exception as e:  # noqa: BLE001 - never leave a payment stuck in CAPTURING
        out = PaymentOutcome(status="UNCERTAIN", order_id=p.paypal_order_id, error=f"{e.__class__.__name__}")
    return apply_capture_outcome(db, wf, p, out)


def apply_capture_outcome(db: Session, wf: Workflow, p: PaymentTransaction, out: PaymentOutcome,
                          source: str = "capture") -> PaymentTransaction:
    if out.status == "CAPTURED":
        p.payment_status, p.paypal_capture_id, p.paypal_status = "CAPTURED", out.capture_id, out.paypal_status
        p.captured_amount, p.captured_currency, p.error = out.amount, out.currency, None
        db.commit()
        emit(db, wf, "payment.captured", f"PayPal captured {out.amount} {out.currency} (capture {out.capture_id}, "
                                         f"via {source})", agent_id="payment_agent",
             payload={"payment_id": p.id, "capture_id": out.capture_id, "amount": out.amount,
                      "currency": out.currency})
        if can_transition(wf.state, WS.PAYMENT_CAPTURED.value):
            set_state(db, wf, WS.PAYMENT_CAPTURED, "Payment captured")
            reconcile_and_issue_passport(db, wf, p)
    elif out.status == "PENDING":
        p.payment_status, p.paypal_capture_id, p.paypal_status = "PENDING", out.capture_id, out.paypal_status
        db.commit()
        emit(db, wf, "payment.capture_pending", "PayPal reports the capture as PENDING; awaiting webhook",
             agent_id="payment_agent")
    elif out.status == "NOT_APPROVED":
        p.payment_status = "APPROVAL_PENDING"
        db.commit()
        raise Conflict("The buyer has not approved this order on PayPal yet.")
    elif out.status == "UNCERTAIN":
        p.payment_status, p.error = "UNCERTAIN", out.error
        db.commit()
        emit(db, wf, "payment.uncertain", "Capture outcome unknown; PayPal state will be re-queried before any "
                                          "retry (same idempotency key)", agent_id="payment_agent")
    else:
        p.payment_status, p.error = "FAILED", out.error
        db.commit()
        emit(db, wf, "payment.failed", f"Capture failed: {out.error}", agent_id="payment_agent")
        if can_transition(wf.state, WS.PAYMENT_FAILED.value) and wf.state != WS.PAYMENT_FAILED.value:
            set_state(db, wf, WS.PAYMENT_FAILED, "Payment failed; you may retry")
    return p


def cancel_payment(db: Session, user_id: str, payment_id: str) -> PaymentTransaction:
    p = owned_payment(db, user_id, payment_id)
    if p.payment_status not in ("CREATING", "APPROVAL_PENDING", "APPROVED"):
        raise Conflict(f"Payment in status {p.payment_status} cannot be cancelled")
    p.payment_status, p.error = "CANCELLED", "Cancelled by buyer before capture"
    db.commit()
    emit(db, db.get(Workflow, p.workflow_id), "payment.cancelled", "Payment cancelled before capture",
         payload={"payment_id": p.id})
    return p


# ========================================================= reconciliation ====
def reconcile_and_issue_passport(db: Session, wf: Workflow, p: PaymentTransaction) -> PurchasePassportRow:
    c = get_container()
    set_state(db, wf, WS.RECONCILIATION, "Reconciling PayPal record and merchant receipt")
    cv = db.scalars(select(ContractVersion).where(ContractVersion.contract_id == p.contract_id,
                                                  ContractVersion.version == p.contract_version)).first()
    terms = ContractTerms.model_validate(cv.terms)
    ver = db.get(CheckoutVerificationRow, p.verification_id)
    snap = CheckoutSnapshot.model_validate(db.get(CheckoutSnapshotRow, ver.snapshot_id).body)
    intent = ShoppingIntent.model_validate(wf.intent)
    run = c.reconciliation().execute(wf.id, subject=p.id, intent=intent, contract=terms, contract_id=p.contract_id,
                                     version=p.contract_version, snapshot=snap, order_id=p.paypal_order_id)
    report: ReconciliationReport = run.value if run.ok else ReconciliationReport(
        status="INCOMPLETE", checks=[], discrepancies=[], paypal_record={}, receipt=None,
        notes=[f"Reconciliation agent failed: {run.envelope.error}"])
    record_run(db, wf, run, f"Reconciliation: {report.status}" if run.ok else None)
    if report.status != "RECONCILED":
        wf.has_discrepancy = report.status == "DISCREPANCY"
        db.commit()
        set_state(db, wf, WS.RECONCILIATION_REQUIRED,
                  f"Reconciliation {report.status}: " + ("; ".join(d.description for d in report.discrepancies)
                                                         or "evidence incomplete"))
    passport = build_passport(db, wf, p, terms, ver, snap, intent, report)
    set_state(db, wf, WS.PASSPORT_GENERATED, "Purchase Truth Passport generated", payload={"passport_id": passport.id})
    emit(db, wf, "passport.generated", f"Passport {passport.id} issued", payload={"passport_hash": passport.passport_hash})
    set_state(db, wf, WS.COMPLETED, "Purchase lifecycle complete" + (" (with recorded discrepancies)"
                                                                     if wf.has_discrepancy else ""))
    return passport


def _selected_evaluation(wf: Workflow, product_id: str) -> dict | None:
    for e in reversed(wf.evaluations or []):
        if e["candidate"]["id"] == product_id:
            return e
    return None


def build_passport(db: Session, wf: Workflow, p: PaymentTransaction, terms: ContractTerms,
                   ver: CheckoutVerificationRow, snap: CheckoutSnapshot, intent: ShoppingIntent,
                   report: ReconciliationReport) -> PurchasePassportRow:
    row = db.get(PurchaseContract, p.contract_id)
    ev = _selected_evaluation(wf, terms.product_id) or {}
    approval = db.scalars(select(ContractApproval).where(ContractApproval.contract_id == row.id,
                                                         ContractApproval.version == p.contract_version)
                          .order_by(ContractApproval.created_at.desc())).first()
    history = [{"version": v.version, "contract_hash": v.contract_hash, "reason": v.change_reason,
                "created_at": v.created_at.isoformat()}
               for v in db.scalars(select(ContractVersion).where(ContractVersion.contract_id == row.id)
                                   .order_by(ContractVersion.version))]
    events = list_events(db, wf.id)
    chain_ok, _ = verify_chain(events)
    pid = new_id("pp")
    body = {
        "schema": "checkmate.purchase_truth_passport/v1",
        "passport_id": pid,
        "purchase_id": p.id,
        "workflow_id": wf.id,
        "generated_at": utcnow().isoformat(),
        "original_intent": {"raw_text": wf.raw_request, "structured": intent.model_dump(mode="json")},
        "selected_product": {"product_id": terms.product_id, "product_name": terms.product_name,
                             "brand": terms.brand, "model": terms.model, "variant": terms.exact_variant,
                             "source_reference": terms.source_reference},
        "seller_evidence": ev.get("seller"),
        "purchase_terms": ev.get("terms"),
        "cost_breakdown": {"pre_contract_assessment": ev.get("cost"),
                           "verified_checkout_lines": [l.model_dump(mode="json") for l in snap.line_items],
                           "verified_checkout_total": money_str(snap.stated_total), "currency": snap.currency},
        "contract": {"contract_id": row.id, "version": p.contract_version, "contract_hash": p.contract_hash,
                     "hash_algorithm": HASH_ALGORITHM, "terms": terms.model_dump(mode="json"),
                     "approved_at": approval.created_at.isoformat() if approval else None,
                     "approved_hash": approval.contract_hash if approval else None, "history": history},
        "checkout_verification": {"verification_id": ver.id, "result": ver.status, "conditions": ver.conditions,
                                  "explanation": ver.explanation, "snapshot": snap.model_dump(mode="json")},
        "payment": {"environment": p.environment, "paypal_order_id": p.paypal_order_id,
                    "paypal_capture_id": p.paypal_capture_id, "captured_amount": p.captured_amount,
                    "captured_currency": p.captured_currency, "status": p.payment_status,
                    "idempotency_key": p.idempotency_key},
        "receipt_evidence": report.receipt,
        "reconciliation": report.model_dump(mode="json"),
        "discrepancies": [d.model_dump() for d in report.discrepancies],
        "event_timeline": [{"seq": e.seq, "type": e.event_type, "agent": e.agent_id, "state": e.state,
                            "message": e.message, "at": e.ts, "hash": e.hash} for e in events],
        "audit_chain": {"head_hash": events[-1].hash if events else None, "events": len(events),
                        "verified": chain_ok},
    }
    passport = PurchasePassportRow(id=pid, workflow_id=wf.id, user_id=wf.user_id, body=to_jsonable(body),
                                   passport_hash=hash_obj(body))
    db.add(passport)
    db.commit()
    return passport


# =========================================================== workflow ops ====
def clarify(db: Session, user_id: str, workflow_id: str, text: str) -> Workflow:
    wf = owned_workflow(db, user_id, workflow_id)
    if wf.state != WS.NEEDS_CLARIFICATION.value:
        raise Conflict("Workflow is not awaiting clarification")
    wf.raw_request = f"{wf.raw_request}\n{text}"
    db.commit()
    emit(db, wf, "intent.clarification_received", "Clarification received")
    return wf


def retry(db: Session, user_id: str, workflow_id: str) -> Workflow:
    wf = owned_workflow(db, user_id, workflow_id)
    if wf.state != WS.EVIDENCE_INCOMPLETE.value:
        raise Conflict("Only workflows with incomplete evidence can be retried")
    return wf


def cancel(db: Session, user_id: str, workflow_id: str) -> Workflow:
    wf = owned_workflow(db, user_id, workflow_id)
    if WS(wf.state) in POST_CAPTURE or WS(wf.state) in TERMINAL:
        raise Conflict(f"Workflow in state {wf.state} cannot be cancelled")
    if wf.current_contract_id:
        _invalidate_approvals(db, wf, db.get(PurchaseContract, wf.current_contract_id), "REVOKED", "workflow cancelled")
    set_state(db, wf, WS.CANCELLED, "Workflow cancelled by user; all permissions revoked")
    return wf


def resume_incomplete() -> list[str]:
    """Called at startup: workflows interrupted mid-pipeline are resumed."""
    from app.orchestrator.state_machine import AUTOMATIC

    with SessionLocal() as db:
        ids = list(db.scalars(select(Workflow.id).where(Workflow.state.in_([s.value for s in AUTOMATIC]))))
    for wid in ids:
        run_pipeline(wid)
    return ids


# =============================================================== webhooks ====
def handle_paypal_webhook(db: Session, headers: dict[str, str], raw_body: bytes) -> dict:
    c = get_container()
    if not c.paypal.verify_webhook(headers, raw_body):
        log.warning("Rejected PayPal webhook with invalid signature")
        raise ServiceError("Invalid webhook signature", 400)
    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError as e:
        raise ServiceError("Malformed webhook body", 400) from e
    eid = event.get("id")
    if not eid:
        raise ServiceError("Webhook event id missing", 400)
    if db.get(WebhookEvent, eid):
        return {"status": "duplicate", "event_id": eid}
    resource = event.get("resource") or {}
    row = WebhookEvent(id=eid, event_type=event.get("event_type", "UNKNOWN"), resource_id=resource.get("id"),
                       payload=event)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"status": "duplicate", "event_id": eid}
    result = _process_webhook(db, event)
    row.processed, row.result = True, result
    db.commit()
    return {"status": "processed", "event_id": eid, "result": result}


def _process_webhook(db: Session, event: dict) -> str:
    etype = event.get("event_type", "")
    resource = event.get("resource") or {}
    if etype.startswith("CHECKOUT.ORDER"):
        order_id = resource.get("id")
    else:
        order_id = ((resource.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
    p = db.scalars(select(PaymentTransaction).where(PaymentTransaction.paypal_order_id == order_id)).first() \
        if order_id else None
    if not p:
        return "ignored: no matching order"
    wf = db.get(Workflow, p.workflow_id)
    emit(db, wf, "payment.webhook_received", f"Verified PayPal webhook {etype}",
         payload={"event_id": event.get("id"), "order_id": order_id})
    agent: PaymentAgent = c_payment()
    try:
        order = agent.get_order(order_id)  # never trust the webhook payload alone
    except (PayPalError, PayPalTimeout) as e:
        return f"deferred: could not confirm order state ({e.__class__.__name__})"
    if etype == "CHECKOUT.ORDER.APPROVED":
        if order.get("status") == "APPROVED" and p.payment_status == "APPROVAL_PENDING":
            p.payment_status = "APPROVED"
            db.commit()
        return f"order status {order.get('status')}"
    if etype in ("PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.PENDING"):
        if p.payment_status == "CAPTURED":
            return "already captured"
        out = PaymentAgent._from_order(order)
        if out.status in ("CAPTURED", "PENDING"):
            apply_capture_outcome(db, wf, p, out, source="webhook")
            return f"capture recorded: {out.status}"
        return f"order not completed ({order.get('status')})"
    if etype in ("PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.DECLINED"):
        if p.payment_status != "CAPTURED":
            apply_capture_outcome(db, wf, p, PaymentOutcome(status="FAILED", error=etype), source="webhook")
        return "capture denied"
    return "acknowledged"


def c_payment() -> PaymentAgent:
    return get_container().payment()


# ============================================================ mode change ====
def set_autonomy_mode(db: Session, user_id: str, workflow_id: str, mode: str) -> Workflow:
    """Monitoring-only workflows can be upgraded to an assisted purchase. The
    reverse (and any change after payment) is not allowed."""
    wf = owned_workflow(db, user_id, workflow_id)
    if mode not in AUTONOMY_MODES:
        raise ServiceError(f"autonomy_mode must be one of {AUTONOMY_MODES}")
    if wf.autonomy_mode == mode:
        return wf
    if mode != "ASSISTED":
        raise Conflict("A workflow can only be switched from monitoring to an assisted purchase.")
    if WS(wf.state) in POST_CAPTURE or WS(wf.state) in TERMINAL:
        raise Conflict(f"Mode cannot change while workflow is {wf.state}")
    old = wf.autonomy_mode
    wf.autonomy_mode = mode
    db.commit()
    emit(db, wf, "workflow.mode_changed", "You switched this workflow to an assisted purchase. Payment still "
                                          "needs your contract approval, a PASS checkout and PayPal approval.",
         payload={"from": old, "to": mode})
    return wf


# ================================================================== jobs =====
STALE_CAPTURE = timedelta(minutes=2)


def sweep_payments() -> list[str]:
    """Settles payments left CAPTURING (e.g. a crash mid-request), UNCERTAIN or
    PENDING by asking PayPal what actually happened. Never captures on its own."""
    from app.agents.payment_agent import PaymentAgent as _PA

    settled: list[str] = []
    with SessionLocal() as db:
        cutoff = utcnow() - STALE_CAPTURE
        rows = db.scalars(select(PaymentTransaction).where(
            PaymentTransaction.paypal_order_id.is_not(None),
            ((PaymentTransaction.payment_status == "CAPTURING") & (PaymentTransaction.updated_at < cutoff))
            | PaymentTransaction.payment_status.in_(("UNCERTAIN", "PENDING")))).all()
        for p in rows:
            wf = db.get(Workflow, p.workflow_id)
            try:
                order = get_container().payment().get_order(p.paypal_order_id)
            except (PayPalError, PayPalTimeout) as e:
                log.warning("sweeper could not query order %s: %s", p.paypal_order_id, e.__class__.__name__)
                continue
            status = order.get("status")
            out = _PA._from_order(order)
            if status == "COMPLETED" and out.status in ("CAPTURED", "PENDING"):
                if p.payment_status != out.status:
                    apply_capture_outcome(db, wf, p, out, source="payment sweeper")
                    settled.append(p.id)
            elif status == "APPROVED" and p.payment_status in ("CAPTURING", "UNCERTAIN"):
                p.payment_status, p.error = "APPROVED", "Capture did not reach PayPal; it can be completed safely."
                db.commit()
                emit(db, wf, "payment.recovered", "A capture interrupted before reaching PayPal was reset; you can "
                                                  "complete it again with the same idempotency key.",
                     agent_id="payment_agent")
                settled.append(p.id)
            elif status in ("VOIDED", "EXPIRED"):
                p.payment_status, p.error = "CANCELLED", f"PayPal order {status.lower()}"
                db.commit()
                emit(db, wf, "payment.cancelled", f"PayPal reports the order as {status.lower()}; no money moved.")
                settled.append(p.id)
    return settled


def monitor_active_contracts() -> list[str]:
    """Scheduled Monitoring Agent pass over every live contract (including
    monitoring-only workflows): detects expiry and offer changes."""
    checked: list[str] = []
    with SessionLocal() as db:
        wfs = db.scalars(select(Workflow).where(Workflow.state.in_(CONTRACT_STATES),
                                                Workflow.current_contract_id.is_not(None))).all()
        for wf in wfs:
            row = db.get(PurchaseContract, wf.current_contract_id)
            if row is None:
                continue
            try:
                monitor(db, wf, row)
                checked.append(wf.id)
            except Exception:  # noqa: BLE001 - one bad workflow must not stop the sweep
                log.exception("scheduled monitoring failed for %s", wf.id)
                db.rollback()
    return checked
