"""Bind a browser consent request to one chat (Plan 06 slice 2).

Revision ID: hb05_grant_request_thread
Revises: d4a6c8e0f2b3
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "hb05_grant_request_thread"
down_revision = "d4a6c8e0f2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "integration_grant_requests",
        sa.Column(
            "thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("threads.id", name="fk_integration_grant_requests_thread_id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_integration_grant_requests_thread_id",
        "integration_grant_requests",
        ["thread_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_integration_grant_requests_thread_id",
        table_name="integration_grant_requests",
    )
    op.drop_column("integration_grant_requests", "thread_id")
