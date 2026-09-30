"""Durable bridge command leases and source replay receipts.

Revision ID: hb03_bridge_delivery
Revises: hb02_harness_runs
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "hb03_bridge_delivery"
down_revision = "hb02_harness_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "harness_sessions",
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("harness_sessions", sa.Column("source_id", sa.String(128)))
    op.add_column(
        "harness_sessions",
        sa.Column("source_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "harness_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("harness_sessions.run_id"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column(
            "acknowledged",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "uq_harness_command_kind",
        "harness_commands",
        ["run_id", "generation", "kind"],
        unique=True,
        postgresql_where=sa.text("kind IN ('start', 'interrupt')"),
    )
    op.create_table(
        "harness_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("harness_sessions.run_id"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("source_seq", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("canonical_seq", sa.Integer()),
    )
    op.create_index(
        "uq_harness_source_seq",
        "harness_receipts",
        ["run_id", "generation", "source_id", "source_seq"],
        unique=True,
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM harness_sessions WHERE workspace_locked) THEN RAISE EXCEPTION 'Active harness runs prevent downgrade'; END IF; END $$;"
    )
    op.drop_table("harness_receipts")
    op.drop_table("harness_commands")
    for column in ("source_seq", "source_id", "generation"):
        op.drop_column("harness_sessions", column)
