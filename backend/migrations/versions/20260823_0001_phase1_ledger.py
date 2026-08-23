"""phase 1 immutable ledger foundation

Revision ID: 20260823_0001
Revises:
Create Date: 2026-08-23
"""
from alembic import op

from app.db import Base
import app.models  # noqa: F401 - registers every Phase 1 table

revision = "20260823_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    phase1_tables = {
        "portfolios",
        "assets",
        "asset_aliases",
        "accounts",
        "api_connections",
        "raw_events",
        "ledger_events",
        "ledger_entries",
        "balance_snapshots",
        "sync_cursors",
    }
    for table in Base.metadata.sorted_tables:
        if table.name in phase1_tables:
            table.create(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    phase1_tables = {
        "portfolios",
        "assets",
        "asset_aliases",
        "accounts",
        "api_connections",
        "raw_events",
        "ledger_events",
        "ledger_entries",
        "balance_snapshots",
        "sync_cursors",
    }
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in phase1_tables:
            table.drop(bind=op.get_bind(), checkfirst=False)
