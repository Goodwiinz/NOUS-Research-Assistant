"""Create verified draft releases (GOO-307).

One insert-only table: a person's promotion of one exact draft version,
snapshotting the claim versions and assessments that authorized it. The only
update is the guarded ``stale_at``/``stale_event_id`` stamp. The partial
unique index allows one live release per draft. All checks are frozen copies
of ``src.models.draft_release`` (never import src here). No backfill: every
existing draft stays a candidate. Downgrade drops the table; releases written
after the upgrade are lost.

Revision ID: d7f9b1c3e5a8
Revises: c4e6a8b0d2f5
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "d7f9b1c3e5a8"
down_revision = "c4e6a8b0d2f5"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_TABLE = "draft_releases"


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
        sa.Column("claim_version_ids", postgresql.JSONB(), nullable=False),
        sa.Column("assessment_ids", postgresql.JSONB(), nullable=False),
        sa.Column(
            "interpretation_claim_version_ids", postgresql.JSONB(), nullable=False
        ),
        _uuid("protocol_version_id", nullable=True),
        sa.Column("policy_version", sa.SmallInteger(), nullable=False),
        _uuid("promoted_by_id"),
        sa.Column("actor_role", sa.String(16), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("stale_at", sa.DateTime(timezone=True), nullable=True),
        _uuid("stale_event_id", nullable=True),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("draft_id", "generated_drafts"),
        _fk("protocol_version_id", "research_protocol_versions"),
        _fk("promoted_by_id", "users"),
        sa.CheckConstraint(
            "actor_role IN ('adjudicator','supervisor')",
            name="ck_draft_releases_actor_role",
        ),
        sa.CheckConstraint(
            "(stale_at IS NULL) = (stale_event_id IS NULL)",
            name="ck_draft_releases_stale_pair",
        ),
    )
    op.create_index(
        "uq_draft_releases_live",
        _TABLE,
        ["draft_id"],
        unique=True,
        postgresql_where=sa.text("stale_at IS NULL"),
    )
    op.create_index("idx_draft_releases_collection", _TABLE, ["collection_id"])
    _deny_data_api(_TABLE)


def downgrade() -> None:
    op.drop_table(_TABLE)  # the indexes go with the table
