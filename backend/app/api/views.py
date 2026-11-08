"""Response shaping. Nothing here ever includes credentials or secrets."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    AgentRun,
    CheckoutSnapshotRow,
    CheckoutVerificationRow,
    ContractApproval,
    ContractVersion,
    PaymentTransaction,
    PurchaseContract,
    PurchasePassportRow,
    Workflow,
    WorkflowEvent,
)
from app.orchestrator.service import active_approval, latest_receipt, latest_verification, near_miss_suggestions
from app.schemas.domain import CandidateEvaluation, ShoppingIntent


def event_view(e: WorkflowEvent) -> dict:
    return {"seq": e.seq, "type": e.event_type, "agent_id": e.agent_id, "state": e.state, "message": e.message,
            "payload": e.payload, "at": e.ts, "hash": e.hash}


def payment_view(p: PaymentTransaction) -> dict:
    return {"id": p.id, "workflow_id": p.workflow_id, "contract_id": p.contract_id,
            "contract_version": p.contract_version, "verification_id": p.verification_id, "environment": p.environment,
            "paypal_order_id": p.paypal_order_id, "paypal_capture_id": p.paypal_capture_id,
            "approve_url": p.approve_url, "amount": p.amount, "currency": p.currency,
            "captured_amount": p.captured_amount, "captured_currency": p.captured_currency,
            "payment_status": p.payment_status, "paypal_status": p.paypal_status, "idempotency_key": p.idempotency_key,
            "error": p.error, "created_at": p.created_at.isoformat(), "updated_at": p.updated_at.isoformat()}


def contract_view(db: Session, row: PurchaseContract) -> dict:
    appr = active_approval(db, row)
    versions = db.scalars(select(ContractVersion).where(ContractVersion.contract_id == row.id)
                          .order_by(ContractVersion.version)).all()
    approvals = db.scalars(select(ContractApproval).where(ContractApproval.contract_id == row.id)
                           .order_by(ContractApproval.created_at)).all()
    return {
        "id": row.id, "workflow_id": row.workflow_id, "version": row.version, "contract_hash": row.contract_hash,
        "approval_status": row.approval_status, "approved": appr is not None, "expires_at": row.expires_at.isoformat(),
        "terms": row.terms,
        "versions": [{"version": v.version, "contract_hash": v.contract_hash, "reason": v.change_reason,
                      "created_at": v.created_at.isoformat()} for v in versions],
        "approvals": [{"id": a.id, "version": a.version, "contract_hash": a.contract_hash, "status": a.status,
                       "reason": a.reason, "source": a.source or "user", "signed": bool(a.signature),
                       "created_at": a.created_at.isoformat()} for a in approvals],
        "approved_by": (appr.source or "user") if appr else None,
        "receipt": latest_receipt(db, row.id),
    }


def verification_view(db: Session, v: CheckoutVerificationRow) -> dict:
    snap = db.get(CheckoutSnapshotRow, v.snapshot_id)
    return {"id": v.id, "contract_id": v.contract_id, "contract_version": v.contract_version,
            "contract_hash": v.contract_hash, "status": v.status, "conditions": v.conditions, "reasons": v.reasons,
            "explanation": v.explanation, "scenario": snap.scenario if snap else None,
            "checkout_snapshot": snap.body if snap else None, "created_at": v.created_at.isoformat()}


def workflow_summary(wf: Workflow) -> dict:
    intent = wf.intent or {}
    return {"id": wf.id, "state": wf.state, "autonomy_mode": wf.autonomy_mode, "request": wf.raw_request,
            "product_description": intent.get("product_description"), "max_budget": intent.get("max_budget"),
            "currency": intent.get("currency"), "has_discrepancy": wf.has_discrepancy,
            "blocked_reason": wf.blocked_reason, "created_at": wf.created_at.isoformat(),
            "updated_at": wf.updated_at.isoformat()}


def workflow_detail(db: Session, wf: Workflow) -> dict:
    out = workflow_summary(wf)
    out["intent"] = wf.intent
    out["evaluations"] = wf.evaluations or []
    out["alternatives"] = [{k: v for k, v in a.items() if k != "evaluation"} for a in (wf.alternatives or [])]
    out["error"] = wf.error
    out["suggestions"] = []
    if wf.state == "EVIDENCE_INCOMPLETE" and wf.intent and wf.evaluations:
        try:
            out["suggestions"] = near_miss_suggestions(
                ShoppingIntent.model_validate(wf.intent),
                [CandidateEvaluation.model_validate(e) for e in wf.evaluations])
        except Exception:  # suggestions are a convenience; never break the page
            out["suggestions"] = []
    contract = db.get(PurchaseContract, wf.current_contract_id) if wf.current_contract_id else None
    out["contract"] = contract_view(db, contract) if contract else None
    v = latest_verification(db, contract.id) if contract else None
    out["verification"] = verification_view(db, v) if v else None
    pays = db.scalars(select(PaymentTransaction).where(PaymentTransaction.workflow_id == wf.id)
                      .order_by(PaymentTransaction.created_at.desc())).all()
    out["payments"] = [payment_view(p) for p in pays]
    pp = db.scalars(select(PurchasePassportRow).where(PurchasePassportRow.workflow_id == wf.id)).first()
    out["passport_id"] = pp.id if pp else None
    runs = db.scalars(select(AgentRun).where(AgentRun.workflow_id == wf.id).order_by(AgentRun.started_at)).all()
    out["agent_runs"] = [{"id": r.id, "agent_id": r.agent_id, "subject": r.subject, "status": r.status,
                          "missing_information": r.missing_information, "confidence": r.confidence,
                          "error": r.error, "evidence": r.evidence, "duration_ms": r.duration_ms,
                          "started_at": r.started_at.isoformat()} for r in runs]
    return out
