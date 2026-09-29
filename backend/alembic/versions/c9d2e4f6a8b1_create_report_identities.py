"""Create project-scoped report/study identity tables (GOO-299).

Revision ID: c9d2e4f6a8b1
Revises: merge_daily_harness_20260928
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "c9d2e4f6a8b1"
down_revision = "merge_daily_harness_20260928"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)


def _deny_data_api(table: str) -> None:
    # Copied from a3c5e7f901b2: resolve through the connection's search_path.
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


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column("id", _UUID, nullable=False),
        sa.Column("collection_id", _UUID, nullable=False),
    ]


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def _collection_fk() -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["collection_id"], ["collections.id"], ondelete="RESTRICT"
    )


def upgrade() -> None:
    op.create_table(
        "research_studies",
        *_base_columns(),
        sa.Column("label", sa.String(length=500), nullable=False),
        _created_at(),
        _collection_fk(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "research_reports",
        *_base_columns(),
        sa.Column("title_snapshot", sa.String(length=500), nullable=False),
        sa.Column("merged_into_report_id", _UUID, nullable=True),
        sa.Column("study_id", _UUID, nullable=True),
        sa.Column("study_link_status", sa.String(length=16), nullable=True),
        sa.Column("study_link_actor_id", _UUID, nullable=True),
        sa.Column("study_link_rationale", sa.Text(), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "study_link_status IN ('proposed','confirmed','disputed')",
            name="ck_research_report_study_link_status",
        ),
        sa.CheckConstraint(
            "(study_id IS NULL) = (study_link_status IS NULL)",
            name="ck_research_report_study_link_pair",
        ),
        _collection_fk(),
        sa.ForeignKeyConstraint(
            ["merged_into_report_id"], ["research_reports.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["study_id"], ["research_studies.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["study_link_actor_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "research_report_identifiers",
        *_base_columns(),
        sa.Column("report_id", _UUID, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("value", sa.String(length=512), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "kind IN ('doi','pmid','pmcid','arxiv_base','openalex','semantic_scholar')",
            name="ck_research_report_identifier_kind",
        ),
        _collection_fk(),
        sa.ForeignKeyConstraint(
            ["report_id"], ["research_reports.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "collection_id",
            "kind",
            "value",
            name="uq_research_report_identifier_value",
        ),
    )
    op.create_table(
        "research_report_observations",
        *_base_columns(),
        sa.Column("report_id", _UUID, nullable=False),
        sa.Column("source_id", _UUID, nullable=False),
        sa.Column("match_method", sa.String(length=32), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), server_default="{}", nullable=False),
        _created_at(),
        _collection_fk(),
        sa.ForeignKeyConstraint(
            ["report_id"], ["research_reports.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["research_sources.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", name="uq_research_report_observation_source"),
    )
    for table, name, columns in _INDEXES:
        op.create_index(name, table, columns)
    for table in _TABLES:
        _deny_data_api(table)


_TABLES = (
    "research_studies",
    "research_reports",
    "research_report_identifiers",
    "research_report_observations",
)
_INDEXES = (
    ("research_studies", "idx_research_study_collection", ["collection_id"]),
    ("research_reports", "idx_research_report_collection", ["collection_id"]),
    (
        "research_report_identifiers",
        "idx_research_report_identifier_collection",
        ["collection_id"],
    ),
    (
        "research_report_identifiers",
        "idx_research_report_identifier_report",
        ["report_id"],
    ),
    (
        "research_report_observations",
        "idx_research_report_observation_collection",
        ["collection_id"],
    ),
    (
        "research_report_observations",
        "idx_research_report_observation_report",
        ["report_id"],
    ),
)


def downgrade() -> None:
    for table, name, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
    for table in reversed(_TABLES):
        op.drop_table(table)
