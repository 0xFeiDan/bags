"""security authentication and session tables

Revision ID: 20260823_0004
Revises: 20260823_0003
Create Date: 2026-08-23
"""
from alembic import op

from app.db import Base
import app.models  # noqa: F401

revision = "20260823_0004"
down_revision = "20260823_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in ("users", "sessions", "login_challenges", "login_attempts", "security_events"):
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in ("security_events", "login_attempts", "login_challenges", "sessions", "users"):
        Base.metadata.tables[table_name].drop(bind=bind, checkfirst=False)
