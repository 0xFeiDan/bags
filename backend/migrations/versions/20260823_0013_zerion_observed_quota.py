"""Cap Zerion settings at the configured free-plan quota

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
    # Never increase a tighter operator setting; only clamp rows that exceed
    # the configured free-plan ceiling.
    op.execute(
        """
        UPDATE account_data_sources
        SET requests_per_second_limit = CASE
                WHEN requests_per_second_limit > 3 THEN 3 ELSE requests_per_second_limit END,
            daily_request_limit = CASE
                WHEN daily_request_limit > 2000 THEN 2000 ELSE daily_request_limit END,
            daily_request_budget = CASE
                WHEN daily_request_budget > 1800 THEN 1800 ELSE daily_request_budget END
        WHERE provider = 'zerion'
        """
    )
    op.execute(
        """
        UPDATE provider_quota_usage
        SET request_limit = CASE WHEN request_limit > 2000 THEN 2000 ELSE request_limit END,
            request_budget = CASE WHEN request_budget > 1800 THEN 1800 ELSE request_budget END
        WHERE provider = 'zerion'
        """
    )


def downgrade() -> None:
    # A downgrade must not silently increase an external-provider quota.
    pass
