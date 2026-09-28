"""Provider acceptance, quarantined workspace writers and recovery status.

Revision ID: hb02_harness_runs
Revises: hb01_integration_grants
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "hb02_harness_runs"
down_revision = "hb01_integration_grants"
branch_labels = None
depends_on = None

_OLD_STATUSES = "'queued', 'running', 'awaiting_confirmation', 'stopping', 'completed', 'failed', 'cancelled'"
_OLD_ACTIVE = "'queued', 'running', 'awaiting_confirmation', 'stopping'"


def _status_constraints(recovery: bool) -> None:
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    statuses = _OLD_STATUSES + (", 'recovering'" if recovery else "")
    op.create_check_constraint(
        "ck_agent_runs_status", "agent_runs", f"status IN ({statuses})"
    )
    op.drop_index("uq_agent_runs_active_thread", table_name="agent_runs")
    active = _OLD_ACTIVE + (", 'recovering'" if recovery else "")
    op.create_index(
        "uq_agent_runs_active_thread",
        "agent_runs",
        ["thread_id"],
        unique=True,
        postgresql_where=sa.text(f"thread_id IS NOT NULL AND status IN ({active})"),
    )


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column(
            "execution_provider", sa.String(16), nullable=False, server_default="nous"
        ),
    )
    op.create_check_constraint(
        "ck_agent_runs_provider",
        "agent_runs",
        "execution_provider IN ('nous', 'codex')",
    )
    _status_constraints(True)
    op.create_table(
        "harness_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.job_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "grant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("integration_grants.id"),
            nullable=False,
        ),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_session_id", sa.String(255)),
        sa.Column("provider_turn_id", sa.String(255)),
        sa.Column(
            "observation", sa.String(16), nullable=False, server_default="unknown"
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "workspace_locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.ForeignKeyConstraint(
            ["device_id", "workspace_id"],
            [
                "bridge_workspace_bindings.device_id",
                "bridge_workspace_bindings.workspace_id",
            ],
        ),
        sa.CheckConstraint(
            "observation IN ('unknown', 'running', 'completed', 'failed', 'interrupted')",
            name="ck_harness_observation",
        ),
    )
    op.create_index(
        "uq_harness_active_workspace",
        "harness_sessions",
        ["device_id", "workspace_id"],
        unique=True,
        postgresql_where=sa.text("workspace_locked"),
    )


def downgrade() -> None:
    # Downgrade must never discard an uncertain writer's only durable lock.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM harness_sessions WHERE workspace_locked) THEN RAISE EXCEPTION 'Active harness runs prevent downgrade'; END IF; END $$;"
    )
    op.drop_table("harness_sessions")
    _status_constraints(False)
    op.drop_constraint("ck_agent_runs_provider", "agent_runs", type_="check")
    op.drop_column("agent_runs", "execution_provider")
