"""Create the insert-only screening resolution chain (GOO-302).

Revision ID: f3b5d7e9a1c4
Revises: e1f3a5c7d9b2
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "f3b5d7e9a1c4"
down_revision = "e1f3a5c7d9b2"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_TABLE = "screening_resolutions"


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
        _uuid("queue_id"),
        _uuid("report_id"),
        sa.Column("basis", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("exclusion_reason", sa.String(length=200), nullable=True),
        sa.Column("input_observation_ids", postgresql.JSONB(), nullable=False),
        sa.Column("criteria_hash", sa.String(length=64), nullable=False),
        _uuid("supersedes_resolution_id", nullable=True),
        _uuid("event_id"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "basis IN ('single','agreement','conflict','adjudicated','reopened')",
            name="ck_screening_resolution_basis",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('include','exclude','uncertain')",
            name="ck_screening_resolution_outcome",
        ),
        sa.CheckConstraint(
            "(basis IN ('single','agreement','adjudicated')) = (outcome IS NOT NULL)",
            name="ck_screening_resolution_outcome_basis",
        ),
        sa.CheckConstraint(
            "exclusion_reason IS NULL OR COALESCE(outcome, '') = 'exclude'",
            name="ck_screening_resolution_reason",
        ),
        _fk("queue_id", "screening_queues"),
        _fk("report_id", "research_reports"),
        _fk("supersedes_resolution_id", _TABLE),
        _fk("event_id", "research_decision_events"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "supersedes_resolution_id", name="uq_screening_resolution_supersedes"
        ),
    )
    op.create_index(
        "uq_screening_resolution_initial",
        _TABLE,
        ["queue_id", "report_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_resolution_id IS NULL"),
    )
    op.create_index(
        "idx_screening_resolution_report", _TABLE, ["queue_id", "report_id"]
    )
    _deny_data_api(_TABLE)


def downgrade() -> None:
    # The indexes go with the table.
    op.drop_table(_TABLE)
