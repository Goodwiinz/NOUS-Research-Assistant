"""Freeze explicitly selected project skills under browser consent.

Revision ID: ic02_selected_skill_snapshot
Revises: sv01_search_vector_repair
"""

import alembic.op as op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "ic02_selected_skill_snapshot"
down_revision = "sv01_search_vector_repair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "integration_context_selections",
        sa.Column("skill_version_ids", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "integration_context_selections",
        sa.Column("runtime_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_context_selection_runtime_snapshot",
        "integration_context_selections",
        "agent_runtime_snapshots",
        ["runtime_snapshot_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_context_selection_runtime_snapshot",
        "integration_context_selections",
        type_="foreignkey",
    )
    op.drop_column("integration_context_selections", "runtime_snapshot_id")
    op.drop_column("integration_context_selections", "skill_version_ids")
