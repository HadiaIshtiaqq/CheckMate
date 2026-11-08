import pytest
from sqlalchemy import select

from app.agents.base import ToolPermissionError
from app.database.models import AgentRun, Workflow
from app.database.session import SessionLocal
from app.orchestrator import service as svc
from app.schemas.common import AgentEnvelope
from app.services.container import get_container

from tests.conftest import A, approve, get_wf, start, verify


def test_pipeline_produces_contract_and_typed_agent_runs(client):
    wf = start(client)
    assert wf["state"] == "CONTRACT_CREATED"
    assert wf["contract"]["terms"]["product_id"] == "hp-aurora-x"
    assert wf["contract"]["terms"]["expected_total"] == "105.00"
    agents = {r["agent_id"] for r in wf["agent_runs"]}
    assert {"intent_agent", "discovery_agent", "seller_trust_agent", "terms_agent", "cost_auditor",
            "contract_agent"} <= agents
    with SessionLocal() as db:
        for r in db.scalars(select(AgentRun).where(AgentRun.workflow_id == wf["id"])):
            AgentEnvelope(workflow_id=wf["id"], agent_id=r.agent_id, timestamp=r.started_at, status=r.status,
                          result=r.output)  # outputs validate against the envelope schema


def test_parallel_agents_do_not_overwrite_each_other(client):
    wf = start(client)
    ids = [e["candidate"]["id"] for e in wf["evaluations"]]
    assert len(ids) == len(set(ids))
    for e in wf["evaluations"]:
        assert e["terms"]["product_id"] == e["candidate"]["id"]
        assert e["cost"]["product_id"] == e["candidate"]["id"]
        assert e["seller"]["seller_id"] == e["candidate"]["seller_id"]


def test_missing_evidence_preserved_and_never_passes(client):
    wf = start(client)
    tempo = next(e for e in wf["evaluations"] if e["candidate"]["id"] == "hp-tempo-lite")
    assert tempo["terms"]["warranty_months"] is None
    assert "warranty_months" in tempo["terms"]["unknown_fields"]
    assert tempo["cost"]["total"] is None
    assert tempo["status"] != "PASS"


def test_events_are_hash_chained(client):
    wf = start(client)
    ev = client.get(f"/api/v1/workflows/{wf['id']}/events", headers=A).json()
    assert ev["chain_verified"] is True
    assert ev["events"][0]["type"] == "intent.created"
    types = [e["type"] for e in ev["events"]]
    for t in ("intent.parsed", "product.discovered", "seller.assessed", "terms.extracted", "cost.calculated",
              "contract.created"):
        assert t in types


def test_tamper_detected(client):
    from app.database.models import WorkflowEvent
    from app.orchestrator.events import list_events, verify_chain

    wf = start(client)
    with SessionLocal() as db:
        e = db.scalars(select(WorkflowEvent).where(WorkflowEvent.workflow_id == wf["id"], WorkflowEvent.seq == 3)).one()
        e.message = "tampered"
        db.commit()
        ok, err = verify_chain(list_events(db, wf["id"]))
    assert not ok and "seq 3" in err


def test_failed_agent_does_not_produce_passing_result(client, monkeypatch):
    c = get_container()
    orig = c.registry._tools["merchant.terms"]

    def boom(_):
        raise RuntimeError("merchant terms service down")

    monkeypatch.setitem(c.registry._tools, "merchant.terms", boom)
    try:
        wf = start(client)
    finally:
        c.registry._tools["merchant.terms"] = orig
    assert wf["state"] == "EVIDENCE_INCOMPLETE"
    assert wf["contract"] is None
    assert all(e["status"] != "PASS" for e in wf["evaluations"])
    assert any(r["status"] == "ERROR" and r["agent_id"] == "terms_agent" for r in wf["agent_runs"])


def test_agent_tool_permissions_enforced():
    c = get_container()
    with pytest.raises(ToolPermissionError):
        c.checkout().tools.call("paypal.capture_order", "X", "Y")
    with pytest.raises(ToolPermissionError):
        c.discovery().tools.call("merchant.create_checkout", "hp-aurora-x", "Black", 1, "clean")
    with pytest.raises(ToolPermissionError):
        c.contract().tools.call("catalog.search", "x", None)
    with pytest.raises(ToolPermissionError):
        c.terms().tools.call("paypal.create_order", {}, "r")


def test_workflow_resumes_after_worker_restart(client):
    wf = start(client)
    with SessionLocal() as db:
        row = db.get(Workflow, wf["id"])
        # simulate a crash: a fresh workflow left mid-discovery
        clone = Workflow(id=wf["id"] + "x", user_id="alice", state="DISCOVERING", autonomy_mode="ASSISTED",
                         raw_request=row.raw_request, intent=row.intent)
        db.add(clone)
        db.commit()
    svc.resume_incomplete()
    resumed = get_wf(client, wf["id"] + "x")
    assert resumed["state"] == "CONTRACT_CREATED"


