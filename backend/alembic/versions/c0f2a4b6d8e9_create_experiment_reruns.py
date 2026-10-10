"""Create insert-only experiment reruns and rerun attempts (GOO-313).

Both tables get GOO-309's ``prevent_research_insert_only_mutation()`` trigger
(SQLSTATE 55000 on UPDATE or DELETE) and the data-API lockout. All DDL is a
frozen copy of ``src.models.research_rerun`` (never import src here). No data
statements.

``UNIQUE(rerun_id, attempt)`` is the single publication point of an attempt;
``ck_experiment_rerun_attempts_executed`` keeps "executed" and "reproduced"
separate (a reproduction verdict exists iff the attempt executed).

Downgrade refuses while any rerun exists (evidence is never dropped
silently), then drops the triggers and tables. The trigger function stays
(``e2a4c6b8d0f1`` owns it).

Revision ID: c0f2a4b6d8e9
Revises: b8e0c2d4f6a7
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "c0f2a4b6d8e9"
down_revision = "b8e0c2d4f6a7"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_TABLES = ("experiment_reruns", "experiment_rerun_attempts")
_STATUSES = (
    "status IN ('restoration_failed','environment_unavailable','execution_failed',"
    "'cancelled','interrupted','executed')"
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


def _uuid(name: str) -> sa.Column:
    return sa.Column(name, _UUID, nullable=False)


def _fk(column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete="RESTRICT")


def _now(name: str) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    op.create_table(
        "experiment_reruns",
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("run_id"),
        _uuid("manifest_id"),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("rule", postgresql.JSONB(), nullable=False),
        sa.Column("rule_hash", sa.String(64), nullable=False),
        _uuid("requested_by_id"),
        sa.Column("actor_role", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("run_id", "research_runs"),
        _fk("manifest_id", "research_run_manifests"),
        _fk("requested_by_id", "users"),
        sa.UniqueConstraint(
            "run_id", "idempotency_key", name="uq_experiment_reruns_key"
        ),
        sa.CheckConstraint(
            "actor_role = 'reviewer'", name="ck_experiment_reruns_actor"
        ),
    )
    op.create_table(
        "experiment_rerun_attempts",
        _uuid("id"),
        _uuid("rerun_id"),
        sa.Column("attempt", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("reproduction", sa.String(16), nullable=True),
        sa.Column("environment_validation", postgresql.JSONB(), nullable=False),
        sa.Column("input_validation", postgresql.JSONB(), nullable=False),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        sa.Column("comparison", postgresql.JSONB(), nullable=True),
        sa.Column("comparison_hash", sa.String(64), nullable=True),
        sa.Column("outputs", postgresql.JSONB(), nullable=False),
        sa.Column("template_id", sa.String(255), nullable=True),
        sa.Column("sandbox_id", sa.String(255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        _now("finished_at"),
        sa.PrimaryKeyConstraint("id"),
        _fk("rerun_id", "experiment_reruns"),
        sa.UniqueConstraint("rerun_id", "attempt", name="uq_experiment_rerun_attempts"),
        sa.CheckConstraint("attempt >= 1", name="ck_experiment_rerun_attempts_attempt"),
        sa.CheckConstraint(_STATUSES, name="ck_experiment_rerun_attempts_status"),
        sa.CheckConstraint(
            "reproduction IS NULL OR reproduction IN ('reproduced','not_reproduced')",
            name="ck_experiment_rerun_attempts_reproduction",
        ),
        sa.CheckConstraint(
            "(status = 'executed') = (reproduction IS NOT NULL)",
            name="ck_experiment_rerun_attempts_executed",
        ),
    )
    for table in _TABLES:
        _deny_data_api(table)
        op.execute(f"""
            CREATE TRIGGER trg_{table}_insert_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION prevent_research_insert_only_mutation()
            """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM experiment_reruns")).first() is not None:
        raise RuntimeError("experiment reruns exist; refusing to drop their evidence")
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER trg_{table}_insert_only ON {table}")
        op.drop_table(table)
