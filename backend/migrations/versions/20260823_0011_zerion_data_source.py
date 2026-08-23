"""Zerion data-source foundation

Revision ID: 20260823_0011
Revises: 20260823_0010
Create Date: 2026-08-23
"""

from alembic import op

from app.db import Base
import app.models  # noqa: F401


revision = "20260823_0011"
down_revision = "20260823_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["account_data_sources"].create(bind=op.get_bind(), checkfirst=False)
    Base.metadata.tables["provider_sync_runs"].create(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.tables["provider_sync_runs"].drop(bind=op.get_bind(), checkfirst=False)
    Base.metadata.tables["account_data_sources"].drop(bind=op.get_bind(), checkfirst=False)
