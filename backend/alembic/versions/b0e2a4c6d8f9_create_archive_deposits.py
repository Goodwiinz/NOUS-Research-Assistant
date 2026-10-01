"""Create archive deposit approvals, attempts and outbox (GOO-318).

Two insert-only evidence tables, each with GOO-309's
``prevent_research_insert_only_mutation()`` trigger (SQLSTATE 55000 on
UPDATE or DELETE), and one mutable work queue (``archive_deposit_outbox``,
the ``artifact_lifecycle_outbox`` shape) that holds no evidence. All three
get the data-API lockout. DDL is a frozen copy of
``src.models.research_deposit`` (never import src here). No data statements.

``UNIQUE(previous_id)`` keeps each attempt chain linear; the partial unique
indexes on the ``prepared`` row give one idempotency key per Collection and
one live operation per (release, repository).

Downgrade refuses while any evidence row exists, then drops the triggers and
tables. The trigger function stays (``e2a4c6b8d0f1`` owns it).

Revision ID: b0e2a4c6d8f9
Revises: f6a8c0d2e4b5
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "b0e2a4c6d8f9"
down_revision = "f6a8c0d2e4b5"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_APPROVALS = "archive_deposit_approvals"
_ATTEMPTS = "archive_deposit_attempts"
_OUTBOX = "archive_deposit_outbox"
_EVIDENCE = (_APPROVALS, _ATTEMPTS)
_PREPARED = sa.text("phase = 'prepared'")
_REPOSITORY = "repository = 'zenodo_sandbox'"


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


def _stamp(name: str = "created_at") -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    op.create_table(
        _APPROVALS,
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("release_id"),
        sa.Column("package_sha256", sa.String(64), nullable=False),
        sa.Column("repository", sa.String(32), nullable=False),
        sa.Column("account_ref", sa.String(128), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        _uuid("approved_by_id"),
        sa.Column("actor_role", sa.String(16), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        _stamp(),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("release_id", "manuscript_releases"),
        _fk("approved_by_id", "users"),
        sa.CheckConstraint(
            "kind IN ('approved','revoked')", name="ck_archive_deposit_approvals_kind"
        ),
        sa.CheckConstraint(
            "action = 'publish'", name="ck_archive_deposit_approvals_action"
        ),
        sa.CheckConstraint(_REPOSITORY, name="ck_archive_deposit_approvals_repository"),
        sa.CheckConstraint(
            "actor_role IN ('adjudicator','supervisor')",
            name="ck_archive_deposit_approvals_role",
        ),
    )
    op.create_index("idx_archive_deposit_approvals_release", _APPROVALS, ["release_id"])
    op.create_table(
        _ATTEMPTS,
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("operation_id"),
        _uuid("release_id"),
        sa.Column("repository", sa.String(32), nullable=False),
        sa.Column("account_ref", sa.String(128), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=True),
        _uuid("requested_by_id"),
        _uuid("approval_id", nullable=True),
        sa.Column("files", postgresql.JSONB(), nullable=False),
        sa.Column("remote_deposition_id", sa.String(64), nullable=True),
        sa.Column("remote_record_id", sa.String(64), nullable=True),
        sa.Column("doi", sa.String(255), nullable=True),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("response", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.String(200), nullable=True),
        _uuid("previous_id", nullable=True),
        _stamp(),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("release_id", "manuscript_releases"),
        _fk("requested_by_id", "users"),
        _fk("approval_id", _APPROVALS),
        _fk("previous_id", _ATTEMPTS),
        sa.UniqueConstraint("previous_id", name="uq_archive_deposit_attempts_previous"),
        sa.CheckConstraint(
            "phase IN ('prepared','draft_created','files_uploaded',"
            "'published','verified')",
            name="ck_archive_deposit_attempts_phase",
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded','failed','unknown')",
            name="ck_archive_deposit_attempts_outcome",
        ),
        sa.CheckConstraint(
            "(phase = 'prepared') = (previous_id IS NULL)",
            name="ck_archive_deposit_attempts_root",
        ),
        sa.CheckConstraint(
            "phase <> 'prepared' OR operation_id = id",
            name="ck_archive_deposit_attempts_operation",
        ),
        sa.CheckConstraint(_REPOSITORY, name="ck_archive_deposit_attempts_repo"),
    )
    op.create_index(
        "uq_deposit_idempotency",
        _ATTEMPTS,
        ["collection_id", "idempotency_key"],
        unique=True,
        postgresql_where=_PREPARED,
    )
    op.create_index(
        "uq_deposit_live",
        _ATTEMPTS,
        ["release_id", "repository"],
        unique=True,
        postgresql_where=_PREPARED,
    )
    op.create_index(
        "idx_archive_deposit_attempts_operation", _ATTEMPTS, ["operation_id"]
    )
    op.create_index(
        "idx_archive_deposit_attempts_collection", _ATTEMPTS, ["collection_id"]
    )
    op.create_table(
        _OUTBOX,
        _uuid("id"),
        _uuid("operation_id"),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        _stamp(),
        _stamp("updated_at"),
        sa.Column("last_error", sa.String(200), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        _fk("operation_id", _ATTEMPTS),
        sa.UniqueConstraint("operation_id", name="uq_archive_deposit_outbox_operation"),
        sa.CheckConstraint(
            "status IN ('pending','processing','done','skipped')",
            name="ck_archive_deposit_outbox_status",
        ),
    )
    op.create_index("idx_archive_deposit_outbox_status", _OUTBOX, ["status"])
    for table in (*_EVIDENCE, _OUTBOX):
        _deny_data_api(table)
    for table in _EVIDENCE:
        op.execute(f"""
            CREATE TRIGGER trg_{table}_insert_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION prevent_research_insert_only_mutation()
            """)


def downgrade() -> None:
    bind = op.get_bind()
    for table in _EVIDENCE:
        if bind.execute(sa.text(f"SELECT 1 FROM {table}")).first() is not None:
            raise RuntimeError(f"{table} has rows; refusing to drop retained records")
    op.drop_table(_OUTBOX)
    for table in reversed(_EVIDENCE):
        op.execute(f"DROP TRIGGER trg_{table}_insert_only ON {table}")
        op.drop_table(table)
