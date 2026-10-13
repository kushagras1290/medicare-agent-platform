from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from medicare_agent.db import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Normalize database timestamps; SQLite returns naive values that are stored as UTC."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    age: Mapped[int] = mapped_column(Integer, nullable=False)
    sex_at_birth: Mapped[str] = mapped_column(String(16), nullable=False)
    conditions_json: Mapped[str] = mapped_column(Text, nullable=False)
    missed_appointment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_contact_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Durable "already handled" marker. Retention deletes cases, never this value, so a purged
    # case can never make the customer eligible for a second automated follow-up.
    follow_up_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    observation: Mapped[Observation] = relationship(back_populates="customer", uselist=False)

    __table_args__ = (
        CheckConstraint("synthetic", name="ck_customers_synthetic_only"),
        Index("ix_customers_missed_appointment", "missed_appointment"),
        Index("ix_customers_follow_up_queue", "missed_appointment", "follow_up_started_at"),
    )


class Observation(Base):
    __tablename__ = "observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bp_systolic_mm_hg: Mapped[float] = mapped_column(Float, nullable=False)
    bp_diastolic_mm_hg: Mapped[float] = mapped_column(Float, nullable=False)
    heart_rate_bpm: Mapped[float] = mapped_column(Float, nullable=False)
    spo2_pct: Mapped[float] = mapped_column(Float, nullable=False)
    temperature_c: Mapped[float] = mapped_column(Float, nullable=False)
    respiratory_rate_bpm: Mapped[float] = mapped_column(Float, nullable=False)
    weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    bmi: Mapped[float] = mapped_column(Float, nullable=False)
    creatinine_mg_dl: Mapped[float] = mapped_column(Float, nullable=False)
    egfr_ml_min_1_73m2: Mapped[float] = mapped_column(Float, nullable=False)
    glucose_mg_dl: Mapped[float] = mapped_column(Float, nullable=False)
    hemoglobin_g_dl: Mapped[float] = mapped_column(Float, nullable=False)
    sodium_mmol_l: Mapped[float] = mapped_column(Float, nullable=False)
    potassium_mmol_l: Mapped[float] = mapped_column(Float, nullable=False)
    phq9_score: Mapped[int] = mapped_column(Integer, nullable=False)

    customer: Mapped[Customer] = relationship(back_populates="observation")


class ConsentEvidence(Base):
    __tablename__ = "consent_evidence"

    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), primary_key=True
    )
    purpose: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint("version > 0", name="ck_consent_version_positive"),
        Index("ix_consent_customer_purpose", "customer_id", "purpose", "version"),
    )


class FollowUpCase(Base):
    __tablename__ = "follow_up_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    rule_reasons_json: Mapped[str] = mapped_column(Text, nullable=False)
    policy_holds_json: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_json: Mapped[str | None] = mapped_column(Text)
    snapshot_json: Mapped[str | None] = mapped_column(Text)
    provider_error: Mapped[str | None] = mapped_column(String(300))
    provider_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_cases_status_created", "status", "created_at"),
        Index("ix_cases_customer", "customer_id"),
    )


class HumanReview(Base):
    __tablename__ = "human_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("follow_up_cases.id"), nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(80), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    rationale: Mapped[str] = mapped_column(String(1000), nullable=False)
    approved_message: Mapped[str | None] = mapped_column(String(500))
    case_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (UniqueConstraint("case_id", "case_version", name="uq_review_case_version"),)


class ProviderCall(Base):
    __tablename__ = "provider_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("follow_up_cases.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("follow_up_cases.id"), nullable=False, unique=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_id: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (Index("ix_outbox_status_available", "status", "available_at"),)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(80), nullable=False)
    data_json: Mapped[str] = mapped_column(Text, nullable=False)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    hash_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_audit_aggregate_created", "aggregate_type", "aggregate_id", "created_at"),
        # One successor per link: two writers racing on the same head cannot fork the chain.
        Index(
            "uq_audit_chain_link", "aggregate_type", "aggregate_id", "previous_hash", unique=True
        ),
    )


class AuditHead(Base):
    """Keyed seal over each chain's length and newest hash, so truncation is detectable."""

    __tablename__ = "audit_heads"

    aggregate_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    aggregate_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    head_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    seal: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_name: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
