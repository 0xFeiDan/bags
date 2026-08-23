"""Zerion shadow synchronization controls

Revision ID: 20260823_0012
Revises: 20260823_0011
Create Date: 2026-08-23
"""

import sqlalchemy as sa
from alembic import op

from app.db import Base
import app.models  # noqa: F401


revision = "20260823_0012"
down_revision = "20260823_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Phase 1 may already have been deployed before the exact free-plan hard
    # limits were added to its model. Fresh databases already receive these
    # columns from 0011's metadata creation; existing databases receive them
    # here without rewriting any source or ledger data.
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("account_data_sources")}
    if "requests_per_second_limit" not in columns:
        op.add_column(
            "account_data_sources",
            sa.Column("requests_per_second_limit", sa.Integer(), nullable=False, server_default="3"),
        )
    if "daily_request_limit" not in columns:
        op.add_column(
            "account_data_sources",
            sa.Column("daily_request_limit", sa.Integer(), nullable=False, server_default="2000"),
        )
    Base.metadata.tables["provider_sync_cursors"].create(bind=op.get_bind(), checkfirst=False)
    Base.metadata.tables["provider_quota_usage"].create(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.tables["provider_quota_usage"].drop(bind=op.get_bind(), checkfirst=False)
    Base.metadata.tables["provider_sync_cursors"].drop(bind=op.get_bind(), checkfirst=False)
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("account_data_sources")}
    if "daily_request_limit" in columns:
        op.drop_column("account_data_sources", "daily_request_limit")
    if "requests_per_second_limit" in columns:
        op.drop_column("account_data_sources", "requests_per_second_limit")
