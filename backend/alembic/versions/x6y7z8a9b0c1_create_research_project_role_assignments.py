"""Create explicit collection-scoped research project role assignments.

Revision ID: x6y7z8a9b0c1
Revises: w5x6y7z8a9b0
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "x6y7z8a9b0c1"
down_revision = "w5x6y7z8a9b0"
branch_labels = None
depends_on = None

role_enum = sa.Enum(
    "reviewer",
    "adjudicator",
    "supervisor",
    name="researchprojectrole",
)


def upgrade() -> None:
    op.create_table(
        "research_project_role_assignments",
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", role_enum, nullable=False),
        sa.Column("assigned_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["assigned_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["collection_id"], ["collections.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "collection_id",
            "user_id",
            "role",
            name="uq_research_project_role_assignment",
        ),
    )
    op.create_index(
        "ix_research_project_role_assignments_collection_id",
        "research_project_role_assignments",
        ["collection_id"],
    )
    op.create_index(
        "ix_research_project_role_assignments_user_id",
        "research_project_role_assignments",
        ["user_id"],
    )
    op.execute("""
        CREATE FUNCTION prevent_linked_research_project_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.collection_id IS NOT NULL THEN
                RAISE EXCEPTION
                    'linked research project history must be retained'
                    USING ERRCODE = '23503';
            END IF;
            RETURN OLD;
        END;
        $$
        """)
    op.execute("""
        CREATE TRIGGER trg_retain_linked_research_project
        BEFORE DELETE ON research_projects
        FOR EACH ROW
        EXECUTE FUNCTION prevent_linked_research_project_delete()
        """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_retain_linked_research_project ON research_projects")
    op.execute("DROP FUNCTION prevent_linked_research_project_delete()")
    op.drop_index(
        "ix_research_project_role_assignments_user_id",
        table_name="research_project_role_assignments",
    )
    op.drop_index(
        "ix_research_project_role_assignments_collection_id",
        table_name="research_project_role_assignments",
    )
    op.drop_table("research_project_role_assignments")
    role_enum.drop(op.get_bind(), checkfirst=True)
