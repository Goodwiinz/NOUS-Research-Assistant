"""Link research-engine projects to canonical collections.

Revision ID: v4w5x6y7z8a9
Revises: u3v4w5x6y7z8
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

revision = "v4w5x6y7z8a9"
down_revision = "u3v4w5x6y7z8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_projects",
        sa.Column("collection_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_research_projects_collection_id",
        "research_projects",
        "collections",
        ["collection_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_research_projects_collection_id", "research_projects", ["collection_id"]
    )
    # Only identical historical IDs are safe to infer. Names are never matched.
    op.execute("""
        UPDATE research_projects AS rp
        SET collection_id = c.id
        FROM collections AS c
        JOIN workspaces AS w ON w.id = c.workspace_id
        WHERE rp.id = c.id
          AND rp.owner_id = w.owner_id
          AND rp.is_deleted = false
          AND c.is_deleted = false
          AND w.is_deleted = false
          AND rp.collection_id IS NULL
        """)


def downgrade() -> None:
    op.drop_constraint(
        "uq_research_projects_collection_id", "research_projects", type_="unique"
    )
    op.drop_constraint(
        "fk_research_projects_collection_id", "research_projects", type_="foreignkey"
    )
    op.drop_column("research_projects", "collection_id")
