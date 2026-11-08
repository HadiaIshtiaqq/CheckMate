import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="checkmate-test-")
os.environ.update(
    DATABASE_URL=f"sqlite:///{Path(_tmp, 'test.db').as_posix()}",
    PAYPAL_MODE="mock",
    PAYPAL_CLIENT_ID="",
    PAYPAL_CLIENT_SECRET="",
    OPENAI_API_KEY="",
    GEMINI_API_KEYS="",
    GEMINI_API_KEY="",
    GOOGLE_API_KEY="",
    LLM_PROVIDER="auto",
    APP_ENV="development",
    JOBS_ENABLED="false",
    CHECKMATE_DEMO_USERS="alice:tok-alice,bob:tok-bob",
    RATE_LIMIT_PER_MINUTE="1000000",
    FRONTEND_URL="http://frontend.test",
    BACKEND_PUBLIC_URL="http://backend.test",
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

HEADPHONES = ("Find wireless headphones under $110. They must have at least a one-year warranty, free returns, "
              "no subscription, and come from a reliable seller.")
A = {"Authorization": "Bearer tok-alice"}
B = {"Authorization": "Bearer tok-bob"}


@pytest.fixture(scope="session")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def paypal():
    from app.services.container import get_container

    return get_container().paypal


def start(client, text=HEADPHONES, headers=A, mode="ASSISTED") -> dict:
    r = client.post("/api/v1/shopping/intents", json={"text": text, "autonomy_mode": mode}, headers=headers)
    assert r.status_code == 202, r.text
    return get_wf(client, r.json()["workflow_id"], headers)


def get_wf(client, wf_id, headers=A) -> dict:
    r = client.get(f"/api/v1/workflows/{wf_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def approve(client, wf: dict, headers=A) -> dict:
    c = wf["contract"]
    r = client.post(f"/api/v1/contracts/{c['id']}/approve", json={"contract_hash": c["contract_hash"]},
                    headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def verify(client, contract_id: str, scenario="clean", headers=A):
    return client.post(f"/api/v1/contracts/{contract_id}/verify", json={"scenario": scenario}, headers=headers)


def create_order(client, contract_id: str, headers=A):
    return client.post("/api/v1/payments/orders", json={"contract_id": contract_id}, headers=headers)


def ready_to_pay(client, scenario="clean") -> tuple[dict, dict]:
    """Workflow with an approved contract and a PASSing verification."""
    wf = start(client)
    approve(client, wf)
    r = verify(client, wf["contract"]["id"], scenario)
    assert r.status_code == 200, r.text
    return get_wf(client, wf["id"]), r.json()


def paid(client, paypal, scenario="clean") -> tuple[dict, dict]:
    wf, _ = ready_to_pay(client, scenario)
    order = create_order(client, wf["contract"]["id"]).json()
    paypal.buyer_decision(order["paypal_order_id"], True)
    r = client.post(f"/api/v1/payments/{order['id']}/capture", headers=A)
    assert r.status_code == 200, r.text
    return get_wf(client, wf["id"]), r.json()
