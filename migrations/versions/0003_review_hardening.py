"""Durable follow-up markers, provider retries, approved messages and sealed audit chains.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# Proposals written before patient_message existed carried only staff-facing guidance. They get
# a neutral placeholder that a reviewer must read (and may edit) before any approval.
LEGACY_PATIENT_MESSAGE = (
    "This is a synthetic reminder from your care team to arrange a follow-up visit."
)


def _backfill_patient_messages() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, proposal_json FROM follow_up_cases WHERE proposal_json IS NOT NULL")
    ).all()
    for case_id, proposal_json in rows:
        proposal = json.loads(proposal_json)
        if "patient_message" in proposal:
            continue
        proposal["patient_message"] = LEGACY_PATIENT_MESSAGE
        bind.execute(
            sa.text("UPDATE follow_up_cases SET proposal_json = :proposal WHERE id = :case_id"),
            {"proposal": json.dumps(proposal), "case_id": case_id},
        )


def upgrade() -> None:
    op.add_column(
        "customers", sa.Column("follow_up_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_customers_follow_up_queue",
        "customers",
        ["missed_appointment", "follow_up_started_at"],
    )
    op.add_column("follow_up_cases", sa.Column("snapshot_json", sa.Text(), nullable=True))
    op.add_column(
        "follow_up_cases",
        sa.Column("provider_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "human_reviews", sa.Column("approved_message", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "audit_events",
        sa.Column("hash_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index(
        "uq_audit_chain_link",
        "audit_events",
        ["aggregate_type", "aggregate_id", "previous_hash"],
        unique=True,
    )
    op.create_table(
        "audit_heads",
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", sa.String(length=36), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("head_hash", sa.String(length=64), nullable=False),
        sa.Column("seal", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("aggregate_type", "aggregate_id"),
    )

    op.execute(
        sa.text(
            """
            UPDATE customers
            SET follow_up_started_at = (
                SELECT MIN(f.created_at) FROM follow_up_cases f WHERE f.customer_id = customers.id
            )
            WHERE EXISTS (SELECT 1 FROM follow_up_cases f WHERE f.customer_id = customers.id)
            """
        )
    )
    # Every case created before this revision received exactly one provider attempt.
    op.execute(sa.text("UPDATE follow_up_cases SET provider_attempts = 1"))
    _backfill_patient_messages()


def downgrade() -> None:
    op.drop_table("audit_heads")
    op.drop_index("uq_audit_chain_link", table_name="audit_events")
    with op.batch_alter_table("audit_events") as batch:
        batch.drop_column("hash_version")
    with op.batch_alter_table("human_reviews") as batch:
        batch.drop_column("approved_message")
    with op.batch_alter_table("follow_up_cases") as batch:
        batch.drop_column("provider_attempts")
        batch.drop_column("snapshot_json")
    op.drop_index("ix_customers_follow_up_queue", table_name="customers")
    with op.batch_alter_table("customers") as batch:
        batch.drop_column("follow_up_started_at")
