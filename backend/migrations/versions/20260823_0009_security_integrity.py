"""security and accounting integrity constraints

Revision ID: 20260823_0009
Revises: 20260823_0008
Create Date: 2026-08-23
"""

from alembic import op


revision = "20260823_0009"
down_revision = "20260823_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE assets DROP CONSTRAINT IF EXISTS asset_identity")
        op.execute(
            "CREATE UNIQUE INDEX ux_asset_identity "
            "ON assets (canonical_symbol, chain_id, contract_address) NULLS NOT DISTINCT"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ux_asset_identity")
        op.execute(
            "ALTER TABLE assets ADD CONSTRAINT asset_identity "
            "UNIQUE (canonical_symbol, chain_id, contract_address)"
        )
