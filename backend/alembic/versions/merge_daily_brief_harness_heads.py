"""Merge the Daily Research Brief and local harness migration heads.

Revision ID: merge_daily_harness_20260928
Revises: merge_harness_heads, merge_research_heads_20260928

Both feature branches contain valid independent migration histories. This
no-op merge restores one upgrade head after their Git histories converge.
"""

revision = "merge_daily_harness_20260928"
down_revision = ("merge_harness_heads", "merge_research_heads_20260928")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
