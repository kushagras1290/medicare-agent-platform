"""Initial synthetic human-in-the-loop schema.

Revision ID: 0001
Revises: None

Explicit DDL: this revision must create the same schema no matter how the ORM models evolve
later. Consent evidence and worker heartbeats arrive in 0002; later columns arrive in 0003.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _timestamp(name: str, *, nullable: bool = False) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.String(length=16), primary_key=True),
        sa.Column("age", sa.Integer(), nullable=False),
        sa.Column("sex_at_birth", sa.String(length=16), nullable=False),
        sa.Column("conditions_json", sa.Text(), nullable=False),
        sa.Column("missed_appointment", sa.Boolean(), nullable=False),
        sa.Column("consent_status", sa.String(length=32), nullable=False),
        _timestamp("last_contact_at"),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        _timestamp("created_at"),
        sa.CheckConstraint("synthetic", name="ck_customers_synthetic_only"),
    )
    op.create_index("ix_customers_missed_appointment", "customers", ["missed_appointment"])

    op.create_table(
        "observations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "customer_id",
            sa.String(length=16),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        _timestamp("measured_at"),
        *(
            sa.Column(name, sa.Float(), nullable=False)
            for name in (
                "bp_systolic_mm_hg",
                "bp_diastolic_mm_hg",
                "heart_rate_bpm",
                "spo2_pct",
                "temperature_c",
                "respiratory_rate_bpm",
                "weight_kg",
                "bmi",
                "creatinine_mg_dl",
                "egfr_ml_min_1_73m2",
                "glucose_mg_dl",
                "hemoglobin_g_dl",
                "sodium_mmol_l",
                "potassium_mmol_l",
            )
        ),
        sa.Column("phq9_score", sa.Integer(), nullable=False),
    )

    op.create_table(
        "follow_up_cases",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "customer_id", sa.String(length=16), sa.ForeignKey("customers.id"), nullable=False
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("rule_tier", sa.String(length=16), nullable=False),
        sa.Column("rule_reasons_json", sa.Text(), nullable=False),
        sa.Column("policy_holds_json", sa.Text(), nullable=False),
        sa.Column("proposal_json", sa.Text(), nullable=True),
        sa.Column("provider_error", sa.String(length=300), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
    )
    op.create_index("ix_cases_status_created", "follow_up_cases", ["status", "created_at"])
    op.create_index("ix_cases_customer", "follow_up_cases", ["customer_id"])

    op.create_table(
        "human_reviews",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "case_id", sa.String(length=36), sa.ForeignKey("follow_up_cases.id"), nullable=False
        ),
        sa.Column("reviewer_id", sa.String(length=80), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("rationale", sa.String(length=1000), nullable=False),
        sa.Column("case_version", sa.Integer(), nullable=False),
        _timestamp("created_at"),
        sa.UniqueConstraint("case_id", "case_version", name="uq_review_case_version"),
    )

    op.create_table(
        "provider_calls",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "case_id", sa.String(length=36), sa.ForeignKey("follow_up_cases.id"), nullable=False
        ),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        _timestamp("created_at"),
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "case_id",
            sa.String(length=36),
            sa.ForeignKey("follow_up_cases.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        _timestamp("available_at"),
        _timestamp("claimed_at", nullable=True),
        _timestamp("delivered_at", nullable=True),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("last_error", sa.String(length=300), nullable=True),
    )
    op.create_index("ix_outbox_status_available", "outbox_events", ["status", "available_at"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=24), nullable=False),
        sa.Column("actor_id", sa.String(length=80), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False),
        sa.Column("previous_hash", sa.String(length=64), nullable=False),
        sa.Column("event_hash", sa.String(length=64), nullable=False, unique=True),
        _timestamp("created_at"),
    )
    op.create_index(
        "ix_audit_aggregate_created",
        "audit_events",
        ["aggregate_type", "aggregate_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_aggregate_created", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_outbox_status_available", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_table("provider_calls")
    op.drop_table("human_reviews")
    op.drop_index("ix_cases_customer", table_name="follow_up_cases")
    op.drop_index("ix_cases_status_created", table_name="follow_up_cases")
    op.drop_table("follow_up_cases")
    op.drop_table("observations")
    op.drop_index("ix_customers_missed_appointment", table_name="customers")
    op.drop_table("customers")
