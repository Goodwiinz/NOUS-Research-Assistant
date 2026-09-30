"""Create insert-only appraisal assessments (GOO-309).

One table plus the generic ``prevent_research_insert_only_mutation()``
trigger function (SQLSTATE 55000), which GOO-310 and GOO-311 attach to their
own insert-only tables. All checks are frozen copies of
``src.models.research_appraisal`` (never import src here). No data
statements: legacy ``QualityMark`` checks are never backfilled. Downgrade
drops the trigger, the table and the function; appraisals are lost.

Revision ID: e2a4c6b8d0f1
Revises: d7f9b1c3e5a8
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "e2a4c6b8d0f1"
down_revision = "d7f9b1c3e5a8"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_TABLE = "appraisal_assessments"
_KEY = [
    "target_key",
    "outcome_key",
    "timepoint",
    "instrument_key",
    "instrument_version",
]


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


def upgrade() -> None:
    op.create_table(
        _TABLE,
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("protocol_version_id"),
        sa.Column("instrument_key", sa.String(32), nullable=False),
        sa.Column("instrument_version", sa.String(32), nullable=False),
        sa.Column("instrument_spec_hash", sa.String(64), nullable=False),
        _uuid("study_id", nullable=True),
        _uuid("report_id", nullable=True),
        sa.Column("target_key", sa.String(80), nullable=False),
        sa.Column("outcome_key", sa.String(100), nullable=False),
        sa.Column("timepoint", sa.String(100), nullable=False),
        sa.Column("study_design", sa.String(40), nullable=False),
        sa.Column("applicability", sa.String(16), nullable=False),
        sa.Column("domains", postgresql.JSONB(), nullable=False),
        sa.Column("overall", sa.String(16), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("actor_role", sa.String(16), nullable=False),
        _uuid("assessor_id"),
        sa.Column("resolves_assessment_ids", postgresql.JSONB(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=False),
        _uuid("supersedes_assessment_id", nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("protocol_version_id", "research_protocol_versions"),
        _fk("study_id", "research_studies"),
        _fk("report_id", "research_reports"),
        _fk("assessor_id", "users"),
        _fk("supersedes_assessment_id", _TABLE),
        sa.CheckConstraint(
            "(study_id IS NULL) <> (report_id IS NULL)", name="ck_appraisal_target"
        ),
        sa.CheckConstraint(
            "target_key = COALESCE('study:' || study_id::text,"
            " 'report:' || report_id::text)",
            name="ck_appraisal_target_key",
        ),
        sa.CheckConstraint(
            "applicability IN ('applicable','not_applicable')",
            name="ck_appraisal_applicability",
        ),
        sa.CheckConstraint(
            "overall IS NULL OR overall IN ('low','some_concerns','high')",
            name="ck_appraisal_overall",
        ),
        sa.CheckConstraint(
            "applicability = 'applicable' OR overall IS NULL",
            name="ck_appraisal_not_applicable_overall",
        ),
        sa.CheckConstraint(
            "kind IN ('independent','adjudicated')", name="ck_appraisal_kind"
        ),
        sa.CheckConstraint(
            "actor_role IN ('reviewer','adjudicator')", name="ck_appraisal_actor_role"
        ),
        sa.CheckConstraint(
            "(kind = 'adjudicated') = (actor_role = 'adjudicator')",
            name="ck_appraisal_kind_role",
        ),
        sa.CheckConstraint(
            "(kind = 'adjudicated') = (resolves_assessment_ids IS NOT NULL)",
            name="ck_appraisal_resolves",
        ),
        sa.CheckConstraint(
            "kind <> 'adjudicated' OR rationale IS NOT NULL",
            name="ck_appraisal_adjudication_rationale",
        ),
        sa.UniqueConstraint("supersedes_assessment_id", name="uq_appraisal_supersedes"),
    )
    op.create_index(
        "uq_appraisal_initial_independent",
        _TABLE,
        ["collection_id", "assessor_id", *_KEY],
        unique=True,
        postgresql_where=sa.text(
            "supersedes_assessment_id IS NULL AND kind = 'independent'"
        ),
    )
    op.create_index(
        "uq_appraisal_initial_adjudicated",
        _TABLE,
        ["collection_id", *_KEY],
        unique=True,
        postgresql_where=sa.text(
            "supersedes_assessment_id IS NULL AND kind = 'adjudicated'"
        ),
    )
    op.create_index(
        "idx_appraisal_collection_key",
        _TABLE,
        ["collection_id", "target_key", "outcome_key", "timepoint"],
    )
    _deny_data_api(_TABLE)
    op.execute("""
        CREATE FUNCTION prevent_research_insert_only_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'research rows are insert-only'
                USING ERRCODE = '55000';
        END;
        $$
        """)
    op.execute(f"""
        CREATE TRIGGER trg_appraisal_assessments_insert_only
        BEFORE UPDATE OR DELETE ON {_TABLE}
        FOR EACH ROW EXECUTE FUNCTION prevent_research_insert_only_mutation()
        """)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER trg_appraisal_assessments_insert_only ON {_TABLE}")
    op.drop_table(_TABLE)  # the indexes go with the table
    op.execute("DROP FUNCTION prevent_research_insert_only_mutation()")
