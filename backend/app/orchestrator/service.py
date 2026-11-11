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
from app.core.config import get_settings
from app.core.util import hash_obj, money, money_str, new_id, to_jsonable, utcnow
from app.database.models import (
    PurchasingMandate,
    ReceiptFile,
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
from app.integrations.paypal.client import PayPalError, PayPalTimeout, extract_capture
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

AUTONOMY_MODES = ("ASSISTED", "MONITORING_ONLY", "MANDATE")
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


class RateLimited(ServiceError):
    status_code = 429


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
def create_workflow(db: Session, user_id: str, text: str, autonomy_mode: str, market: str | None = None,
                    search_depth: str = "standard", source: str | None = None,
                    stores: list[str] | None = None) -> Workflow:
    from app.integrations.merchants.serpapi_shopping import DEPTHS, ENGINES, ENGINE_LABELS, MARKETS, market_engines

    if autonomy_mode not in AUTONOMY_MODES:
        raise ServiceError(f"autonomy_mode must be one of {AUTONOMY_MODES}")
    if market is not None and market not in MARKETS:
        raise ServiceError(f"market must be one of {', '.join(MARKETS)}")
    if search_depth not in DEPTHS:
        raise ServiceError(f"search_depth must be one of {', '.join(DEPTHS)}")
    if source not in (None, "stores", "partner"):
        raise ServiceError("source must be 'stores' or 'partner'")
    if stores is not None:
        unknown = [x for x in stores if x not in ENGINES]
        if unknown:
            raise ServiceError(f"stores must be chosen from {', '.join(ENGINES)}")
        stores = [e for e in ENGINES if e in stores]
        if not stores:
            raise ServiceError("Pick at least one store to search.")
        if market in MARKETS and not set(stores) & set(market_engines(market)):
            raise ServiceError(f"None of those stores can be searched in {MARKETS[market]['name']}; "
                               f"available: {', '.join(ENGINE_LABELS[e] for e in market_engines(market))}.")
    c = get_container()
    if source == "partner" and c.merchant is None:
        raise ServiceError("The PayPal partner merchant isn't enabled on this server.")
    if source == "stores" and c.external is None:
        raise ServiceError("Real-store search isn't connected on this server.")
    s = get_settings()
    order = list(DEPTHS)
    if s.max_search_depth in DEPTHS and order.index(search_depth) > order.index(s.max_search_depth):
        search_depth = s.max_search_depth  # deployment cap protects the shared search quota
    if s.requests_per_user_per_day and source != "partner":  # partner checkouts spend no search quota
        since = utcnow() - timedelta(days=1)
        used = db.scalar(select(func.count()).select_from(Workflow)
                         .where(Workflow.user_id == user_id, Workflow.created_at >= since,
                                (Workflow.source.is_(None)) | (Workflow.source != "partner"))) or 0
        if used >= s.requests_per_user_per_day:
            raise RateLimited(f"You've reached this demo's limit of {s.requests_per_user_per_day} shopping "
                              "requests per day. Please try again tomorrow.")
    wf = Workflow(id=new_id("wf"), user_id=user_id, state=WS.CREATED.value, autonomy_mode=autonomy_mode,
                  raw_request=text, market=market, search_depth=search_depth, source=source,
                  stores=",".join(stores) if stores else None)
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
        apply_market(intent, wf)
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


def apply_market(intent: ShoppingIntent, wf: Workflow) -> None:
    """Which country's stores to search, and the matching currency when the request didn't say."""
    from app.integrations.merchants.serpapi_shopping import MARKET_BY_CURRENCY, MARKETS

    intent.search_depth = wf.search_depth or "standard"
    intent.source = wf.source
    intent.stores = wf.stores.split(",") if wf.stores else None
    if wf.market in MARKETS:
        intent.market = wf.market
        cur = MARKETS[wf.market]["currency"]
        raw = wf.raw_request.lower()
        if intent.max_budget is not None and intent.currency is None:
            intent.currency = cur
            intent.clarifications_needed = [q for q in intent.clarifications_needed if "currency" not in q.lower()]
            intent.assumptions.append(f"Budget read as {cur} because you chose {MARKETS[wf.market]['name']} stores.")
        elif intent.currency == "USD" and cur in ("CAD", "AUD") and "usd" not in raw and "us$" not in raw:
            intent.currency = cur
            intent.assumptions.append(f"'$' read as {cur} because you chose {MARKETS[wf.market]['name']} stores.")
    else:
        intent.market = MARKET_BY_CURRENCY.get(intent.currency or "USD", "us")


def discover_and_evaluate(db: Session, wf: Workflow, c: Container, intent: ShoppingIntent, exclude: list[str],
                          mark_states: bool) -> list[CandidateEvaluation] | None:
    timeout = c.settings.agent_timeout_seconds
    disc = c.discovery().execute(wf.id, subject=intent.category or intent.product_description, intent=intent,
                                 exclude_product_ids=exclude)
    n = len(disc.value or [])
    record_run(db, wf, disc, f"{n} candidate product(s) found" if disc.ok else None)
    if not disc.ok or not disc.value:
        if mark_states:
            err = disc.envelope.error or ""
            wf.blocked_reason = (err.split(": ", 1)[1] if err.startswith("ProductSourceError") else
                                 f"No listings were found for \"{intent.product_description}\" in the connected "
                                 f"stores." if disc.ok else "Product search failed; please try again.")
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
    row = create_contract(db, wf, run.value)
    set_state(db, wf, WS.CONTRACT_CREATED, "Purchase contract ready for your approval")
    if wf.autonomy_mode == "MANDATE":
        apply_mandate(db, wf, row)


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
    return record_approval(db, wf, row, user_id, "user", f"You approved contract v{row.version}")


def approval_payload(a: ContractApproval) -> dict:
    return {"approval_id": a.id, "contract_id": a.contract_id, "version": a.version, "contract_hash": a.contract_hash,
            "user_id": a.user_id, "source": a.source or "user", "approved_at": a.created_at.isoformat()}


def record_approval(db: Session, wf: Workflow, row: PurchaseContract, user_id: str, source: str,
                    message: str) -> ContractApproval:
    """Creates an approval bound to the exact contract version + hash, signed with
    the server key (kept outside the database) so it can't be forged by DB edits."""
    from app.security.signing import sign

    a = ContractApproval(id=new_id("apv"), contract_id=row.id, version=row.version, contract_hash=row.contract_hash,
                         user_id=user_id, source=source, created_at=utcnow())
    a.signature = sign(approval_payload(a))
    row.approval_status = "APPROVED"
    db.add(a)
    db.commit()
    emit(db, wf, "contract.approved", message, payload={"contract_id": row.id, "version": row.version,
                                                        "contract_hash": row.contract_hash, "source": source})
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
    if row.terms.get("purchase_channel") == "external_store":
        raise Conflict("This is a real store's offer. Its checkout can't be inspected by CHECKMATE: confirm the "
                       "checklist on the store's page, buy there, then report your purchase.")
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
        external = e.candidate.external
        failing = [c for c in e.conditions if c.mandatory and
                   (c.result == Tri.FAIL if external else c.result != Tri.PASS)]
        if not failing or any(c.result == Tri.UNKNOWN for c in failing):
            continue  # unknown evidence is never "fixed" by relaxing the user's rules
        keys = {c.key for c in failing}
        name = e.candidate.product_name
        total = (e.cost.total or (e.cost.known_subtotal if external else None)) if e.cost else None
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


def select_alternative(db: Session, user_id: str, workflow_id: str, product_id: str,
                       override: bool = False) -> PurchaseContract:
    wf = owned_workflow(db, user_id, workflow_id)
    if wf.state not in CONTRACT_STATES and wf.state != WS.EVIDENCE_INCOMPLETE.value:
        raise Conflict(f"Cannot switch offers while workflow is {wf.state}")
    alt = next((a for a in (wf.alternatives or []) if a["product_id"] == product_id), None)
    ev_json = alt["evaluation"] if alt else next(
        (e for e in reversed(wf.evaluations or []) if e["candidate"]["id"] == product_id), None)
    if not ev_json:
        raise NotFound("Offer not found")
    ev = CandidateEvaluation.model_validate(ev_json)
    failed = [f"{c.label}: {c.reason}" for c in ev.conditions if c.mandatory and c.result == Tri.FAIL]
    if not ev.candidate.external and ev.status != Tri.PASS:
        raise Conflict("The demo store's checkout is enforced against your rules; only fully passing offers can "
                       "be chosen there.")
    if failed and not override:
        raise Conflict({"message": "This offer breaks some of your rules. Confirm to choose it anyway.",
                        "reasons": failed})
    intent = ShoppingIntent.model_validate(wf.intent)
    row = db.get(PurchaseContract, wf.current_contract_id) if wf.current_contract_id else None
    new_terms = build_contract_terms(intent, ev, wf.id, utcnow() + _ttl(), overrides=failed)
    if failed and new_terms.max_total is not None and ev.cost and ev.cost.known_subtotal is not None \
            and ev.cost.known_subtotal > new_terms.max_total:
        # Choosing an offer above budget raises this plan's limit to its listed cost; recorded as an override.
        new_terms = new_terms.model_copy(update={"max_total": ev.cost.known_subtotal})
    reason = f"You chose {ev.candidate.product_name}" + (
        f", overriding {len(failed)} rule(s)" if failed else "")
    if row is None:  # nothing qualified automatically: your choice creates the plan
        row = create_contract(db, wf, new_terms)
    else:
        revise_contract(db, wf, row, new_terms, reason)
    if failed:
        emit(db, wf, "contract.rules_overridden", "You chose this offer despite: " + "; ".join(failed),
             payload={"overridden": failed})
    if alt:
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
    if row.terms.get("purchase_channel") == "external_store":
        raise Forbidden("Real-store purchases are paid on the store's own site, not through CHECKMATE.")
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


def refund_payment(db: Session, user_id: str, payment_id: str, reason: str | None = None) -> PaymentTransaction:
    """Full PayPal refund of a captured payment, e.g. when reconciliation finds the merchant's
    receipt doesn't match what was paid, or the buyer returns the item."""
    p = owned_payment(db, user_id, payment_id)
    if p.payment_status != "CAPTURED" or not p.paypal_capture_id:
        raise Conflict("Only a captured PayPal payment can be refunded")
    if p.refund_status == "COMPLETED":
        raise Conflict("This payment has already been refunded")
    wf = db.get(Workflow, p.workflow_id)
    why = (reason or "").strip()[:200] or "Refund requested by the buyer"
    try:
        r = get_container().paypal.refund_capture(p.paypal_capture_id, request_id=f"refund-{p.id}",
                                                  note=f"CHECKMATE: {why}")
    except PayPalTimeout:
        raise Conflict("PayPal didn't answer in time. The refund may still go through; try again to check.")
    except PayPalError as e:
        if e.issue == "CAPTURE_FULLY_REFUNDED":
            p.refund_status = "COMPLETED"
            db.commit()
            return p
        raise Conflict(f"PayPal refused the refund: {e.message}")
    amt = r.get("amount") or {}
    p.refund_id, p.refund_status = r.get("id"), r.get("status") or "PENDING"
    p.refunded_amount = amt.get("value") or p.captured_amount
    p.refund_reason = why
    db.commit()
    emit(db, wf, "payment.refunded",
         f"PayPal refund {p.refund_status.lower()}: {p.refunded_amount} {amt.get('currency_code') or p.captured_currency}",
         payload={"payment_id": p.id, "refund_id": p.refund_id, "status": p.refund_status, "reason": why})
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
                     "approved_hash": approval.contract_hash if approval else None, "history": history,
                     "approval": ({**approval_payload(approval), "signature": approval.signature}
                                  if approval else None)},
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
    body = seal_passport(body)
    passport = PurchasePassportRow(id=pid, workflow_id=wf.id, user_id=wf.user_id, body=body,
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
    if not p and etype == "PAYMENT.CAPTURE.REFUNDED":  # a refund resource points "up" to its capture
        up = next((l.get("href", "") for l in resource.get("links") or [] if l.get("rel") == "up"), "")
        capture_id = up.rstrip("/").rsplit("/", 1)[-1] if "/captures/" in up else None
        p = db.scalars(select(PaymentTransaction).where(PaymentTransaction.paypal_capture_id == capture_id)).first() \
            if capture_id else None
        order_id = p.paypal_order_id if p else None
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
    if etype == "PAYMENT.CAPTURE.REFUNDED":
        if p.refund_status == "COMPLETED":
            return "already refunded"
        cap = extract_capture(order) or {}
        if cap.get("status") not in ("REFUNDED", "PARTIALLY_REFUNDED"):
            return f"capture not refunded ({cap.get('status')})"  # never trust the webhook payload alone
        amt = resource.get("amount") or {}
        p.refund_id = resource.get("id") or p.refund_id
        p.refund_status = "COMPLETED"
        p.refunded_amount = amt.get("value") or p.captured_amount
        p.refund_reason = p.refund_reason or "Refunded in PayPal"
        db.commit()
        emit(db, wf, "payment.refunded",
             f"PayPal refund completed: {p.refunded_amount} {amt.get('currency_code') or p.captured_currency}",
             payload={"payment_id": p.id, "refund_id": p.refund_id, "status": "COMPLETED", "source": "webhook"})
        return "refund recorded"
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


# ============================================================ store purchase ====
def report_store_purchase(db: Session, user_id: str, contract_id: str, *, amount: str, currency: str,
                          confirmations: dict[str, bool], order_reference: str | None) -> Workflow:
    """Real-store channel: the shopper bought on the store's site and reports what they
    paid and confirmed. CHECKMATE reconciles that against the approved plan."""
    row, wf = owned_contract(db, user_id, contract_id)
    terms = ContractTerms.model_validate(row.terms)
    if terms.purchase_channel != "external_store":
        raise Conflict("Purchases from the demo store are paid through PayPal in CHECKMATE.")
    if wf.state != WS.CONTRACT_CREATED.value:
        raise Conflict(f"A purchase can't be reported while the workflow is {wf.state}")
    if utcnow() >= row.expires_at:
        raise Conflict("This purchase plan has expired. Search again for current prices.")
    if not active_approval(db, row):
        raise Conflict(f"Approve the purchase plan (v{row.version}) before reporting a purchase.")
    amt = money(amount) if amount.replace(".", "", 1).isdigit() else None
    if amt is None or amt <= 0 or amt > Decimal("1000000"):
        raise ServiceError("Enter the amount you paid, e.g. 97.50")
    known = {i["key"] for i in terms.checklist}
    confirmations = {k: bool(v) for k, v in confirmations.items() if k in known}
    report = {"amount": money_str(amt), "currency": currency, "confirmations": confirmations,
              "order_reference": (order_reference or "").strip() or None,
              "receipt_file": latest_receipt(db, row.id)}
    emit(db, wf, "purchase.reported", f"You reported buying at {terms.store_name} for {money_str(amt)} {currency}",
         payload=report)
    set_state(db, wf, WS.RECONCILIATION, "Reconciling your store purchase against the approved plan")
    c = get_container()
    intent = ShoppingIntent.model_validate(wf.intent)
    run = c.reconciliation().execute(wf.id, subject=row.id, intent=intent, contract=terms, contract_id=row.id,
                                     version=row.version, store_report=report)
    rec: ReconciliationReport = run.value if run.ok else ReconciliationReport(
        status="INCOMPLETE", checks=[], discrepancies=[], paypal_record={}, receipt=None,
        notes=[f"Reconciliation agent failed: {run.envelope.error}"])
    record_run(db, wf, run, f"Reconciliation: {rec.status}" if run.ok else None)
    if rec.status != "RECONCILED":
        wf.has_discrepancy = rec.status == "DISCREPANCY"
        db.commit()
        set_state(db, wf, WS.RECONCILIATION_REQUIRED, f"Reconciliation {rec.status}: " + (
            "; ".join(d.description for d in rec.discrepancies) or "some checklist items were not confirmed"))
    passport = build_store_passport(db, wf, row, terms, intent, rec, report)
    set_state(db, wf, WS.PASSPORT_GENERATED, "Purchase Truth Passport generated", payload={"passport_id": passport.id})
    emit(db, wf, "passport.generated", f"Passport {passport.id} issued", payload={"passport_hash": passport.passport_hash})
    set_state(db, wf, WS.COMPLETED, "Purchase lifecycle complete" + (" (with recorded discrepancies)"
                                                                     if wf.has_discrepancy else ""))
    return wf


def build_store_passport(db: Session, wf: Workflow, row: PurchaseContract, terms: ContractTerms,
                         intent: ShoppingIntent, rec: ReconciliationReport, report: dict) -> PurchasePassportRow:
    ev = _selected_evaluation(wf, terms.product_id) or {}
    approval = db.scalars(select(ContractApproval).where(ContractApproval.contract_id == row.id,
                                                         ContractApproval.version == row.version)
                          .order_by(ContractApproval.created_at.desc())).first()
    history = [{"version": v.version, "contract_hash": v.contract_hash, "reason": v.change_reason,
                "created_at": v.created_at.isoformat()}
               for v in db.scalars(select(ContractVersion).where(ContractVersion.contract_id == row.id)
                                   .order_by(ContractVersion.version))]
    events = list_events(db, wf.id)
    chain_ok, _ = verify_chain(events)
    confirm_checks = [c.model_dump(mode="json") for c in rec.checks if c.key.startswith("confirm_")]
    pid = new_id("pp")
    body = {
        "schema": "checkmate.purchase_truth_passport/v1",
        "passport_id": pid,
        "purchase_id": f"store:{row.id}:v{row.version}",
        "workflow_id": wf.id,
        "purchase_channel": "external_store",
        "generated_at": utcnow().isoformat(),
        "original_intent": {"raw_text": wf.raw_request, "structured": intent.model_dump(mode="json")},
        "selected_product": {"product_id": terms.product_id, "product_name": terms.product_name,
                             "brand": terms.brand, "model": terms.model, "variant": terms.exact_variant,
                             "source_reference": terms.source_reference, "store": terms.store_name,
                             "listing_seen_at": terms.price_as_of, "thumbnail": (ev.get("candidate") or {}).get("thumbnail")},
        "seller_evidence": ev.get("seller"),
        "purchase_terms": ev.get("terms"),
        "cost_breakdown": {"pre_contract_assessment": ev.get("cost"),
                           "listed_price": money_str(terms.unit_price), "listed_price_as_of": terms.price_as_of,
                           "verified_checkout_lines": [], "verified_checkout_total": report["amount"],
                           "currency": report["currency"], "total_source": "self-reported"},
        "contract": {"contract_id": row.id, "version": row.version, "contract_hash": row.contract_hash,
                     "hash_algorithm": HASH_ALGORITHM, "terms": terms.model_dump(mode="json"),
                     "approved_at": approval.created_at.isoformat() if approval else None,
                     "approved_hash": approval.contract_hash if approval else None, "history": history,
                     "approval": ({**approval_payload(approval), "signature": approval.signature}
                                  if approval else None)},
        "checkout_verification": {
            "result": "CONFIRMED_BY_YOU" if all(c["result"] == "PASS" for c in confirm_checks) else
                      ("NOT_MET" if any(c["result"] == "FAIL" for c in confirm_checks) else "INCOMPLETE"),
            "conditions": confirm_checks,
            "explanation": "Real stores' checkouts can't be inspected by CHECKMATE; these items were confirmed by "
                           "you on the store's page."},
        "payment": {"environment": "external_store", "channel": "external_store", "store": terms.store_name,
                    "store_url": terms.store_url, "captured_amount": report["amount"],
                    "captured_currency": report["currency"], "order_reference": report.get("order_reference"),
                    "status": "REPORTED_BY_YOU", "paypal_order_id": None, "paypal_capture_id": None,
                    "source": "Self-reported by you; CHECKMATE did not process this payment."},
        "receipt_evidence": rec.receipt,
        "reconciliation": rec.model_dump(mode="json"),
        "discrepancies": [d.model_dump() for d in rec.discrepancies],
        "event_timeline": [{"seq": e.seq, "type": e.event_type, "agent": e.agent_id, "state": e.state,
                            "message": e.message, "at": e.ts, "hash": e.hash} for e in events],
        "audit_chain": {"head_hash": events[-1].hash if events else None, "events": len(events),
                        "verified": chain_ok},
    }
    body = seal_passport(body)
    passport = PurchasePassportRow(id=pid, workflow_id=wf.id, user_id=wf.user_id, body=body,
                                   passport_hash=hash_obj(body))
    db.add(passport)
    db.commit()
    return passport


# ================================================================= sealing ====
def seal_passport(body: dict) -> dict:
    """Signs the passport and the audit-chain head with the server key."""
    from app.security.signing import key_id, sign

    clean = to_jsonable(body)
    head = {"workflow_id": clean["workflow_id"], "head_hash": clean["audit_chain"]["head_hash"]}
    clean["seal"] = {"algorithm": "HMAC-SHA256 with a server key kept outside the database", "key_id": key_id(),
                     "signature": sign(clean), "audit_head_signature": sign(head)}
    return clean


def verify_passport(db: Session, user_id: str, passport_id: str) -> dict:
    from app.security.signing import verify

    pp = db.get(PurchasePassportRow, passport_id)
    if not pp or pp.user_id != user_id:
        raise NotFound("Passport not found")
    body = dict(pp.body)
    seal = body.pop("seal", None) or {}
    events = list_events(db, pp.workflow_id)
    chain_ok, chain_err = verify_chain(events)
    head = (body.get("audit_chain") or {}).get("head_hash")
    approval = (body.get("contract") or {}).get("approval")
    approval_ok = None
    if approval:
        sig = approval.get("signature")
        approval_ok = verify({k: v for k, v in approval.items() if k != "signature"}, sig)
    result = {
        "passport_hash_matches": hash_obj(pp.body) == pp.passport_hash,
        "passport_signature_valid": verify(body, seal.get("signature")),
        "audit_head_signature_valid": verify({"workflow_id": body.get("workflow_id"), "head_hash": head},
                                             seal.get("audit_head_signature")),
        "audit_chain_intact": chain_ok,
        "audit_head_present_in_log": any(e.hash == head for e in events),
        "approval_signature_valid": approval_ok,
        "key_id": seal.get("key_id"),
        "detail": chain_err,
    }
    result["valid"] = all(v is not False for k, v in result.items() if k not in ("key_id", "detail"))
    return result


# ================================================================ mandates ====
def create_mandate(db: Session, user_id: str, *, max_per_purchase: str, monthly_cap: str, currency: str,
                   allowed_stores: list[str], allowed_keywords: list[str], require_established_store: bool,
                   days: int) -> PurchasingMandate:
    per, cap = money(max_per_purchase), money(monthly_cap)
    if per is None or cap is None or per <= 0 or cap <= 0:
        raise ServiceError("Limits must be positive amounts")
    if per > cap:
        raise ServiceError("The per-purchase limit can't exceed the monthly cap")
    if not 1 <= days <= 90:
        raise ServiceError("A mandate can last 1 to 90 days")
    m = PurchasingMandate(id=new_id("mdt"), user_id=user_id, max_per_purchase=money_str(per),
                          monthly_cap=money_str(cap), currency=currency.upper()[:3],
                          allowed_stores=[x.strip() for x in allowed_stores if x.strip()][:20],
                          allowed_keywords=[x.strip().lower() for x in allowed_keywords if x.strip()][:20],
                          require_established_store=require_established_store,
                          expires_at=utcnow() + timedelta(days=days))
    db.add(m)
    db.commit()
    return m


def month_spend(db: Session, user_id: str, currency: str) -> Decimal:
    start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = Decimal("0")
    for pp in db.scalars(select(PurchasePassportRow).where(PurchasePassportRow.user_id == user_id,
                                                           PurchasePassportRow.created_at >= start)):
        pay = (pp.body or {}).get("payment") or {}
        if pay.get("captured_currency") == currency and pay.get("captured_amount"):
            total += Decimal(pay["captured_amount"])
    return money(total)


def revoke_mandate(db: Session, user_id: str, mandate_id: str) -> PurchasingMandate:
    m = db.get(PurchasingMandate, mandate_id)
    if not m or m.user_id != user_id:
        raise NotFound("Mandate not found")
    if m.status == "REVOKED":
        return m
    m.status, m.revoked_at = "REVOKED", utcnow()
    db.commit()
    for a in db.scalars(select(ContractApproval).where(ContractApproval.source == f"mandate:{m.id}",
                                                       ContractApproval.status == "ACTIVE")).all():
        row = db.get(PurchaseContract, a.contract_id)
        wf = db.get(Workflow, row.workflow_id)
        if WS(wf.state) in POST_CAPTURE or WS(wf.state) in TERMINAL:
            continue
        a.status, a.reason, a.ended_at = "REVOKED", "mandate revoked", utcnow()
        row.approval_status = "REVOKED"
        db.commit()
        emit(db, wf, "contract.approval_revoked", "Approval withdrawn: the mandate that granted it was revoked")
        if wf.state in (WS.PAYMENT_PENDING_APPROVAL.value, WS.CHECKOUT_BLOCKED.value, WS.PAYMENT_FAILED.value):
            set_state(db, wf, WS.CONTRACT_CREATED, "Mandate revoked; your approval is needed")
    return m


def apply_mandate(db: Session, wf: Workflow, row: PurchaseContract) -> PurchasingMandate | None:
    """Mode B: approve the contract on the user's behalf if an active mandate covers it.
    Payment still needs PayPal buyer approval (demo store) or the user buying at the store."""
    terms = ContractTerms.model_validate(row.terms)
    now = utcnow()
    mandates = db.scalars(select(PurchasingMandate).where(
        PurchasingMandate.user_id == wf.user_id, PurchasingMandate.status == "ACTIVE",
        PurchasingMandate.currency == terms.currency, PurchasingMandate.expires_at > now)).all()
    if not mandates:
        emit(db, wf, "mandate.not_applied", "No active mandate in this currency; your approval is needed.")
        return None
    ev = _selected_evaluation(wf, terms.product_id) or {}
    level = ((ev.get("seller") or {}).get("evidence_level"))
    store = (terms.store_name or terms.seller_name or "").lower()
    text_blob = f"{terms.product_name} {(wf.intent or {}).get('product_description', '')}".lower()
    spent = month_spend(db, wf.user_id, terms.currency)
    reasons: list[str] = []
    for m in mandates:
        why = []
        if terms.max_total > money(m.max_per_purchase):
            why.append(f"max total {terms.max_total} is above the per-purchase limit {m.max_per_purchase}")
        if spent + terms.max_total > money(m.monthly_cap):
            why.append(f"would exceed the monthly cap ({spent} already spent of {m.monthly_cap})")
        if terms.subscription_allowed:
            why.append("mandates never cover subscriptions")
        if m.allowed_stores and not any(x.lower() in store for x in m.allowed_stores):
            why.append(f"store '{terms.store_name or terms.seller_name}' is not in the mandate's store list")
        if m.allowed_keywords and not any(k in text_blob for k in m.allowed_keywords):
            why.append("product is outside the mandate's categories")
        if m.require_established_store and level != "STRONG_EVIDENCE":
            why.append("seller evidence is not strong")
        if not why:
            record_approval(db, wf, row, wf.user_id, f"mandate:{m.id}",
                            f"Approved automatically under your mandate (limit {m.max_per_purchase} {m.currency}, "
                            f"{spent} of {m.monthly_cap} used this month)")
            return m
        reasons.append("; ".join(why))
    emit(db, wf, "mandate.not_applied", "Your mandate doesn't cover this purchase: " + " | ".join(reasons),
         payload={"reasons": reasons})
    return None


# ============================================================ refresh offer ====
def refresh_offer(db: Session, user_id: str, workflow_id: str) -> dict:
    """Re-checks a real store's listing price (uses one product search)."""
    wf = owned_workflow(db, user_id, workflow_id)
    if wf.state != WS.CONTRACT_CREATED.value or not wf.current_contract_id:
        raise Conflict("Prices can be refreshed while a purchase plan is open")
    row = db.get(PurchaseContract, wf.current_contract_id)
    terms = ContractTerms.model_validate(row.terms)
    c = get_container()
    if terms.purchase_channel != "external_store" or c.external is None:
        raise Conflict("Only real-store offers can be refreshed")
    fresh = c.external.refresh_listing(terms.product_id)
    if fresh is None:
        emit(db, wf, "offer.refreshed", f"The listing no longer appears in Google Shopping results for "
                                        f"{terms.store_name}; it may be sold out or delisted.", agent_id="monitoring_agent")
        return {"found": False}
    price = money(fresh["price"])
    if fresh.get("currency") != terms.currency or price == terms.unit_price:
        emit(db, wf, "offer.refreshed", f"Price re-checked: still {price} {fresh.get('currency')}",
             agent_id="monitoring_agent")
        return {"found": True, "changed": False, "price": money_str(price)}
    ship = (fresh.get("terms") or {}).get("shipping_cost")
    known = money(price * terms.quantity + (money(ship) if ship is not None else Decimal("0")))
    new_terms = terms.model_copy(update={"unit_price": price, "known_subtotal": known,
                                         "price_as_of": fresh.get("retrieved_at")})
    revise_contract(db, wf, row, new_terms, f"Price refreshed from {terms.unit_price} to {price}")
    return {"found": True, "changed": True, "price": money_str(price)}


# ================================================================= receipts ====
RECEIPT_TYPES = {"application/pdf": (b"%PDF",), "image/png": (b"\x89PNG",), "image/jpeg": (b"\xff\xd8\xff",),
                 "image/webp": (b"RIFF",)}


def save_receipt(db: Session, user_id: str, contract_id: str, filename: str, content_type: str,
                 data: bytes) -> ReceiptFile:
    import hashlib

    row, wf = owned_contract(db, user_id, contract_id)
    if row.terms.get("purchase_channel") != "external_store":
        raise Conflict("Receipts are attached to real-store purchases")
    if wf.state != WS.CONTRACT_CREATED.value:
        raise Conflict("Attach the receipt before reporting the purchase")
    limit = get_container().settings.receipt_max_bytes
    if not data or len(data) > limit:
        raise ServiceError(f"Receipt must be between 1 byte and {limit // (1024 * 1024)} MB")
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in RECEIPT_TYPES or not any(data.startswith(m) for m in RECEIPT_TYPES[ctype]):
        raise ServiceError("Receipt must be a PDF, PNG, JPEG or WebP file")
    digest = hashlib.sha256(data).hexdigest()
    rf = ReceiptFile(id=new_id("rcf"), contract_id=row.id, user_id=user_id,
                     filename=(filename or "receipt")[:200], content_type=ctype, size=len(data), sha256=digest, data=data)
    db.add(rf)
    db.commit()
    emit(db, wf, "receipt.uploaded", f"Receipt attached ({rf.filename}, {len(data) // 1024} KB)",
         payload={"receipt_id": rf.id, "sha256": digest, "size": len(data)})
    return rf


def latest_receipt(db: Session, contract_id: str) -> dict | None:
    rf = db.scalars(select(ReceiptFile).where(ReceiptFile.contract_id == contract_id)
                    .order_by(ReceiptFile.created_at.desc())).first()
    if not rf:
        return None
    return {"receipt_id": rf.id, "filename": rf.filename, "content_type": rf.content_type, "size": rf.size,
            "sha256": rf.sha256, "uploaded_at": rf.created_at.isoformat()}
