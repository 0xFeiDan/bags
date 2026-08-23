"""Add editable wallet contracts and persistent market scopes.

Revision ID: 20260823_0014
Revises: 20260823_0013
"""

import sqlalchemy as sa
from alembic import op


revision = "20260823_0014"
down_revision = "20260823_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evm_tracked_contracts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("contract_address", sa.String(length=42), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "contract_address", name="evm_tracked_contract_identity"),
    )
    op.create_index("ix_evm_tracked_contracts_account_id", "evm_tracked_contracts", ["account_id"])
    op.create_table(
        "connection_market_scopes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("product", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("discovery_source", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["api_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "product", "symbol", name="connection_market_scope_identity"),
    )
    op.create_index("ix_connection_market_scopes_connection_id", "connection_market_scopes", ["connection_id"])


def downgrade() -> None:
    op.drop_index("ix_connection_market_scopes_connection_id", table_name="connection_market_scopes")
    op.drop_table("connection_market_scopes")
    op.drop_index("ix_evm_tracked_contracts_account_id", table_name="evm_tracked_contracts")
    op.drop_table("evm_tracked_contracts")
