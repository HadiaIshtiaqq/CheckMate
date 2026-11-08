from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.views import (
    contract_view,
    event_view,
    payment_view,
    verification_view,
    workflow_detail,
    workflow_summary,
)
from app.core.config import get_settings
from app.database.models import PurchasePassportRow, User, Workflow
from app.database.session import get_db
from app.integrations.merchants.catalog import SCENARIOS
from app.orchestrator import service as svc
from app.orchestrator.events import list_events, verify_chain
from app.api.locks import serialized_user
from app.security.auth import current_user
from app.services.container import get_container

router = APIRouter(prefix="/api/v1")


class IntentIn(BaseModel):
    text: str = Field(min_length=3, max_length=2000)
    autonomy_mode: str = "ASSISTED"


class ModeIn(BaseModel):
    autonomy_mode: str


class ClarifyIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class ApproveIn(BaseModel):
    contract_hash: str = Field(min_length=64, max_length=64)


class VerifyIn(BaseModel):
    scenario: str = "clean"


class OrderIn(BaseModel):
    contract_id: str


class ReviseIn(BaseModel):
    max_budget: str | None = Field(default=None, max_length=14)
    drop_specs: list[str] = Field(default_factory=list, max_length=10)
    clear_variant: bool = False
    accept_limited_seller: bool = False
    warranty_min_months: int | None = None


class SearchIn(BaseModel):
    query: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=50)


class OfferChangeIn(BaseModel):
    price: str | None = Field(default=None, max_length=12)


# ------------------------------------------------------------------ system ---
@router.get("/system")
def system_info():
    s = get_settings()
    llm = get_container().llm
    return {"paypal_environment": s.paypal_mode, "llm_enabled": llm.enabled, "llm_provider": llm.provider,
            "llm_model": llm.model if llm.enabled else None, "product_source": "demo-merchant",
            "channel3_enabled": False, "demo_controls_enabled": s.demo_controls_enabled,
            "scenarios": SCENARIOS}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "display_name": user.display_name}


# --------------------------------------------------------------- workflows ---
@router.post("/shopping/intents", status_code=202)
def submit_intent(body: IntentIn, bg: BackgroundTasks, user: User = Depends(serialized_user),
                  db: Session = Depends(get_db)):
    wf = svc.create_workflow(db, user.id, body.text.strip(), body.autonomy_mode)
    bg.add_task(svc.run_pipeline, wf.id)
    return {"workflow_id": wf.id, "state": wf.state}


@router.get("/workflows")
def list_workflows(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Workflow).where(Workflow.user_id == user.id).order_by(Workflow.created_at.desc())
                      .limit(100)).all()
    return [workflow_summary(w) for w in rows]


@router.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return workflow_detail(db, svc.owned_workflow(db, user.id, workflow_id))


