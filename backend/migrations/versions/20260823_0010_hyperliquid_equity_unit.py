"""correct Hyperliquid account equity reporting unit

Revision ID: 20260823_0010
Revises: 20260823_0009
Create Date: 2026-08-23
"""

from alembic import op


revision = "20260823_0010"
down_revision = "20260823_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Raw events remain immutable. AccountEquitySnapshot is a rebuildable
    # derived record, and Hyperliquid marginSummary.accountValue is already
    # the clearinghouse USD account-value result.
    op.execute(
        "UPDATE account_equity_snapshots SET currency = 'USD' "
        "WHERE provider = 'hyperliquid' AND currency = 'USDC'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE account_equity_snapshots SET currency = 'USDC' "
        "WHERE provider = 'hyperliquid' AND currency = 'USD'"
    )
