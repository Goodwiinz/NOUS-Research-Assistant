"""Artifact lifecycle outbox.

Revision ID: aw02_artifact_lifecycle
Revises: aw01_artifact_workspace
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "aw02_artifact_lifecycle"
# Merge point: develop carried aw01 (artifacts) and d4e6f8a0b2c3 (search
# imports, #1754) as sibling heads off c9d1e2f3a4b5.
down_revision = ("aw01_artifact_workspace", "d4e6f8a0b2c3")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifact_lifecycle_outbox",
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
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("artifacts.id"),
            nullable=False,
        ),
        sa.Column(
            "version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("artifact_versions.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.job_id")),
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("threads.id")
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(200)),
        sa.UniqueConstraint(
            "version_id", "kind", name="uq_artifact_lifecycle_version_kind"
        ),
    )
    op.create_index(
        "ix_artifact_lifecycle_outbox_status", "artifact_lifecycle_outbox", ["status"]
    )


def downgrade() -> None:
    op.drop_table("artifact_lifecycle_outbox")
