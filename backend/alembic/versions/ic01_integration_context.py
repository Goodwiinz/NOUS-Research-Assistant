"""Explicit integration context selections.

Revision ID: ic01_integration_context
Revises: it02_merge_integration_heads
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ic01_integration_context"
down_revision = "it02_merge_integration_heads"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")


def _deny_data_api(table: str) -> None:
    # Copied from f2a4c6e8b0d3: internal table, never reachable through the Data API.
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
        "integration_context_selections",
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
            "consent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("integration_grant_requests.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("memory_ids", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_integration_context_selections_organization_id",
        "integration_context_selections",
        ["organization_id"],
    )
    op.create_index(
        "ix_integration_context_selections_id",
        "integration_context_selections",
        ["id"],
    )
    _deny_data_api("integration_context_selections")


def downgrade() -> None:
    op.drop_table("integration_context_selections")
