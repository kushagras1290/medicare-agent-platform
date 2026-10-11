"""Initial synthetic human-in-the-loop schema.

Revision ID: 0001
Revises: None
"""

from __future__ import annotations

from alembic import op

from medicare_agent import models  # noqa: F401
from medicare_agent.db import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
