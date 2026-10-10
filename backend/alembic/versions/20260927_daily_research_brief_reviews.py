"""Add append-only research stage review ledger.

Revision ID: daily_brief_reviews_20260927
Revises: agent_ops_20260925
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "daily_brief_reviews_20260927"
down_revision = "agent_ops_20260925"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_stage_reviews",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("stage_type", sa.String(length=50), nullable=False),
        sa.Column("review_kind", sa.String(length=50), nullable=False),
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("output_hash", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("decision_payload", postgresql.JSONB(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"]),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"]),
        sa.UniqueConstraint(
            "run_id",
            "step_index",
            "output_hash",
            "review_kind",
            name="uq_research_stage_reviews_gate",
        ),
    )
    op.create_index(
        "ix_research_stage_reviews_run_step",
        "research_stage_reviews",
        ["run_id", "step_index"],
        unique=False,
    )
    op.create_index(
        "ix_research_stage_reviews_owner_id",
        "research_stage_reviews",
        ["owner_id"],
        unique=False,
    )
    op.create_index(
        "ix_research_stage_reviews_reviewer_id",
        "research_stage_reviews",
        ["reviewer_id"],
        unique=False,
    )
    op.create_index(
        "ix_research_stage_reviews_organization_id",
        "research_stage_reviews",
        ["organization_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_stage_reviews_organization_id",
        table_name="research_stage_reviews",
    )
    op.drop_index(
        "ix_research_stage_reviews_reviewer_id",
        table_name="research_stage_reviews",
    )
    op.drop_index(
        "ix_research_stage_reviews_owner_id",
        table_name="research_stage_reviews",
    )
    op.drop_index(
        "ix_research_stage_reviews_run_step",
        table_name="research_stage_reviews",
    )
    op.drop_table("research_stage_reviews")
