"""Create protocol-bound screening queue tables (GOO-301).

Revision ID: e1f3a5c7d9b2
Revises: d4e6f8a0b2c3
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "e1f3a5c7d9b2"
down_revision = "d4e6f8a0b2c3"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)


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


def _id() -> sa.Column:
    return sa.Column("id", _UUID, nullable=False)


def _uuid(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, _UUID, nullable=nullable)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def _fk(column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete="RESTRICT")


def upgrade() -> None:
    op.create_table(
        "screening_queues",
        _id(),
        _uuid("collection_id"),
        _uuid("protocol_version_id"),
        sa.Column("criteria_hash", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("report_ids", postgresql.JSONB(), nullable=False),
        _uuid("supersedes_queue_id", nullable=True),
        _uuid("created_by_id"),
        _created_at(),
        sa.CheckConstraint(
            "stage IN ('title_abstract','full_text')", name="ck_screening_queue_stage"
        ),
        _fk("collection_id", "collections"),
        _fk("protocol_version_id", "research_protocol_versions"),
        _fk("supersedes_queue_id", "screening_queues"),
        _fk("created_by_id", "users"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "supersedes_queue_id", name="uq_screening_queue_supersedes"
        ),
    )
    op.create_table(
        "screening_assignments",
        _id(),
        _uuid("queue_id"),
        _uuid("reviewer_id"),
        _uuid("assigned_by_id"),
        _created_at(),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        _uuid("revoked_by_id", nullable=True),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by_id IS NULL)",
            name="ck_screening_assignment_revocation",
        ),
        _fk("queue_id", "screening_queues"),
        _fk("reviewer_id", "users"),
        _fk("assigned_by_id", "users"),
        _fk("revoked_by_id", "users"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "screening_observations",
        _id(),
        _uuid("queue_id"),
        _uuid("report_id"),
        _uuid("reviewer_id"),
        _uuid("assignment_id"),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("exclusion_reason", sa.String(length=200), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        _uuid("supersedes_observation_id", nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "decision IN ('include','exclude','uncertain')",
            name="ck_screening_observation_decision",
        ),
        sa.CheckConstraint(
            "decision = 'exclude' OR exclusion_reason IS NULL",
            name="ck_screening_observation_reason",
        ),
        _fk("queue_id", "screening_queues"),
        _fk("report_id", "research_reports"),
        _fk("reviewer_id", "users"),
        _fk("assignment_id", "screening_assignments"),
        _fk("supersedes_observation_id", "screening_observations"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "supersedes_observation_id", name="uq_screening_observation_supersedes"
        ),
    )
    op.create_table(
        "screening_suggestions",
        _id(),
        _uuid("queue_id"),
        _uuid("report_id"),
        _uuid("source_id"),
        _uuid("step_id"),
        sa.Column("model_id", sa.String(length=100), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "decision IN ('include','exclude')",
            name="ck_screening_suggestion_decision",
        ),
        _fk("queue_id", "screening_queues"),
        _fk("report_id", "research_reports"),
        _fk("source_id", "research_sources"),
        _fk("step_id", "research_steps"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "queue_id", "step_id", "source_id", name="uq_screening_suggestion_source"
        ),
    )
    op.create_index(
        "idx_screening_queue_collection", "screening_queues", ["collection_id"]
    )
    op.create_index(
        "uq_screening_assignment_active",
        "screening_assignments",
        ["queue_id", "reviewer_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "uq_screening_observation_initial",
        "screening_observations",
        ["queue_id", "report_id", "reviewer_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_observation_id IS NULL"),
    )
    op.create_index(
        "idx_screening_observation_reviewer",
        "screening_observations",
        ["queue_id", "reviewer_id"],
    )
    for table in _TABLES:
        _deny_data_api(table)


_TABLES = (
    "screening_queues",
    "screening_assignments",
    "screening_observations",
    "screening_suggestions",
)


def downgrade() -> None:
    # Indexes go with their tables.
    for table in reversed(_TABLES):
        op.drop_table(table)
