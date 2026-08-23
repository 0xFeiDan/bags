"""Retired account-management schema compatibility marker.

Revision ID: 20260823_0014
Revises: 20260823_0013

The short-lived account-management revision was removed when Zerion became the
chain data layer again. Keep the revision ID so a database that already recorded
it can still start and advance to later migrations.
"""

revision = "20260823_0014"
down_revision = "20260823_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
