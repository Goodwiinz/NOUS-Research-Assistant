"""Create superseding review versions and release links (GOO-320).

Two insert-only tables, each with GOO-309's
``prevent_research_insert_only_mutation()`` trigger (SQLSTATE 55000 on
UPDATE or DELETE), and the data-API lockout. DDL is a frozen copy of
``src.models.research_review_version`` (never import src here). No data
statements.

``UNIQUE(parent_review_version_id)`` keeps a Collection's version chain
linear (a second successor of one parent loses), the partial unique index
allows one root per Collection, and ``UNIQUE(accepted_execution_id)`` lets a
GOO-319 delta be accepted once. A release link supersedes at most one
release (``UNIQUE(supersedes_release_id)``).

Downgrade refuses while any row exists, then drops the triggers and the two
tables only. The trigger function stays (``e2a4c6b8d0f1`` owns it).

Revision ID: d4a6c8e0f2b3
Revises: c2f4b6d8e0a1
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "d4a6c8e0f2b3"
down_revision = "c2f4b6d8e0a1"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_VERSIONS = "research_review_versions"
_LINKS = "research_review_release_links"
_TABLES = (_VERSIONS, _LINKS)


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


def _json(name: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(), nullable=False)


def _stamp() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    op.create_table(
        _VERSIONS,
        _uuid("id"),
        _uuid("collection_id"),
        sa.Column("version_number", sa.Integer(), nullable=False),
        _uuid("parent_review_version_id", nullable=True),
        _uuid("accepted_execution_id", nullable=True),
        sa.Column("delta_hash", sa.String(64), nullable=True),
        _uuid("protocol_version_id"),
        sa.Column("strategy_version", sa.String(80), nullable=True),
        _json("input_versions"),
        _json("report_ids"),
        _json("carried"),
        _json("required_work"),
        _json("needs_attention"),
        _json("missing_history"),
        sa.Column("prisma_body_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        _uuid("created_by_id"),
        sa.Column("actor_role", sa.String(16), nullable=False),
        _stamp(),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("parent_review_version_id", _VERSIONS),
        _fk("accepted_execution_id", "research_search_executions"),
        _fk("protocol_version_id", "research_protocol_versions"),
        _fk("created_by_id", "users"),
        sa.UniqueConstraint(
            "parent_review_version_id", name="uq_research_review_versions_parent"
        ),
        sa.UniqueConstraint(
            "accepted_execution_id", name="uq_research_review_versions_execution"
        ),
        sa.CheckConstraint(
            "(parent_review_version_id IS NULL) = (accepted_execution_id IS NULL)"
            " AND (accepted_execution_id IS NULL) = (delta_hash IS NULL)"
            " AND (parent_review_version_id IS NULL) = (version_number = 1)",
            name="ck_research_review_versions_root",
        ),
        sa.CheckConstraint(
            "actor_role = 'supervisor'", name="ck_research_review_versions_role"
        ),
    )
    op.create_index(
        "uq_review_version_root",
        _VERSIONS,
        ["collection_id"],
        unique=True,
        postgresql_where=sa.text("parent_review_version_id IS NULL"),
    )
    op.create_index(
        "idx_research_review_versions_collection", _VERSIONS, ["collection_id"]
    )
    op.create_table(
        _LINKS,
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("review_version_id"),
        _uuid("release_id"),
        _uuid("supersedes_release_id", nullable=True),
        _uuid("linked_by_id"),
        _stamp(),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("review_version_id", _VERSIONS),
        _fk("release_id", "manuscript_releases"),
        _fk("supersedes_release_id", "manuscript_releases"),
        _fk("linked_by_id", "users"),
        sa.UniqueConstraint(
            "review_version_id", name="uq_research_review_release_links_version"
        ),
        sa.UniqueConstraint(
            "supersedes_release_id",
            name="uq_research_review_release_links_supersedes",
        ),
    )
    op.create_index(
        "idx_research_review_release_links_collection", _LINKS, ["collection_id"]
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
    for table in _TABLES:
        if bind.execute(sa.text(f"SELECT 1 FROM {table}")).first() is not None:
            raise RuntimeError(f"{table} has rows; refusing to drop retained records")
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER trg_{table}_insert_only ON {table}")
        op.drop_table(table)
