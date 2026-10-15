"""Authenticated artifact sharing and durable native completion.

Revision ID: aw03_artifact_completion
Revises: ic02_selected_skill_snapshot
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "aw03_artifact_completion"
down_revision = "ic02_selected_skill_snapshot"
branch_labels = None
depends_on = None

_TABLES = ("artifact_shares", "artifact_share_audiences", "native_artifact_intents")


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
        "artifact_shares",
        *_base_columns(),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("artifact_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "artifact_share_audiences",
        *_base_columns(),
        sa.Column(
            "share_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("artifact_shares.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "share_id", "user_id", name="uq_artifact_share_audience_user"
        ),
    )
    op.create_table(
        "native_artifact_intents",
        *_base_columns(),
        sa.Column(
            "draft_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("generated_drafts.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("threads.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.job_id", ondelete="CASCADE"),
        ),
        sa.Column("draft_sha256", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("artifact_versions.id"),
        ),
        sa.Column("last_error", sa.String(64)),
    )
    for table, columns in (
        ("artifact_shares", ("id", "creator_id", "version_id")),
        ("artifact_share_audiences", ("id", "share_id", "user_id")),
        ("native_artifact_intents", ("id", "status")),
    ):
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])
    # Backend-only state: no PostgREST/browser role may bypass service checks.
    # The application connects directly as the owning backend role.
    for table in _TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        for role in ("anon", "authenticated"):
            op.execute(f"""
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                        EXECUTE 'REVOKE ALL ON TABLE "{table}" FROM {role}';
                    END IF;
                END
                $$;
            """)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)
