"""Create the retained research decision ledger.

Revision ID: a3c5e7f901b2
Revises: x6y7z8a9b0c1
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "a3c5e7f901b2"
down_revision = "x6y7z8a9b0c1"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")


def _deny_data_api(table: str) -> None:
    # Resolve through the migration connection's search_path. Production uses
    # ``public``; isolated PostgreSQL migration tests use a dedicated schema.
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


def upgrade() -> None:
    op.create_table(
        "research_decision_streams",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("next_seq", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("next_seq > 0", name="ck_research_decision_stream_next_seq"),
        sa.ForeignKeyConstraint(
            ["collection_id"], ["collections.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "aggregate_type",
            "aggregate_id",
            name="uq_research_decision_stream_aggregate",
        ),
    )
    op.create_index(
        "idx_research_decision_stream_collection",
        "research_decision_streams",
        ["collection_id"],
    )
    op.create_table(
        "research_decision_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stream_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_schema_version", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_role", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("subject_hash", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("seq > 0", name="ck_research_decision_event_seq"),
        sa.CheckConstraint(
            "event_schema_version > 0",
            name="ck_research_decision_event_schema_version",
        ),
        sa.CheckConstraint(
            "length(subject_hash) = 64",
            name="ck_research_decision_event_subject_hash",
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_research_decision_event_request_fingerprint",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["collection_id"], ["collections.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["research_decision_streams.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stream_id", "seq", name="uq_research_decision_event_stream_seq"
        ),
        sa.UniqueConstraint(
            "stream_id",
            "idempotency_key",
            name="uq_research_decision_event_stream_idempotency",
        ),
    )
    op.create_index(
        "idx_research_decision_event_collection_seq",
        "research_decision_events",
        ["collection_id", "seq"],
    )
    op.create_index(
        "idx_research_decision_event_stream_seq",
        "research_decision_events",
        ["stream_id", "seq"],
    )
    op.execute("""
        CREATE FUNCTION prevent_research_decision_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'research decision events are append-only'
                USING ERRCODE = '55000';
        END;
        $$
        """)
    op.execute("""
        CREATE TRIGGER trg_research_decision_events_append_only
        BEFORE UPDATE OR DELETE ON research_decision_events
        FOR EACH ROW EXECUTE FUNCTION prevent_research_decision_event_mutation()
        """)
    _deny_data_api("research_decision_streams")
    _deny_data_api("research_decision_events")


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_research_decision_events_append_only "
        "ON research_decision_events"
    )
    op.execute("DROP FUNCTION prevent_research_decision_event_mutation()")
    op.drop_index(
        "idx_research_decision_event_stream_seq",
        table_name="research_decision_events",
    )
    op.drop_index(
        "idx_research_decision_event_collection_seq",
        table_name="research_decision_events",
    )
    op.drop_table("research_decision_events")
    op.drop_index(
        "idx_research_decision_stream_collection",
        table_name="research_decision_streams",
    )
    op.drop_table("research_decision_streams")
