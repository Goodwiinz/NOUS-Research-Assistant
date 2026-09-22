"""add stable citation source position

Revision ID: t2u3v4w5x6y7
Revises: s1t2u3v4w5x6
Create Date: 2026-09-12
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

revision = "t2u3v4w5x6y7"
down_revision = "s1t2u3v4w5x6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "citations", sa.Column("source_position", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("citations", "source_position")
