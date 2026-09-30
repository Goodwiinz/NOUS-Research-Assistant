"""Merge the draft-promotion and integration-merge heads.

#1778 (d7f9b1c3e5a8) and #1787 (it02_merge_integration_heads) landed back to
back from the same history. No schema change.

Revision ID: it03_merge_draft_heads
Revises: d7f9b1c3e5a8, it02_merge_integration_heads
"""

revision = "it03_merge_draft_heads"
down_revision = ("d7f9b1c3e5a8", "it02_merge_integration_heads")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
