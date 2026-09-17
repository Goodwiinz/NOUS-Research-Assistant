"""Add bounded provider reasoning summaries to chat messages.

Revision ID: u3v4w5x6y7z8
Revises: t2u3v4w5x6y7
Create Date: 2026-09-17

The field contains only provider-authored public summary text extracted from
typed reasoning summary blocks. It is nullable so legacy and ordinary turns
remain unchanged; no historical reasoning is backfilled.
"""

import sqlalchemy as sa
from alembic import op

revision = "u3v4w5x6y7z8"
down_revision = "t2u3v4w5x6y7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("reasoning_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "reasoning_summary")
