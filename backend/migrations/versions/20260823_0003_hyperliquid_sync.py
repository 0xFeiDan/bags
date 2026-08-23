"""hyperliquid account equity snapshots

Revision ID: 20260823_0003
Revises: 20260823_0002
Create Date: 2026-08-23
"""
from alembic import op

from app.db import Base
import app.models  # noqa: F401

revision = "20260823_0003"
down_revision = "20260823_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["account_equity_snapshots"].create(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.tables["account_equity_snapshots"].drop(bind=op.get_bind(), checkfirst=False)
