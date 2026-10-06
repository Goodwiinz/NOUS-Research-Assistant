"""Create search schedules, executions, attempts and results (GOO-319).

Four insert-only tables, each with GOO-309's
``prevent_research_insert_only_mutation()`` trigger (SQLSTATE 55000 on
UPDATE or DELETE), and the data-API lockout. DDL is a frozen copy of
``src.models.research_search_update`` (never import src here). It also
widens ``ck_research_import_receipt_kind`` with ``scheduled_search`` (drop +
create). No data statements.

``UNIQUE(schedule_id, scheduled_local)`` is the claim's idempotency guard;
``UNIQUE(supersedes_schedule_version_id)`` keeps each schedule chain linear.

Downgrade refuses while any evidence row or ``scheduled_search`` receipt
exists, then drops the triggers and the four tables and restores the
two-value kind CHECK. The trigger function stays (``e2a4c6b8d0f1`` owns it).

Revision ID: c2f4b6d8e0a1
Revises: b0e2a4c6d8f9
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "c2f4b6d8e0a1"
down_revision = "b0e2a4c6d8f9"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_SCHEDULES = "research_search_schedules"
_EXECUTIONS = "research_search_executions"
_ATTEMPTS = "research_search_execution_attempts"
_RESULTS = "research_search_execution_results"
_TABLES = (_SCHEDULES, _EXECUTIONS, _ATTEMPTS, _RESULTS)
_RECEIPTS = "research_import_receipts"
_KIND = "ck_research_import_receipt_kind"


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


def _stamp() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    op.create_table(
        _SCHEDULES,
        _uuid("id"),
        _uuid("schedule_id"),
        _uuid("collection_id"),
        _uuid("owner_id"),
        _uuid("protocol_version_id"),
        _uuid("source_run_id"),
        sa.Column("step_id", sa.String(100), nullable=False),
        sa.Column("strategy_version", sa.String(80), nullable=False),
        sa.Column("strategy", postgresql.JSONB(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("cron", sa.String(64), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("baseline_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("baseline_digest", sa.String(64), nullable=True),
        _uuid("supersedes_schedule_version_id", nullable=True),
        _stamp(),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("owner_id", "users"),
        _fk("protocol_version_id", "research_protocol_versions"),
        _fk("source_run_id", "research_runs"),
        _fk("supersedes_schedule_version_id", _SCHEDULES),
        sa.UniqueConstraint(
            "supersedes_schedule_version_id",
            name="uq_research_search_schedules_supersedes",
        ),
        sa.CheckConstraint(
            "(supersedes_schedule_version_id IS NULL) = (baseline_snapshot IS NOT NULL)"
            " AND (baseline_snapshot IS NULL) = (baseline_digest IS NULL)",
            name="ck_research_search_schedules_root",
        ),
        sa.CheckConstraint(
            "supersedes_schedule_version_id IS NOT NULL OR schedule_id = id",
            name="ck_research_search_schedules_id",
        ),
    )
    op.create_index(
        "idx_research_search_schedules_collection", _SCHEDULES, ["collection_id"]
    )
    op.create_index(
        "idx_research_search_schedules_schedule", _SCHEDULES, ["schedule_id"]
    )
    op.create_table(
        _EXECUTIONS,
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("schedule_id"),
        _uuid("schedule_version_id"),
        sa.Column("scheduled_local", sa.String(16), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("missed_fires", sa.Integer(), nullable=False),
        _stamp(),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("schedule_id", _SCHEDULES),
        _fk("schedule_version_id", _SCHEDULES),
        sa.UniqueConstraint(
            "schedule_id", "scheduled_local", name="uq_research_search_executions_fire"
        ),
        sa.CheckConstraint(
            "missed_fires >= 0", name="ck_research_search_executions_missed"
        ),
    )
    op.create_index(
        "idx_research_search_executions_collection", _EXECUTIONS, ["collection_id"]
    )
    op.create_table(
        _ATTEMPTS,
        _uuid("id"),
        _uuid("execution_id"),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("worker", sa.String(100), nullable=False),
        _stamp(),
        sa.PrimaryKeyConstraint("id"),
        _fk("execution_id", _EXECUTIONS),
        sa.CheckConstraint(
            "outcome IN ('started','succeeded','failed','skipped')",
            name="ck_research_search_attempts_outcome",
        ),
        sa.CheckConstraint(
            "outcome IN ('started','succeeded') OR reason IS NOT NULL",
            name="ck_research_search_attempts_reason",
        ),
    )
    op.create_index(
        "idx_research_search_attempts_execution", _ATTEMPTS, ["execution_id"]
    )
    op.create_table(
        _RESULTS,
        _uuid("execution_id"),
        _uuid("baseline_execution_id", nullable=True),
        _uuid("import_receipt_id"),
        sa.Column("citation_chasing", postgresql.JSONB(), nullable=False),
        sa.Column("coverage", postgresql.JSONB(), nullable=False),
        sa.Column("corpus_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("delta", postgresql.JSONB(), nullable=False),
        sa.Column("delta_hash", sa.String(64), nullable=False),
        _stamp(),
        sa.PrimaryKeyConstraint("execution_id"),
        _fk("execution_id", _EXECUTIONS),
        _fk("baseline_execution_id", _EXECUTIONS),
        _fk("import_receipt_id", _RECEIPTS),
    )
    for table in _TABLES:
        _deny_data_api(table)
        op.execute(f"""
            CREATE TRIGGER trg_{table}_insert_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION prevent_research_insert_only_mutation()
            """)
    op.drop_constraint(_KIND, _RECEIPTS, type_="check")
    op.create_check_constraint(
        _KIND, _RECEIPTS, "kind IN ('file_import','citation_chase','scheduled_search')"
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        if bind.execute(sa.text(f"SELECT 1 FROM {table}")).first() is not None:
            raise RuntimeError(f"{table} has rows; refusing to drop retained records")
    scheduled = sa.text(f"SELECT 1 FROM {_RECEIPTS} WHERE kind = 'scheduled_search'")
    if bind.execute(scheduled).first() is not None:
        raise RuntimeError("scheduled_search receipts exist; refusing to downgrade")
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER trg_{table}_insert_only ON {table}")
        op.drop_table(table)
    op.drop_constraint(_KIND, _RECEIPTS, type_="check")
    op.create_check_constraint(
        _KIND, _RECEIPTS, "kind IN ('file_import','citation_chase')"
    )
