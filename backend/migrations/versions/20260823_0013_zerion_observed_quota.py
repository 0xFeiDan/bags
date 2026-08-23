"""Tighten Zerion defaults to the observed demo quota

Revision ID: 20260823_0013
Revises: 20260823_0012
Create Date: 2026-08-23
"""

from alembic import op


revision = "20260823_0013"
down_revision = "20260823_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows may contain the older published 3/2,000 limits. Never
    # increase a tighter operator setting; only clamp Zerion rows downward.
    op.execute(
        """
        UPDATE account_data_sources
        SET requests_per_second_limit = CASE
                WHEN requests_per_second_limit > 1 THEN 1 ELSE requests_per_second_limit END,
            daily_request_limit = CASE
                WHEN daily_request_limit > 300 THEN 300 ELSE daily_request_limit END,
            daily_request_budget = CASE
                WHEN daily_request_budget > 270 THEN 270 ELSE daily_request_budget END
        WHERE provider = 'zerion'
        """
    )
    op.execute(
        """
        UPDATE provider_quota_usage
        SET request_limit = CASE WHEN request_limit > 300 THEN 300 ELSE request_limit END,
            request_budget = CASE WHEN request_budget > 270 THEN 270 ELSE request_budget END
        WHERE provider = 'zerion'
        """
    )


def downgrade() -> None:
    # A downgrade must not silently increase an external-provider quota.
    pass
