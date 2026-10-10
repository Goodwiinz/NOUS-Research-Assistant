"""Merge the existing schema head with the local harness approval branch.

Revision ID: merge_harness_heads
Revises: b4d6f8021a3c, hb04_harness_approvals

The harness approval migration was developed from the harness delivery branch,
while the repository's main Alembic history continued at b4d6f8021a3c. This
no-op merge restores one upgrade head without changing either schema branch.
"""

revision = "merge_harness_heads"
down_revision = ("b4d6f8021a3c", "hb04_harness_approvals")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
