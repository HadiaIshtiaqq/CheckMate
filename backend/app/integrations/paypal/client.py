"""PayPal Orders v2 integration. Credentials stay on the backend. Two
implementations share one interface:

* PayPalSandboxClient - real PayPal Sandbox REST API (OAuth 2.0 client credentials).
* MockPayPalClient    - a local, clearly-labelled simulation used when no sandbox
                        credentials are configured, so the demo still runs end to end.
"""
from __future__ import annotations

import copy
import json
import secrets
import threading
import time
from typing import Any, Protocol

import httpx
from sqlalchemy.orm import sessionmaker

from app.core.config import PAYPAL_SANDBOX_BASE_URL
from app.core.util import constant_time_equals, hmac_hex, new_id, utcnow
from app.database.models import MockPayPalOrder, MockPayPalRequest


class PayPalError(Exception):
    def __init__(self, status: int, name: str, message: str, debug_id: str | None = None, details: Any = None):
        super().__init__(f"PayPal {status} {name}: {message}")
        self.status, self.name, self.message, self.debug_id, self.details = status, name, message, debug_id, details

    @property
    def issue(self) -> str | None:
        if isinstance(self.details, list) and self.details:
            return self.details[0].get("issue")
        return None


class PayPalTimeout(Exception):
    """The outcome of the call is UNKNOWN. Never treat as failure; query state first."""


class PayPalGateway(Protocol):
    environment: str

    def create_order(self, payload: dict, request_id: str) -> dict: ...
    def get_order(self, order_id: str) -> dict: ...
    def capture_order(self, order_id: str, request_id: str) -> dict: ...
    def refund_capture(self, capture_id: str, request_id: str, note: str | None = None) -> dict: ...
    def verify_webhook(self, headers: dict[str, str], raw_body: bytes) -> bool: ...


def approve_link(order: dict) -> str | None:
    for link in order.get("links", []):
        if link.get("rel") in ("payer-action", "approve"):
            return link.get("href")
    return None


def extract_capture(order: dict) -> dict | None:
    for unit in order.get("purchase_units", []):
        captures = (unit.get("payments") or {}).get("captures") or []
        if captures:
            return captures[0]
    return None


