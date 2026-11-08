"""Persistence model. Every transaction-related record links to its workflow and
the contract version it was produced against."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.util import utcnow
from app.database.session import Base, UTCDateTime


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Workflow(Base):
    __tablename__ = "workflows"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    state: Mapped[str] = mapped_column(String(40), index=True)
    autonomy_mode: Mapped[str] = mapped_column(String(20), default="ASSISTED")
    raw_request: Mapped[str] = mapped_column(Text)
    intent: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    evaluations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    alternatives: Mapped[list | None] = mapped_column(JSON, nullable=True)
    current_contract_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_discrepancy: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class WorkflowEvent(Base):
    """Append-only, hash-chained event log (tamper-evident audit trail)."""

    __tablename__ = "workflow_events"
    __table_args__ = (UniqueConstraint("workflow_id", "seq", name="uq_event_seq"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ts: Mapped[str] = mapped_column(String(40))
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), index=True)
    agent_id: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    output: Mapped[Any] = mapped_column(JSON, nullable=True)
    evidence: Mapped[list | None] = mapped_column(JSON, nullable=True)
    missing_information: Mapped[list | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int] = mapped_column(Integer)


class PurchaseContract(Base):
    __tablename__ = "purchase_contracts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    contract_hash: Mapped[str] = mapped_column(String(64))
    terms: Mapped[dict] = mapped_column(JSON)
    approval_status: Mapped[str] = mapped_column(String(20), default="PENDING")
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ContractVersion(Base):
    __tablename__ = "contract_versions"
    __table_args__ = (UniqueConstraint("contract_id", "version", name="uq_contract_version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("purchase_contracts.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    contract_hash: Mapped[str] = mapped_column(String(64))
    terms: Mapped[dict] = mapped_column(JSON)
    change_reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ContractApproval(Base):
    __tablename__ = "contract_approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("purchase_contracts.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    contract_hash: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE | INVALIDATED | REVOKED
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class CheckoutSnapshotRow(Base):
    __tablename__ = "checkout_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), index=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("purchase_contracts.id"), index=True)
    merchant_checkout_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scenario: Mapped[str] = mapped_column(String(40))
    body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CheckoutVerificationRow(Base):
    __tablename__ = "checkout_verifications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), index=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("purchase_contracts.id"), index=True)
    contract_version: Mapped[int] = mapped_column(Integer)
    contract_hash: Mapped[str] = mapped_column(String(64))
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("checkout_snapshots.id"))
    status: Mapped[str] = mapped_column(String(10))
    conditions: Mapped[list] = mapped_column(JSON)
    reasons: Mapped[list] = mapped_column(JSON)
    explanation: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("purchase_contracts.id"), index=True)
    contract_version: Mapped[int] = mapped_column(Integer)
    contract_hash: Mapped[str] = mapped_column(String(64))
    verification_id: Mapped[str] = mapped_column(ForeignKey("checkout_verifications.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    environment: Mapped[str] = mapped_column(String(16))
    paypal_order_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    paypal_capture_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approve_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(3))
    captured_amount: Mapped[str | None] = mapped_column(String(32), nullable=True)
    captured_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    # CREATING | APPROVAL_PENDING | APPROVED | CAPTURING | CAPTURED | PENDING | UNCERTAIN | FAILED | CANCELLED
    payment_status: Mapped[str] = mapped_column(String(20), index=True)
    paypal_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # PayPal event id
    event_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PurchasePassportRow(Base):
    __tablename__ = "purchase_passports"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    body: Mapped[dict] = mapped_column(JSON)
    passport_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --- Simulation-side tables (not CHECKMATE's own records) ------------------------


class DemoMerchantCheckout(Base):
    """State held by the controlled demo merchant."""

    __tablename__ = "demo_merchant_checkouts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scenario: Mapped[str] = mapped_column(String(40))
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MockPayPalOrder(Base):
    """State held by the simulated PayPal used when no sandbox credentials are set."""

    __tablename__ = "mock_paypal_orders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))
    body: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MockPayPalRequest(Base):
    __tablename__ = "mock_paypal_requests"
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    response: Mapped[dict] = mapped_column(JSON)
