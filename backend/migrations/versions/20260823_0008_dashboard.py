"""portfolio dashboard snapshots

Revision ID: 20260823_0008
Revises: 20260823_0007
Create Date: 2026-08-23
"""
from alembic import op

from app.db import Base
import app.models  # noqa: F401

revision = "20260823_0008"
down_revision = "20260823_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["portfolio_snapshots"].create(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.tables["portfolio_snapshots"].drop(bind=op.get_bind(), checkfirst=False)