# ============================================================ real sandbox ===
class PayPalSandboxClient:
    environment = "sandbox"

    def __init__(self, client_id: str, client_secret: str, webhook_id: str | None, timeout: float = 20.0):
        self._client_id = client_id
        self._client_secret = client_secret
        self._webhook_id = webhook_id
        self._base = PAYPAL_SANDBOX_BASE_URL
        self._timeout = timeout
        self._token: str | None = None
        self._token_expiry = 0.0
        self._lock = threading.Lock()

    def _access_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._token_expiry - 60:
                return self._token
            try:
                r = httpx.post(
                    f"{self._base}/v1/oauth2/token",
                    auth=(self._client_id, self._client_secret),
                    data={"grant_type": "client_credentials"},
                    headers={"Accept": "application/json"},
                    timeout=self._timeout,
                )
            except httpx.TimeoutException as e:
                raise PayPalTimeout("OAuth token request timed out") from e
            if r.status_code != 200:
                raise PayPalError(r.status_code, "AUTHENTICATION_FAILURE", "Could not obtain PayPal access token")
            body = r.json()
            self._token = body["access_token"]
            self._token_expiry = time.time() + int(body.get("expires_in", 300))
            return self._token

    def _request(self, method: str, path: str, *, json_body: Any = None, request_id: str | None = None) -> dict:
        for attempt in (1, 2):
            headers = {
                "Authorization": f"Bearer {self._access_token(force=attempt == 2)}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            try:
                r = httpx.request(method, f"{self._base}{path}", json=json_body, headers=headers, timeout=self._timeout)
            except httpx.TimeoutException as e:
                raise PayPalTimeout(f"{method} {path} timed out") from e
            except httpx.TransportError as e:
                raise PayPalTimeout(f"{method} {path} transport error: {e.__class__.__name__}") from e
            if r.status_code == 401 and attempt == 1:
                continue
            if r.status_code >= 400:
                try:
                    err = r.json()
                except json.JSONDecodeError:
                    err = {}
                raise PayPalError(r.status_code, err.get("name", "ERROR"), err.get("message", r.text[:200]),
                                  err.get("debug_id"), err.get("details"))
            return r.json() if r.content else {}
        raise PayPalError(401, "AUTHENTICATION_FAILURE", "PayPal rejected credentials")

    def create_order(self, payload: dict, request_id: str) -> dict:
        return self._request("POST", "/v2/checkout/orders", json_body=payload, request_id=request_id)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    def capture_order(self, order_id: str, request_id: str) -> dict:
        return self._request("POST", f"/v2/checkout/orders/{order_id}/capture", json_body={}, request_id=request_id)

    def refund_capture(self, capture_id: str, request_id: str, note: str | None = None) -> dict:
        """Full refund of a capture (Payments v2). PayPal-Request-Id makes retries safe."""
        body = {"note_to_payer": note[:255]} if note else {}
        return self._request("POST", f"/v2/payments/captures/{capture_id}/refund", json_body=body,
                             request_id=request_id)

    def verify_webhook(self, headers: dict[str, str], raw_body: bytes) -> bool:
        if not self._webhook_id:
            return False  # without a configured webhook id nothing can be verified
        h = {k.lower(): v for k, v in headers.items()}
        required = ["paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id",
                    "paypal-transmission-sig", "paypal-transmission-time"]
        if any(k not in h for k in required):
            return False
        try:
            event = json.loads(raw_body)
        except json.JSONDecodeError:
            return False
        body = {
            "auth_algo": h["paypal-auth-algo"],
            "cert_url": h["paypal-cert-url"],
            "transmission_id": h["paypal-transmission-id"],
            "transmission_sig": h["paypal-transmission-sig"],
            "transmission_time": h["paypal-transmission-time"],
            "webhook_id": self._webhook_id,
            "webhook_event": event,
        }
        try:
            result = self._request("POST", "/v1/notifications/verify-webhook-signature", json_body=body)
        except (PayPalError, PayPalTimeout):
            return False
        return result.get("verification_status") == "SUCCESS"


# ================================================================ simulation ===
class MockPayPalClient:
    """Behaves like Orders v2 for the flows CHECKMATE uses, including
    PayPal-Request-Id idempotency and ORDER_NOT_APPROVED errors."""

    environment = "mock"

    def __init__(self, session_factory: sessionmaker, public_base_url: str, webhook_secret: str):
        self._sessions = session_factory
        self._public = public_base_url.rstrip("/")
        self._secret = webhook_secret
        self._lock = threading.Lock()
        # test hooks
        self.fail_next_capture_with_timeout = False
        self.decline_next_capture = False

    def _idempotent(self, db, request_id: str) -> dict | None:
        row = db.get(MockPayPalRequest, request_id)
        return row.response if row else None

    def create_order(self, payload: dict, request_id: str) -> dict:
        with self._lock, self._sessions() as db:
            prior = self._idempotent(db, request_id)
            if prior:
                return prior
            unit = payload["purchase_units"][0]
            if not unit.get("amount", {}).get("value"):
                raise PayPalError(422, "UNPROCESSABLE_ENTITY", "Amount is required")
            order_id = "MOCK" + new_id("o").split("_", 1)[1][:13].upper()
            approval_token = secrets.token_urlsafe(16)  # only the holder of the approval link can approve
            ctx = (payload.get("payment_source", {}).get("paypal", {}) or {}).get("experience_context", {})
            order = {
                "id": order_id,
                "status": "PAYER_ACTION_REQUIRED",
                "intent": payload.get("intent", "CAPTURE"),
                "purchase_units": [unit],
                "create_time": utcnow().isoformat(),
                "links": [
                    {"rel": "self", "href": f"{self._public}/mock-paypal/orders/{order_id}", "method": "GET"},
                    {"rel": "payer-action",
                     "href": f"{self._public}/mock-paypal/checkout?token={order_id}&ct={approval_token}",
                     "method": "GET"},
                ],
                "_return_url": ctx.get("return_url"),
                "_cancel_url": ctx.get("cancel_url"),
                "_approval_token": approval_token,
            }
            db.add(MockPayPalOrder(id=order_id, status=order["status"], body=order))
            db.add(MockPayPalRequest(request_id=request_id, response=self._public_view(order)))
            db.commit()
            return self._public_view(order)

    @staticmethod
    def _public_view(order: dict) -> dict:
        return {k: v for k, v in order.items() if not k.startswith("_")}

    def get_order(self, order_id: str) -> dict:
        with self._sessions() as db:
            row = db.get(MockPayPalOrder, order_id)
            if not row:
                raise PayPalError(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.")
            body = dict(row.body)
            body["status"] = row.status
            return self._public_view(body)

    def order_urls(self, order_id: str) -> tuple[str | None, str | None]:
        with self._sessions() as db:
            row = db.get(MockPayPalOrder, order_id)
            return (row.body.get("_return_url"), row.body.get("_cancel_url")) if row else (None, None)

    def approval_token_valid(self, order_id: str, presented: str | None) -> bool:
        with self._sessions() as db:
            row = db.get(MockPayPalOrder, order_id)
            expected = row.body.get("_approval_token") if row else None
        return bool(expected and presented and constant_time_equals(expected, presented))

    def buyer_decision(self, order_id: str, approve: bool) -> None:
        """Simulates the buyer approving/cancelling on the PayPal-hosted page."""
        with self._lock, self._sessions() as db:
            row = db.get(MockPayPalOrder, order_id)
            if not row:
                raise PayPalError(404, "RESOURCE_NOT_FOUND", "Unknown order")
            if row.status in ("PAYER_ACTION_REQUIRED", "CREATED"):
                row.status = "APPROVED" if approve else "VOIDED"
                body = dict(row.body)
                if approve:
                    body["payer"] = {"payer_id": "MOCKPAYER01", "email_address": "sandbox-buyer@example.test"}
                row.body = body
                db.commit()

    def capture_order(self, order_id: str, request_id: str) -> dict:
        with self._lock, self._sessions() as db:
            prior = self._idempotent(db, request_id)
            if prior:
                return prior
            row = db.get(MockPayPalOrder, order_id)
            if not row:
                raise PayPalError(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.")
            if row.status == "COMPLETED":
                raise PayPalError(422, "UNPROCESSABLE_ENTITY", "Order already captured.",
                                  details=[{"issue": "ORDER_ALREADY_CAPTURED"}])
            if row.status != "APPROVED":
                raise PayPalError(422, "UNPROCESSABLE_ENTITY", "Payer has not yet approved the Order for payment.",
                                  details=[{"issue": "ORDER_NOT_APPROVED"}])
            if self.decline_next_capture:
                self.decline_next_capture = False
                raise PayPalError(422, "UNPROCESSABLE_ENTITY", "The instrument presented was declined.",
                                  details=[{"issue": "INSTRUMENT_DECLINED"}])
            body = dict(row.body)
            unit = dict(body["purchase_units"][0])
            capture = {
                "id": "MOCKCAP" + new_id("c").split("_", 1)[1][:10].upper(),
                "status": "COMPLETED",
                "amount": dict(unit["amount"], breakdown=None),
                "final_capture": True,
                "create_time": utcnow().isoformat(),
            }
            capture["amount"].pop("breakdown", None)
            unit["payments"] = {"captures": [capture]}
            body["purchase_units"] = [unit]
            body["status"] = "COMPLETED"
            row.body, row.status = body, "COMPLETED"
            response = self._public_view(body)
            db.add(MockPayPalRequest(request_id=request_id, response=response))
            db.commit()
            if self.fail_next_capture_with_timeout:
                # The capture happened, but the response is "lost" - exactly the
                # case where a client must not assume failure.
                self.fail_next_capture_with_timeout = False
                raise PayPalTimeout("simulated network timeout after capture")
            return response

    def refund_capture(self, capture_id: str, request_id: str, note: str | None = None) -> dict:
        with self._lock, self._sessions() as db:
            prior = self._idempotent(db, request_id)
            if prior:
                return prior
            for row in db.query(MockPayPalOrder).all():
                caps = ((row.body.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or []
                cap = next((x for x in caps if x.get("id") == capture_id), None)
                if cap is None:
                    continue
                if row.body.get("refund"):
                    raise PayPalError(422, "UNPROCESSABLE_ENTITY", "Capture has already been fully refunded.",
                                      details=[{"issue": "CAPTURE_FULLY_REFUNDED"}])
                refund = {"id": "MOCKREF" + new_id("r").split("_", 1)[1][:10].upper(), "status": "COMPLETED",
                          "amount": dict(cap["amount"]), "note_to_payer": note, "create_time": utcnow().isoformat()}
                refund["links"] = [{"rel": "up", "method": "GET",
                                    "href": f"https://api-m.sandbox.paypal.com/v2/payments/captures/{capture_id}"}]
                units = copy.deepcopy(row.body.get("purchase_units") or [])
                for c in (units[0].get("payments") or {}).get("captures") or [] if units else []:
                    if c.get("id") == capture_id:
                        c["status"] = "REFUNDED"  # as PayPal reports a fully refunded capture
                row.body = {**row.body, "purchase_units": units, "refund": refund}
                db.add(MockPayPalRequest(request_id=request_id, response=refund))
                db.commit()
                return refund
            raise PayPalError(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.")

    # --- webhooks: HMAC-signed locally in simulation mode ---------------------
    def sign_webhook(self, raw_body: bytes) -> str:
        return hmac_hex(self._secret, raw_body.decode("utf-8"))

    def verify_webhook(self, headers: dict[str, str], raw_body: bytes) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        sig = h.get("x-checkmate-mock-signature")
        if not sig:
            return False
        return constant_time_equals(sig, self.sign_webhook(raw_body))
