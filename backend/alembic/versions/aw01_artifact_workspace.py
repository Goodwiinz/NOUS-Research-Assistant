"""Durable artifact versions, uploads, and references.

Revision ID: aw01_artifact_workspace
Revises: merge_daily_harness_20260928
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "aw01_artifact_workspace"
down_revision = "c9d1e2f3a4b5"
branch_labels = None
depends_on = None


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    ]


def upgrade() -> None:
    op.create_table(
        "artifacts",
        *_base_columns(),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("collections.id"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_index("ix_artifacts_organization_id", "artifacts", ["organization_id"])
    op.create_index("ix_artifacts_project_id", "artifacts", ["project_id"])

    op.create_table(
        "artifact_uploads",
        *_base_columns(),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("collections.id"),
            nullable=False,
        ),
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("publication_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("publish_hash", sa.String(64)),
        sa.Column("storage_key", sa.String(512)),
        sa.Column("stored_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True)),
        sa.UniqueConstraint(
            "grant_id", "publication_id", name="uq_artifact_upload_publication"
        ),
    )
    op.create_index(
        "ix_artifact_uploads_project_id", "artifact_uploads", ["project_id"]
    )

    op.create_table(
        "artifact_versions",
        *_base_columns(),
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("artifacts.id"),
            nullable=False,
        ),
        sa.Column(
            "parent_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("artifact_versions.id"),
        ),
        sa.Column(
            "upload_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("artifact_uploads.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("producer", sa.String(16), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.job_id")),
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("threads.id")
        ),
        sa.Column("grant_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_index(
        "ix_artifact_versions_artifact_id", "artifact_versions", ["artifact_id"]
    )

    op.create_table(
        "artifact_references",
        *_base_columns(),
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
            unique=True,
        ),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.job_id")),
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("threads.id")
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id"),
        ),
    )
    op.create_index(
        "ix_artifact_references_thread_id", "artifact_references", ["thread_id"]
    )


def downgrade() -> None:
    op.drop_table("artifact_references")
    op.drop_table("artifact_versions")
    op.drop_table("artifact_uploads")
    op.drop_table("artifacts")
