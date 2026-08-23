"""cost basis engine, prices, overrides, and pnl adjustments

Revision ID: 20260823_0007
Revises: 20260823_0006
Create Date: 2026-08-23
"""
from alembic import op

from app.db import Base
import app.models  # noqa: F401

revision = "20260823_0007"
down_revision = "20260823_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in (
        "asset_prices",
        "cost_basis_overrides",
        "cost_basis_runs",
        "cost_lots",
        "cost_lot_consumptions",
        "realized_pnl_records",
        "position_cost_snapshots",
        "pnl_adjustments",
    ):
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in (
        "pnl_adjustments",
        "position_cost_snapshots",
        "realized_pnl_records",
        "cost_lot_consumptions",
        "cost_lots",
        "cost_basis_runs",
        "cost_basis_overrides",
        "asset_prices",
    ):
        Base.metadata.tables[table_name].drop(bind=bind, checkfirst=False)
