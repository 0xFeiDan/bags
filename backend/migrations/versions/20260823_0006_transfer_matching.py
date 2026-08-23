"""transfer matching candidates and groups

Revision ID: 20260823_0006
Revises: 20260823_0005
Create Date: 2026-08-23
"""
from alembic import op

from app.db import Base
import app.models  # noqa: F401

revision = "20260823_0006"
down_revision = "20260823_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in ("transfer_match_runs", "transfer_candidates", "transfer_groups"):
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in ("transfer_groups", "transfer_candidates", "transfer_match_runs"):
        Base.metadata.tables[table_name].drop(bind=bind, checkfirst=False)
