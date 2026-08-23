"""Restore the current Zerion free-plan limits.

Revision ID: 20260823_0015
Revises: 20260823_0014
"""

from alembic import op


revision = "20260823_0015"
down_revision = "20260823_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Revision 0013 previously clamped rows to 1/300/270. Raise only that exact
    # historical tuple; other tighter operator settings remain untouched.
    op.execute(
        """
        UPDATE account_data_sources
        SET requests_per_second_limit = 3,
            daily_request_limit = 2000,
            daily_request_budget = 1800
        WHERE provider = 'zerion'
          AND requests_per_second_limit = 1
          AND daily_request_limit = 300
          AND daily_request_budget = 270
        """
    )
    op.execute(
        """
        UPDATE provider_quota_usage
        SET request_limit = 2000,
            request_budget = 1800
        WHERE provider = 'zerion'
          AND request_limit = 300
          AND request_budget = 270
        """
    )


def downgrade() -> None:
    # Never silently lower a provider quota during rollback.
    pass
