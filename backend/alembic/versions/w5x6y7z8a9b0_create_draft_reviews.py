"""create durable draft citation reviews

Revision ID: w5x6y7z8a9b0
Revises: v4w5x6y7z8a9
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "w5x6y7z8a9b0"
down_revision = "v4w5x6y7z8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "draft_reviews",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("base_draft_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("candidate_content_hash", sa.String(length=64), nullable=False),
        sa.Column("candidate_content", sa.Text(), nullable=False),
        sa.Column("source_document_ids", postgresql.JSONB(), nullable=False),
        sa.Column("review", postgresql.JSONB(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('passed', 'blocked')", name="ck_draft_reviews_outcome"
        ),
        sa.ForeignKeyConstraint(
            ["base_draft_id"], ["generated_drafts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["collections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_draft_reviews_project_created",
        "draft_reviews",
        ["project_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_draft_reviews_project_created", table_name="draft_reviews")
    op.drop_table("draft_reviews")
