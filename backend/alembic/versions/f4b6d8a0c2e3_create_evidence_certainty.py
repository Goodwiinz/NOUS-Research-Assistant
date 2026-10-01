"""Create insert-only evidence tables, contradictions and certainty (GOO-310).

Three tables, each with GOO-309's ``prevent_research_insert_only_mutation()``
trigger function attached (SQLSTATE 55000 on UPDATE or DELETE). All checks
are frozen copies of ``src.models.research_evidence_table`` (never import src
here). No data statements. Downgrade drops the triggers and the tables in
reverse FK order and keeps the function, which ``e2a4c6b8d0f1`` owns.

Revision ID: f4b6d8a0c2e3
Revises: e2a4c6b8d0f1
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "f4b6d8a0c2e3"
down_revision = "e2a4c6b8d0f1"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_TABLES = (  # creation order; dependents last
    "evidence_table_versions",
    "evidence_contradictions",
    "outcome_certainty_assessments",
)


def _deny_data_api(table: str) -> None:
    # Copied from c9d2e4f6a8b1: resolve through the connection's search_path.
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


def _uuid(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, _UUID, nullable=nullable)


def _fk(column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete="RESTRICT")


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    op.create_table(
        "evidence_table_versions",
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("protocol_version_id"),
        sa.Column("outcome_key", sa.String(100), nullable=False),
        sa.Column("timepoint", sa.String(100), nullable=False),
        _uuid("matrix_id"),
        _uuid("form_version_id"),
        sa.Column("field_ids", postgresql.JSONB(), nullable=False),
        sa.Column("rows", postgresql.JSONB(), nullable=False),
        sa.Column("excluded", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        _uuid("created_by_id"),
        _uuid("supersedes_table_id", nullable=True),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("protocol_version_id", "research_protocol_versions"),
        _fk("matrix_id", "extraction_matrices"),
        _fk("form_version_id", "extraction_form_versions"),
        _fk("created_by_id", "users"),
        _fk("supersedes_table_id", "evidence_table_versions"),
        sa.UniqueConstraint("supersedes_table_id", name="uq_evidence_table_supersedes"),
        sa.CheckConstraint(
            "jsonb_typeof(field_ids) = 'array'"
            " AND jsonb_array_length(field_ids) BETWEEN 1 AND 50",
            name="ck_evidence_table_field_ids",
        ),
    )
    op.create_index(
        "uq_evidence_table_initial",
        "evidence_table_versions",
        ["collection_id", "outcome_key", "timepoint"],
        unique=True,
        postgresql_where=sa.text("supersedes_table_id IS NULL"),
    )
    op.create_table(
        "evidence_contradictions",
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("contradiction_id"),
        _uuid("table_version_id"),
        _uuid("field_id"),
        sa.Column("accepted_value_ids", postgresql.JSONB(), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        _uuid("actor_id"),
        sa.Column("actor_role", sa.String(16), nullable=False),
        sa.Column("suggestion", postgresql.JSONB(), nullable=True),
        _uuid("previous_id", nullable=True),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("table_version_id", "evidence_table_versions"),
        _fk("actor_id", "users"),
        _fk("previous_id", "evidence_contradictions"),
        sa.CheckConstraint(
            "kind IN ('opened','resolved','acknowledged','dissent')",
            name="ck_evidence_contradiction_kind",
        ),
        sa.CheckConstraint(
            "actor_role IN ('reviewer','adjudicator')",
            name="ck_evidence_contradiction_actor_role",
        ),
        sa.CheckConstraint(
            "(kind = 'opened') = (previous_id IS NULL)",
            name="ck_evidence_contradiction_chain",
        ),
        sa.CheckConstraint(
            "kind <> 'opened' OR contradiction_id = id",
            name="ck_evidence_contradiction_group",
        ),
        sa.CheckConstraint(
            "(kind = 'opened') = (accepted_value_ids IS NOT NULL)",
            name="ck_evidence_contradiction_members",
        ),
        sa.CheckConstraint(
            "suggestion IS NULL OR kind = 'opened'",
            name="ck_evidence_contradiction_suggestion",
        ),
        sa.CheckConstraint(
            "kind NOT IN ('resolved','acknowledged') OR actor_role = 'adjudicator'",
            name="ck_evidence_contradiction_decider",
        ),
        sa.UniqueConstraint("previous_id", name="uq_evidence_contradiction_previous"),
    )
    op.create_index(
        "idx_evidence_contradiction_table",
        "evidence_contradictions",
        ["collection_id", "table_version_id"],
    )
    op.create_table(
        "outcome_certainty_assessments",
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("table_version_id"),
        sa.Column("outcome_key", sa.String(100), nullable=False),
        sa.Column("timepoint", sa.String(100), nullable=False),
        sa.Column("method_key", sa.String(16), nullable=False),
        sa.Column("method_version", sa.String(32), nullable=False),
        sa.Column("starting_level", sa.String(8), nullable=False),
        sa.Column("domains", postgresql.JSONB(), nullable=False),
        sa.Column("level", sa.String(16), nullable=True),
        sa.Column("appraisal_assessment_ids", postgresql.JSONB(), nullable=False),
        sa.Column("contradiction_ids", postgresql.JSONB(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        _uuid("assessed_by_id"),
        sa.Column("actor_role", sa.String(16), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        _uuid("supersedes_certainty_id", nullable=True),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("table_version_id", "evidence_table_versions"),
        _fk("assessed_by_id", "users"),
        _fk("supersedes_certainty_id", "outcome_certainty_assessments"),
        sa.CheckConstraint(
            "starting_level IN ('high','low')", name="ck_certainty_starting_level"
        ),
        sa.CheckConstraint(
            "level IS NULL OR level IN ('very_low','low','moderate','high')",
            name="ck_certainty_level",
        ),
        sa.CheckConstraint("actor_role = 'reviewer'", name="ck_certainty_actor_role"),
        sa.UniqueConstraint("supersedes_certainty_id", name="uq_certainty_supersedes"),
    )
    op.create_index(
        "uq_certainty_initial",
        "outcome_certainty_assessments",
        ["collection_id", "outcome_key", "timepoint"],
        unique=True,
        postgresql_where=sa.text("supersedes_certainty_id IS NULL"),
    )
    for table in _TABLES:
        _deny_data_api(table)
        op.execute(f"""
            CREATE TRIGGER trg_{table}_insert_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION prevent_research_insert_only_mutation()
            """)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER trg_{table}_insert_only ON {table}")
        op.drop_table(table)  # the indexes go with the table
