"""merge artifact workspace and research heads

Revision ID: 70f0b2060bc5
Revises: aw01_artifact_workspace, d4e6f8a0b2c3
Create Date: 2026-09-30 01:55:28.915050

Both heads branched from c9d1e2f3a4b5 in parallel (artifact persistence and
search-result imports); they touch disjoint tables, so this only joins them.
"""

# revision identifiers, used by Alembic.
revision = "70f0b2060bc5"
down_revision = ("aw01_artifact_workspace", "d4e6f8a0b2c3")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
