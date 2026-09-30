"""Create draft_task_results: retained terminal state of draft tasks (GOO-297).

Revision ID: c9d1e2f3a4b5
Revises: c9d2e4f6a8b1
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "c9d1e2f3a4b5"
down_revision = "c9d2e4f6a8b1"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")


def _deny_data_api(table: str) -> None:
    # Resolve through the migration connection's search_path. Production uses
    # ``public``; isolated PostgreSQL migration tests use a dedicated schema.
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
        "draft_task_results",
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("artifact_version", sa.Integer(), nullable=True),
        sa.Column("artifact_hash", sa.String(length=64), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('running', 'completed', 'failed', 'cancelled', 'interrupted')",
            name="ck_draft_task_results_state",
        ),
        sa.CheckConstraint(
            "(state = 'completed') = (artifact_id IS NOT NULL"
            " AND artifact_version IS NOT NULL AND artifact_hash IS NOT NULL)",
            name="ck_draft_task_results_artifact",
        ),
        sa.CheckConstraint(
            "artifact_hash IS NULL OR length(artifact_hash) = 64",
            name="ck_draft_task_results_artifact_hash",
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_draft_task_results_request_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"], ["collections.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index(
        "idx_draft_task_results_collection",
        "draft_task_results",
        ["collection_id"],
    )
    _deny_data_api("draft_task_results")


def downgrade() -> None:
    op.drop_index("idx_draft_task_results_collection", table_name="draft_task_results")
    op.drop_table("draft_task_results")