@router.get("/workflows/{workflow_id}/events")
def get_events(workflow_id: str, after: int = Query(0, ge=0), user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    svc.owned_workflow(db, user.id, workflow_id)
    events = list_events(db, workflow_id)
    ok, err = verify_chain(events)
    return {"chain_verified": ok, "chain_error": err,
            "events": [event_view(e) for e in events if e.seq > after]}


@router.post("/workflows/{workflow_id}/clarify", status_code=202)
def clarify(workflow_id: str, body: ClarifyIn, bg: BackgroundTasks, user: User = Depends(serialized_user),
            db: Session = Depends(get_db)):
    wf = svc.clarify(db, user.id, workflow_id, body.text.strip())
    bg.add_task(svc.run_pipeline, wf.id)
    return {"workflow_id": wf.id, "state": wf.state}


@router.post("/workflows/{workflow_id}/retry", status_code=202)
def retry(workflow_id: str, bg: BackgroundTasks, user: User = Depends(serialized_user), db: Session = Depends(get_db)):
    wf = svc.retry(db, user.id, workflow_id)
    bg.add_task(svc.run_pipeline, wf.id)
    return {"workflow_id": wf.id, "state": wf.state}


@router.post("/workflows/{workflow_id}/revise", status_code=202)
def revise(workflow_id: str, body: ReviseIn, bg: BackgroundTasks, user: User = Depends(serialized_user),
           db: Session = Depends(get_db)):
    wf = svc.revise_workflow(db, user.id, workflow_id, max_budget=body.max_budget, drop_specs=body.drop_specs,
                             clear_variant=body.clear_variant, accept_limited_seller=body.accept_limited_seller,
                             warranty_min_months=body.warranty_min_months)
    bg.add_task(svc.run_pipeline, wf.id)
    return {"workflow_id": wf.id, "state": wf.state}


@router.post("/workflows/{workflow_id}/mode")
def set_mode(workflow_id: str, body: ModeIn, user: User = Depends(serialized_user), db: Session = Depends(get_db)):
    return workflow_summary(svc.set_autonomy_mode(db, user.id, workflow_id, body.autonomy_mode))


@router.post("/workflows/{workflow_id}/cancel")
def cancel(workflow_id: str, user: User = Depends(serialized_user), db: Session = Depends(get_db)):
    return workflow_summary(svc.cancel(db, user.id, workflow_id))


@router.post("/workflows/{workflow_id}/monitor")
def monitor(workflow_id: str, user: User = Depends(serialized_user), db: Session = Depends(get_db)):
    wf = svc.owned_workflow(db, user.id, workflow_id)
    if not wf.current_contract_id:
        raise svc.Conflict("No contract to monitor")
    row, _ = svc.owned_contract(db, user.id, wf.current_contract_id)
    if wf.state not in svc.CONTRACT_STATES:
        raise svc.Conflict(f"Nothing to monitor while workflow is {wf.state}")
    report = svc.monitor(db, wf, row)
    return report.model_dump(mode="json") if report else {"error": "monitoring failed"}


@router.post("/workflows/{workflow_id}/alternatives/{product_id}/select")
def select_alternative(workflow_id: str, product_id: str, user: User = Depends(serialized_user),
                       db: Session = Depends(get_db)):
    return contract_view(db, svc.select_alternative(db, user.id, workflow_id, product_id))


# ---------------------------------------------------------------- products ---
@router.post("/products/search")
def search_products(body: SearchIn, user: User = Depends(current_user)):
    return get_container().merchant.search_products(body.query, body.category)


@router.get("/products/{product_id}")
def get_product(product_id: str, user: User = Depends(current_user)):
    m = get_container().merchant
    p = m.get_product_details(product_id)
    if not p:
        raise svc.NotFound("Product not found")
    return {**p, "terms": m.get_purchase_terms(product_id), "seller": m.get_seller_information(p["seller_id"])}


# --------------------------------------------------------------- contracts ---
@router.get("/contracts/{contract_id}")
def get_contract(contract_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row, _ = svc.owned_contract(db, user.id, contract_id)
    return contract_view(db, row)


@router.post("/contracts/{contract_id}/approve")
def approve_contract(contract_id: str, body: ApproveIn, user: User = Depends(serialized_user),
                     db: Session = Depends(get_db)):
    svc.approve_contract(db, user.id, contract_id, body.contract_hash)
    row, _ = svc.owned_contract(db, user.id, contract_id)
    return contract_view(db, row)


@router.post("/contracts/{contract_id}/revoke")
def revoke_contract(contract_id: str, user: User = Depends(serialized_user), db: Session = Depends(get_db)):
    return contract_view(db, svc.revoke_contract(db, user.id, contract_id))


@router.post("/contracts/{contract_id}/verify")
def verify_contract(contract_id: str, body: VerifyIn, user: User = Depends(serialized_user),
                    db: Session = Depends(get_db)):
    return verification_view(db, svc.verify_checkout(db, user.id, contract_id, body.scenario))


# ---------------------------------------------------------------- payments ---
@router.post("/payments/orders")
def create_order(body: OrderIn, user: User = Depends(serialized_user), db: Session = Depends(get_db)):
    return payment_view(svc.create_payment_order(db, user.id, body.contract_id))


@router.post("/payments/{payment_id}/capture")
def capture(payment_id: str, user: User = Depends(serialized_user), db: Session = Depends(get_db)):
    return payment_view(svc.capture_payment(db, user.id, payment_id))


@router.post("/payments/{payment_id}/cancel")
def cancel_payment(payment_id: str, user: User = Depends(serialized_user), db: Session = Depends(get_db)):
    return payment_view(svc.cancel_payment(db, user.id, payment_id))


@router.get("/payments/{payment_id}")
def get_payment(payment_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return payment_view(svc.owned_payment(db, user.id, payment_id))


@router.post("/webhooks/paypal")
async def paypal_webhook(request: Request, db: Session = Depends(get_db)):
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > 256_000:
        raise svc.ServiceError("Payload too large", 413)
    raw = await request.body()
    if len(raw) > 256_000:
        raise svc.ServiceError("Payload too large", 413)
    return svc.handle_paypal_webhook(db, dict(request.headers), raw)


# --------------------------------------------------------------- passports ---
def _owned_passport(db: Session, user_id: str, passport_id: str) -> PurchasePassportRow:
    pp = db.get(PurchasePassportRow, passport_id)
    if not pp or pp.user_id != user_id:
        raise svc.NotFound("Passport not found")
    return pp


@router.get("/passports/{passport_id}")
def get_passport(passport_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    pp = _owned_passport(db, user.id, passport_id)
    return {"id": pp.id, "passport_hash": pp.passport_hash, "body": pp.body}


@router.get("/passports/{passport_id}/export")
def export_passport(passport_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    pp = _owned_passport(db, user.id, passport_id)
    return JSONResponse({"passport_hash": pp.passport_hash, "hash_algorithm": "sha256(canonical-json(body))",
                         "passport": pp.body},
                        headers={"Content-Disposition": f'attachment; filename="{pp.id}.json"'})


# -------------------------------------------------------------- demo knobs ---
@router.post("/demo/offers/{product_id}")
def change_offer(product_id: str, body: OfferChangeIn, user: User = Depends(serialized_user)):
    """Demo-only: simulate the merchant changing an offer's price so the
    Monitoring Agent has something to detect. Disabled in production."""
    if not get_settings().demo_controls_enabled:
        raise svc.Forbidden("Demo controls are disabled")
    m = get_container().merchant
    if not m.get_product_details(product_id):
        raise svc.NotFound("Product not found")
    m.set_price_override(product_id, body.price)
    return m.get_product_details(product_id)
