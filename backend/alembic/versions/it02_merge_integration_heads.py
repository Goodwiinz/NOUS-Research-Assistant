"""Merge the integration-actions and research-claims heads.

#1773 (c4e6a8b0d2f5) and #1780 (it01_integration_actions) both branched from
b8d0f2a4c6e9 and landed back to back. No schema change.

Revision ID: it02_merge_integration_heads
Revises: c4e6a8b0d2f5, it01_integration_actions
"""

revision = "it02_merge_integration_heads"
down_revision = ("c4e6a8b0d2f5", "it01_integration_actions")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
