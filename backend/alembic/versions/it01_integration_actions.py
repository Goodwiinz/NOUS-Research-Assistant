"""Durable integration tool actions.

Revision ID: it01_integration_actions
Revises: d7f9b1c3e5a8
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "it01_integration_actions"
# #1780 and #1773/#1778 branched from b8d0f2a4c6e9 in parallel; this revision
# now follows GOO-307's d7f9b1c3e5a8 so develop keeps a single head.
down_revision = "d7f9b1c3e5a8"
branch_labels = None
depends_on = None
_POSTGREST_ROLES = ("anon", "authenticated")


def _deny_data_api(table: str) -> None:
    # Copied from f2a4c6e8b0d3: internal ledger, never reachable through the
    # Data API. An inherited insert could forge an `approved` native row.
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    for role in _POSTGREST_ROLES:
        op.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    EXECUTE 'REVOKE ALL ON TABLE "{table}" FROM {role}';
                END IF;
            END
            $$
            """)


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
        sa.Column("consent_id", postgresql.UUID(as_uuid=True)),
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
    op.create_index(
        "ix_integration_tool_actions_consent_id",
        "integration_tool_actions",
        ["consent_id"],
    )
    op.create_index(
        "ix_integration_tool_actions_id", "integration_tool_actions", ["id"]
    )
    _deny_data_api("integration_tool_actions")


def downgrade() -> None:
    op.drop_table("integration_tool_actions")
