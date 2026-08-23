"""Retired provider schema compatibility marker.

Revision ID: 20260823_0011
Revises: 20260823_0010

The provider feature was removed from the application.  This no-op revision is
kept so databases that already recorded the historical revision can still be
opened without restoring the removed runtime integration.
"""

revision = "20260823_0011"
down_revision = "20260823_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
