"""Create full-text acquisition requests and attempts (GOO-303).

Revision ID: f2a4c6e8b0d3
Revises: f3b5d7e9a1c4
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "f2a4c6e8b0d3"
down_revision = "f3b5d7e9a1c4"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_REQUESTS = "research_fulltext_requests"
_ATTEMPTS = "research_fulltext_attempts"


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


def _now(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        _REQUESTS,
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("report_id"),
        _uuid("requested_by_id"),
        _now("requested_at"),
        _uuid("protocol_version_id", nullable=True),
        _fk("collection_id", "collections"),
        _fk("report_id", "research_reports"),
        _fk("requested_by_id", "users"),
        _fk("protocol_version_id", "research_protocol_versions"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "collection_id", "report_id", name="uq_research_fulltext_request_report"
        ),
    )
    op.create_index(
        "idx_research_fulltext_request_collection", _REQUESTS, ["collection_id"]
    )
    op.create_table(
        _ATTEMPTS,
        _uuid("id"),
        _uuid("request_id"),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("attempted_on", sa.Date(), nullable=False),
        _uuid("actor_id"),
        _now("created_at"),
        _uuid("document_id", nullable=True),
        sa.Column("document_content_hash", sa.String(length=64), nullable=True),
        _uuid("previous_attempt_id", nullable=True),
        sa.CheckConstraint(
            "outcome IN ('requested','retrieved','unavailable')",
            name="ck_research_fulltext_attempt_outcome",
        ),
        sa.CheckConstraint(
            "(outcome = 'retrieved') = (document_id IS NOT NULL)",
            name="ck_research_fulltext_attempt_document",
        ),
        sa.CheckConstraint(
            "(document_id IS NULL) = (document_content_hash IS NULL)",
            name="ck_research_fulltext_attempt_hash",
        ),
        sa.CheckConstraint(
            "outcome <> 'unavailable' OR reason IS NOT NULL",
            name="ck_research_fulltext_attempt_reason",
        ),
        _fk("request_id", _REQUESTS),
        _fk("actor_id", "users"),
        _fk("document_id", "documents"),
        # Same-request only: the previous attempt must share request_id.
        sa.ForeignKeyConstraint(
            ["request_id", "previous_attempt_id"],
            [f"{_ATTEMPTS}.request_id", f"{_ATTEMPTS}.id"],
            ondelete="RESTRICT",
            name="fk_research_fulltext_attempt_previous",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "previous_attempt_id", name="uq_research_fulltext_attempt_previous"
        ),
        sa.UniqueConstraint(
            "request_id", "id", name="uq_research_fulltext_attempt_chain"
        ),
    )
    op.create_index(
        "uq_research_fulltext_attempt_head",
        _ATTEMPTS,
        ["request_id"],
        unique=True,
        postgresql_where=sa.text("previous_attempt_id IS NULL"),
    )
    op.create_index("idx_research_fulltext_attempt_request", _ATTEMPTS, ["request_id"])
    _deny_data_api(_REQUESTS)
    _deny_data_api(_ATTEMPTS)


def downgrade() -> None:
    # The indexes go with the tables.
    op.drop_table(_ATTEMPTS)
    op.drop_table(_REQUESTS)
