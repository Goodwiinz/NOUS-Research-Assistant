"""Create insert-only candidate and verified manuscript releases (GOO-315).

The table gets GOO-309's ``prevent_research_insert_only_mutation()`` trigger
(SQLSTATE 55000 on UPDATE or DELETE) and the data-API lockout. All DDL is a
frozen copy of ``src.models.manuscript_release`` (never import src here). No
data statements.

``ck_manuscript_releases_verified`` makes a verified row name its candidate
and its GOO-307 ``draft_release``; ``ck_manuscript_releases_stage_role``
makes a candidate an editor's and a promotion an adjudicator's or
supervisor's. ``UNIQUE(candidate_release_id)`` is the concurrency backstop:
one verified promotion per candidate.

Downgrade refuses while any release exists (packages are never orphaned
silently), then drops the trigger and table. The trigger function stays
(``e2a4c6b8d0f1`` owns it).

Revision ID: e4c6a8b0d2f3
Revises: d2a4c6e8f0b1
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "e4c6a8b0d2f3"
down_revision = "d2a4c6e8f0b1"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_TABLE = "manuscript_releases"
_VERIFIED_SHAPE = (
    "(stage = 'verified')"
    " = (candidate_release_id IS NOT NULL AND draft_release_id IS NOT NULL)"
)
_STAGE_ROLE = (
    "(stage = 'candidate' AND actor_role = 'editor')"
    " OR (stage = 'verified' AND actor_role IN ('adjudicator','supervisor'))"
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


def _uuid(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, _UUID, nullable=nullable)


def _fk(column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete="RESTRICT")


def upgrade() -> None:
    op.create_table(
        _TABLE,
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("draft_id"),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        _uuid("candidate_release_id", nullable=True),
        _uuid("draft_release_id", nullable=True),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("checks", postgresql.JSONB(), nullable=False),
        sa.Column("checks_hash", sa.String(64), nullable=False),
        sa.Column("package_files", postgresql.JSONB(), nullable=False),
        sa.Column("package_sha256", sa.String(64), nullable=False),
        sa.Column("package_storage_key", sa.String(512), nullable=False),
        _uuid("created_by_id"),
        sa.Column("actor_role", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("draft_id", "generated_drafts"),
        _fk("candidate_release_id", _TABLE),
        _fk("draft_release_id", "draft_releases"),
        _fk("created_by_id", "users"),
        sa.UniqueConstraint(
            "candidate_release_id", name="uq_manuscript_releases_candidate"
        ),
        sa.CheckConstraint(
            "stage IN ('candidate','verified')", name="ck_manuscript_releases_stage"
        ),
        sa.CheckConstraint(
            "actor_role IN ('editor','adjudicator','supervisor')",
            name="ck_manuscript_releases_actor_role",
        ),
        sa.CheckConstraint(_VERIFIED_SHAPE, name="ck_manuscript_releases_verified"),
        sa.CheckConstraint(_STAGE_ROLE, name="ck_manuscript_releases_stage_role"),
    )
    op.create_index("idx_manuscript_releases_collection", _TABLE, ["collection_id"])
    op.create_index("idx_manuscript_releases_draft", _TABLE, ["draft_id"])
    _deny_data_api(_TABLE)
    op.execute(f"""
        CREATE TRIGGER trg_{_TABLE}_insert_only
        BEFORE UPDATE OR DELETE ON {_TABLE}
        FOR EACH ROW EXECUTE FUNCTION prevent_research_insert_only_mutation()
        """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT 1 FROM {_TABLE}")).first() is not None:
        raise RuntimeError("manuscript releases exist; refusing to drop their packages")
    op.execute(f"DROP TRIGGER trg_{_TABLE}_insert_only ON {_TABLE}")
    op.drop_table(_TABLE)
