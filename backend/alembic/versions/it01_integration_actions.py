"""Durable integration tool actions.

Revision ID: it01_integration_actions
Revises: a3c5e7f9b1d4
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "it01_integration_actions"
down_revision = "a3c5e7f9b1d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_tool_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("collections.id"),
            nullable=False,
        ),
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("threads.id")
        ),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.job_id")),
        sa.Column("grant_id", postgresql.UUID(as_uuid=True)),
        sa.Column("invocation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("argument_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("approved", sa.Boolean()),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("result", sa.JSON()),
        sa.Column("last_error", sa.Text()),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "invocation_id",
            name="uq_integration_tool_actions_invocation",
        ),
    )
    op.create_index(
        "ix_integration_tool_actions_organization_id",
        "integration_tool_actions",
        ["organization_id"],
    )
    op.create_index(
        "ix_integration_tool_actions_state", "integration_tool_actions", ["state"]
    )


def downgrade() -> None:
    op.drop_table("integration_tool_actions")
