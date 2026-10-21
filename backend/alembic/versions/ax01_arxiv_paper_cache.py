"""Tenant-scoped arXiv metadata cache.

Revision ID: ax01_arxiv_paper_cache
Revises: ho04_handoff_org_slots
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ax01_arxiv_paper_cache"
down_revision = "ho04_handoff_org_slots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "arxiv_papers",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("paper_id", sa.String(64), primary_key=True),
        sa.Column("metadata_payload", sa.JSON(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute('ALTER TABLE "arxiv_papers" ENABLE ROW LEVEL SECURITY')
    for role in ("anon", "authenticated"):
        op.execute(f"""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    EXECUTE 'REVOKE ALL ON TABLE "arxiv_papers" FROM {role}';
                END IF;
            END $$;
        """)


def downgrade() -> None:
    op.drop_table("arxiv_papers")
