"""Versioned chat handoff records written by external harnesses (Plan 06 slice 3).

Revision ID: hb06_integration_handoffs
Revises: hb05_grant_request_thread
"""

from typing import Any

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "hb06_integration_handoffs"
down_revision = "hb05_grant_request_thread"
branch_labels = None
depends_on = None


def _fk(name: str, target: str, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, name=f"fk_integration_handoffs_{name}"),
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "integration_handoffs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        _fk("organization_id", "organizations.id"),
        _fk("project_id", "collections.id"),
        _fk("thread_id", "threads.id"),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("handoff_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("goal", sa.String(2000), nullable=False),
        sa.Column("decisions", sa.JSON(), nullable=False),
        sa.Column("remaining", sa.JSON(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("harness_name", sa.String(64), nullable=False),
        sa.Column("harness_session_id", sa.String(128)),
        _fk("grant_id", "integration_grants.id"),
        _fk("consent_id", "integration_grant_requests.id", nullable=True),
        _fk("created_by_user_id", "users.id"),
        sa.UniqueConstraint(
            "thread_id",
            "project_id",
            "version",
            name="uq_integration_handoffs_thread_project_version",
        ),
        sa.UniqueConstraint(
            "thread_id",
            "project_id",
            "handoff_id",
            name="uq_integration_handoffs_thread_project_handoff",
        ),
    )
    op.create_index("ix_integration_handoffs_id", "integration_handoffs", ["id"])


def downgrade() -> None:
    op.drop_index("ix_integration_handoffs_id", table_name="integration_handoffs")
    op.drop_table("integration_handoffs")
