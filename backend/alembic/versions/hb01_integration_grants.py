"""Restricted integration grants and owner-bound devices.

Revision ID: hb01_integration_grants
Revises: u3v4w5x6y7z8 (execution-time head)
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "hb01_integration_grants"
down_revision = "u3v4w5x6y7z8"
branch_labels = None
depends_on = None


def _base() -> list[sa.Column[Any]]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    ]


def _identity() -> list[sa.Column[Any]]:
    return [
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
    ]


def _project() -> sa.Column[Any]:
    return sa.Column(
        "project_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("collections.id"),
        nullable=False,
    )


def _device(nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(
        "device_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("bridge_devices.id"),
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "bridge_devices",
        *_base(),
        *_identity(),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_bridge_devices_user_id", "bridge_devices", ["user_id"])
    op.create_table(
        "bridge_workspace_bindings",
        *_base(),
        _device(),
        _project(),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.UniqueConstraint(
            "device_id", "workspace_id", name="uq_bridge_device_workspace"
        ),
    )
    op.create_table(
        "integration_grant_requests",
        *_base(),
        *_identity(),
        _device(),
        _project(),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("consent_revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "integration_grants",
        *_base(),
        *_identity(),
        _device(nullable=True),
        _project(),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("integration_grant_requests.id"),
        ),
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("threads.id")
        ),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.job_id")),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
    )
    for table in (
        "bridge_devices",
        "bridge_workspace_bindings",
        "integration_grant_requests",
        "integration_grants",
    ):
        op.create_index(f"ix_{table}_id", table, ["id"])


def downgrade() -> None:
    for table in (
        "integration_grants",
        "integration_grant_requests",
        "bridge_workspace_bindings",
        "bridge_devices",
    ):
        op.drop_table(table)
