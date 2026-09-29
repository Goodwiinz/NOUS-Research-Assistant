"""Merge daily brief and academic workflow heads

Revision ID: merge_research_heads_20260928
Revises: b4d6f8021a3c, daily_brief_reviews_20260927
Create Date: 2026-09-28 14:38:47.830672

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "merge_research_heads_20260928"
down_revision = ("b4d6f8021a3c", "daily_brief_reviews_20260927")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
