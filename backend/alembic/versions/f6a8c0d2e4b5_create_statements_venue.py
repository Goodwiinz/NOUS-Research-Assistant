"""Create statement sets, approvals, ORCID receipts and venue checks (GOO-316).

Four insert-only tables, each with GOO-309's
``prevent_research_insert_only_mutation()`` trigger (SQLSTATE 55000 on
UPDATE or DELETE) and the data-API lockout. All DDL is a frozen copy of
``src.models.manuscript_statements`` (never import src here). No data
statements.

``fk_manuscript_statement_approvals_set`` binds an approval to its set's
exact hash; ``orcid_authentications`` has no token column at all (only the
ID token's sha256). ``venue_checks`` names the exact package hash it ran on.

Downgrade refuses while any row exists, then drops the triggers and tables.
The trigger function stays (``e2a4c6b8d0f1`` owns it).

Revision ID: f6a8c0d2e4b5
Revises: e4c6a8b0d2f3
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "f6a8c0d2e4b5"
down_revision = "e4c6a8b0d2f3"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_SETS = "manuscript_statement_sets"
_APPROVALS = "manuscript_statement_approvals"
_ORCID = "orcid_authentications"
_VENUE = "venue_checks"
_TABLES = (_SETS, _APPROVALS, _ORCID, _VENUE)


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


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    op.create_table(
        _SETS,
        _uuid("id"),
        _uuid("collection_id"),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.Column("set_hash", sa.String(64), nullable=False),
        sa.Column("schema", sa.String(32), nullable=False),
        _uuid("supersedes_set_id", nullable=True),
        _uuid("created_by_id"),
        sa.Column("actor_role", sa.String(16), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("supersedes_set_id", _SETS),
        _fk("created_by_id", "users"),
        sa.UniqueConstraint(
            "supersedes_set_id", name="uq_manuscript_statement_sets_supersedes"
        ),
        sa.UniqueConstraint("id", "set_hash", name="uq_manuscript_statement_sets_hash"),
        sa.CheckConstraint(
            "actor_role = 'editor'", name="ck_manuscript_statement_sets_role"
        ),
    )
    op.create_index(
        "uq_manuscript_statement_sets_initial",
        _SETS,
        ["collection_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_set_id IS NULL"),
    )
    op.create_index(
        "idx_manuscript_statement_sets_collection", _SETS, ["collection_id"]
    )
    op.create_table(
        _APPROVALS,
        _uuid("id"),
        _uuid("statement_set_id"),
        sa.Column("author_key", sa.String(64), nullable=False),
        sa.Column("set_hash", sa.String(64), nullable=False),
        sa.Column("method", sa.String(24), nullable=False),
        _uuid("approved_by_id"),
        sa.Column("attestation_note", sa.Text(), nullable=True),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["statement_set_id", "set_hash"],
            [f"{_SETS}.id", f"{_SETS}.set_hash"],
            ondelete="RESTRICT",
            name="fk_manuscript_statement_approvals_set",
        ),
        _fk("approved_by_id", "users"),
        sa.UniqueConstraint(
            "statement_set_id",
            "author_key",
            name="uq_manuscript_statement_approvals_author",
        ),
        sa.CheckConstraint(
            "method IN ('in_app_self','recorded_attestation')",
            name="ck_manuscript_statement_approvals_method",
        ),
        sa.CheckConstraint(
            "method <> 'recorded_attestation' OR attestation_note IS NOT NULL",
            name="ck_manuscript_statement_approvals_note",
        ),
    )
    op.create_table(
        _ORCID,
        _uuid("id"),
        _uuid("user_id"),
        sa.Column("orcid", sa.String(19), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("name_claim", sa.String(255), nullable=True),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("token_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id_token_sha256", sa.String(64), nullable=True),
        sa.Column("flow_state_sha256", sa.String(64), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        _fk("user_id", "users"),
        sa.UniqueConstraint(
            "user_id",
            "orcid",
            "token_received_at",
            name="uq_orcid_authentications_receipt",
        ),
        sa.CheckConstraint(
            r"orcid ~ '^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$'",
            name="ck_orcid_authentications_orcid",
        ),
        sa.CheckConstraint(
            "environment IN ('sandbox','production')",
            name="ck_orcid_authentications_env",
        ),
    )
    op.create_index("idx_orcid_authentications_user", _ORCID, ["user_id"])
    op.create_table(
        _VENUE,
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("release_id"),
        sa.Column("profile_id", sa.String(64), nullable=False),
        sa.Column("profile_version", sa.SmallInteger(), nullable=False),
        sa.Column("package_sha256", sa.String(64), nullable=False),
        sa.Column("anonymized_sha256", sa.String(64), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        _uuid("checked_by_id"),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("release_id", "manuscript_releases"),
        _fk("checked_by_id", "users"),
        sa.CheckConstraint("status IN ('pass','fail')", name="ck_venue_checks_status"),
    )
    op.create_index("idx_venue_checks_release", _VENUE, ["release_id"])
    for table in _TABLES:
        _deny_data_api(table)
        op.execute(f"""
            CREATE TRIGGER trg_{table}_insert_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION prevent_research_insert_only_mutation()
            """)


def downgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        if bind.execute(sa.text(f"SELECT 1 FROM {table}")).first() is not None:
            raise RuntimeError(f"{table} has rows; refusing to drop retained records")
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER trg_{table}_insert_only ON {table}")
        op.drop_table(table)
