"""EVM wallet synchronization runs

Revision ID: 20260823_0005
Revises: 20260823_0004
Create Date: 2026-08-23
"""
from alembic import op

from app.db import Base
import app.models  # noqa: F401

revision = "20260823_0005"
down_revision = "20260823_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["wallet_sync_runs"].create(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.tables["wallet_sync_runs"].drop(bind=op.get_bind(), checkfirst=False)
