"""Consent evidence and worker health controls.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("consent_evidence"):
        op.create_table(
            "consent_evidence",
            sa.Column("customer_id", sa.String(length=16), nullable=False),
            sa.Column("purpose", sa.String(length=64), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("source", sa.String(length=80), nullable=False),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("synthetic", sa.Boolean(), nullable=False),
            sa.CheckConstraint("version > 0", name="ck_consent_version_positive"),
            sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("customer_id", "purpose", "version"),
        )
        op.create_index(
            "ix_consent_customer_purpose",
            "consent_evidence",
            ["customer_id", "purpose", "version"],
            unique=False,
        )
    op.execute(
        sa.text(
            """
            INSERT INTO consent_evidence
                (customer_id, purpose, version, status, source, captured_at, expires_at, synthetic)
            SELECT id, 'mock_follow_up', 1, consent_status, 'migration_backfill_v1',
                   created_at, NULL, synthetic
            FROM customers c
            WHERE NOT EXISTS (
                SELECT 1 FROM consent_evidence e
                WHERE e.customer_id = c.id AND e.purpose = 'mock_follow_up' AND e.version = 1
            )
            """
        )
    )
    if not inspector.has_table("worker_heartbeats"):
        op.create_table(
            "worker_heartbeats",
            sa.Column("worker_name", sa.String(length=40), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("processed", sa.Integer(), nullable=False),
            sa.Column("detail_json", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("worker_name"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("worker_heartbeats"):
        op.drop_table("worker_heartbeats")
    if inspector.has_table("consent_evidence"):
        indexes = {index["name"] for index in inspector.get_indexes("consent_evidence")}
        if "ix_consent_customer_purpose" in indexes:
            op.drop_index("ix_consent_customer_purpose", table_name="consent_evidence")
        op.drop_table("consent_evidence")
