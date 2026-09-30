"""Exact owner-mediated Codex approvals and required input.

Revision ID: hb04_harness_approvals
Revises: hb03_bridge_delivery
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "hb04_harness_approvals"
down_revision = "hb03_bridge_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "harness_native_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("harness_sessions.run_id"),
            nullable=False,
        ),
        sa.Column(
            "command_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("harness_commands.id"),
            nullable=False,
        ),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(255), nullable=False),
        sa.Column("turn_id", sa.String(255), nullable=False),
        sa.Column("item_id", sa.String(255), nullable=False),
        sa.Column("request_id_type", sa.String(8), nullable=False),
        sa.Column("request_id_value", sa.String(255), nullable=False),
        sa.Column("approval_id", sa.String(255)),
        sa.Column("method", sa.String(80), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("target_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("decision", sa.JSON()),
        sa.Column("response_command_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_index(
        "ix_harness_native_request_owner",
        "harness_native_requests",
        ["actor_id", "expires_at"],
    )
    op.create_index(
        "ix_harness_native_request_run",
        "harness_native_requests",
        ["run_id", "generation"],
    )
    op.create_index(
        "uq_harness_native_callback",
        "harness_native_requests",
        [
            "run_id",
            "generation",
            "session_id",
            "turn_id",
            "item_id",
            "request_id_type",
            "request_id_value",
        ],
        unique=True,
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM harness_native_requests WHERE consumed_at IS NULL AND expires_at > now()) THEN RAISE EXCEPTION 'Live native approvals prevent downgrade'; END IF; END $$;"
    )
    op.drop_table("harness_native_requests")
