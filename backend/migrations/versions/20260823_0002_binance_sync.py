"""binance position snapshots and auditable sync runs

Revision ID: 20260823_0002
Revises: 20260823_0001
Create Date: 2026-08-23
"""
from alembic import op

from app.db import Base
import app.models  # noqa: F401

revision = "20260823_0002"
down_revision = "20260823_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("position_snapshots", "sync_runs"):
        Base.metadata.tables[name].create(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    for name in ("sync_runs", "position_snapshots"):
        Base.metadata.tables[name].drop(bind=op.get_bind(), checkfirst=False)