def test_clarification_flow(client):
    r = client.post("/api/v1/shopping/intents", json={"text": "Find wireless headphones under 110"}, headers=A)
    wf = get_wf(client, r.json()["workflow_id"])
    assert wf["state"] == "NEEDS_CLARIFICATION"
    r = client.post(f"/api/v1/workflows/{wf['id']}/clarify", json={"text": "USD please, free returns"}, headers=A)
    assert r.status_code == 202
    assert get_wf(client, wf["id"])["state"] == "CONTRACT_CREATED"


def test_laptop_request(client):
    wf = start(client, "Find a laptop under $900 with 16GB RAM, 512GB SSD and at least a one-year warranty.")
    assert wf["state"] == "CONTRACT_CREATED"
    assert wf["contract"]["terms"]["product_id"] == "lt-nimbus-14-air"  # cheapest qualifying: 780.03


def test_near_miss_suggestions_and_revision(client):
    wf = start(client, "Find a laptop under $700 with 16GB RAM, 512GB SSD and at least a one-year warranty.")
    assert wf["state"] == "EVIDENCE_INCOMPLETE"
    s = wf["suggestions"]
    assert s[0]["kind"] == "raise_budget" and s[0]["max_budget"] == "790.00"
    assert s[0]["product_id"] == "lt-nimbus-14-air"
    assert any(x["kind"] == "relax_specs" and x["product_id"] == "lt-orbit-13" for x in s) is False  # 737.86 > 700
    r = client.post(f"/api/v1/workflows/{wf['id']}/revise", json={"max_budget": s[0]["max_budget"]}, headers=A)
    assert r.status_code == 202
    new = get_wf(client, r.json()["workflow_id"])
    assert new["state"] == "CONTRACT_CREATED"
    assert new["contract"]["terms"]["max_total"] == "790.00"
    assert new["intent"]["specifications"] == {"ram_gb": "16", "storage_gb": "512"}  # nothing else relaxed
    assert get_wf(client, wf["id"])["state"] == "EVIDENCE_INCOMPLETE"  # original untouched


def test_spec_relaxation_suggestion(client):
    wf = start(client, "Find a laptop under $760 with 32GB RAM and at least a one-year warranty.")
    assert wf["state"] == "EVIDENCE_INCOMPLETE"
    relax = next(x for x in wf["suggestions"] if x["kind"] == "relax_specs")
    assert relax["drop_specs"] == ["ram_gb"]
    r = client.post(f"/api/v1/workflows/{wf['id']}/revise", json={"drop_specs": ["ram_gb"]}, headers=A)
    assert get_wf(client, r.json()["workflow_id"])["state"] == "CONTRACT_CREATED"


def test_revise_rejects_bad_input_and_foreign_users(client):
    wf = start(client, "Find a laptop under $700 with 16GB RAM, 512GB SSD and at least a one-year warranty.")
    assert client.post(f"/api/v1/workflows/{wf['id']}/revise", json={}, headers=A).status_code == 400
    assert client.post(f"/api/v1/workflows/{wf['id']}/revise", json={"max_budget": "-5"},
                       headers=A).status_code == 400
    from tests.conftest import B
    assert client.post(f"/api/v1/workflows/{wf['id']}/revise", json={"max_budget": "900"},
                       headers=B).status_code == 404


def test_material_change_invalidates_approval_and_requires_reapproval(client):
    wf = start(client)
    approve(client, wf)
    c = get_container()
    c.merchant.set_price_override("hp-aurora-x", "92.00")
    try:
        r = verify(client, wf["contract"]["id"])
        assert r.status_code == 409
        wf2 = get_wf(client, wf["id"])
        assert wf2["contract"]["version"] == 2
        assert wf2["contract"]["approved"] is False
        assert wf2["contract"]["terms"]["unit_price"] == "92.00"
        assert any(a["status"] == "INVALIDATED" for a in wf2["contract"]["approvals"])
        # the stale hash can no longer be approved
        r = client.post(f"/api/v1/contracts/{wf['contract']['id']}/approve",
                        json={"contract_hash": wf["contract"]["contract_hash"]}, headers=A)
        assert r.status_code == 409
        approve(client, wf2)
        assert verify(client, wf["contract"]["id"]).json()["status"] == "PASS"  # 92 + 10 + 5.12 = 107.12
    finally:
        c.merchant.set_price_override("hp-aurora-x", None)


def test_price_rise_beyond_budget_blocks_checkout(client):
    wf = start(client)
    c = get_container()
    c.merchant.set_price_override("hp-aurora-x", "95.00")
    try:
        assert verify(client, wf["contract"]["id"]).status_code == 409  # monitoring revises -> v2
        approve(client, get_wf(client, wf["id"]))
        v = verify(client, wf["contract"]["id"]).json()
        assert v["status"] == "FAIL"  # 95 + 10 + 5.28 = 110.28 > 110
        assert any(x["key"] == "budget" and x["result"] == "FAIL" for x in v["conditions"])
    finally:
        c.merchant.set_price_override("hp-aurora-x", None)


def test_verification_requires_approval(client):
    wf = start(client)
    r = verify(client, wf["contract"]["id"])
    assert r.status_code == 409 and "Approve" in r.json()["detail"]
