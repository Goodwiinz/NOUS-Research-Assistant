"""Create insert-only run manifests, run artifacts, figures and the figure link (GOO-312).

``research_run_manifests``, ``research_run_artifacts`` and ``research_figures``
get GOO-309's ``prevent_research_insert_only_mutation()`` trigger (SQLSTATE
55000 on UPDATE or DELETE). ``research_claim_evidence_links`` gains a nullable
``figure_id`` and its kind/shape CHECKs admit a fifth kind; existing rows
satisfy the new CHECKs because the column is NULL. All DDL is a frozen copy of
``src.models.research_experiment`` and ``src.models.research_claim`` (never
import src here). No data statements.

Downgrade refuses while any ``figure`` link or manifest row exists (evidence is
never dropped silently), then restores GOO-311's CHECKs and drops the column,
triggers and tables. The trigger function stays (``e2a4c6b8d0f1`` owns it).

Revision ID: b8e0c2d4f6a7
Revises: a6c8e0b2d4f5
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "b8e0c2d4f6a7"
down_revision = "a6c8e0b2d4f5"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_LINKS = "research_claim_evidence_links"
_TABLES = ("research_run_manifests", "research_run_artifacts", "research_figures")

_OLD_KIND = (
    "kind IN ('extraction','source_span','legacy_unanchored','synthesis_result')"
)
_OLD_SHAPE = (
    "(kind = 'extraction' AND accepted_value_id IS NOT NULL"
    " AND document_id IS NOT NULL AND source_hash IS NOT NULL"
    " AND draft_citation_id IS NULL AND start_char IS NULL"
    " AND end_char IS NULL AND quote IS NULL AND synthesis_result_id IS NULL)"
    " OR (kind = 'source_span' AND document_id IS NOT NULL"
    " AND source_hash IS NOT NULL AND text_sha256 IS NOT NULL"
    " AND start_char IS NOT NULL AND end_char IS NOT NULL"
    " AND 0 <= start_char AND start_char < end_char AND quote IS NOT NULL"
    " AND accepted_value_id IS NULL AND draft_citation_id IS NULL"
    " AND synthesis_result_id IS NULL)"
    " OR (kind = 'legacy_unanchored' AND draft_citation_id IS NOT NULL"
    " AND accepted_value_id IS NULL AND source_hash IS NULL"
    " AND text_sha256 IS NULL AND start_char IS NULL AND end_char IS NULL"
    " AND quote IS NULL AND synthesis_result_id IS NULL)"
    " OR (kind = 'synthesis_result' AND synthesis_result_id IS NOT NULL"
    " AND accepted_value_id IS NULL AND document_id IS NULL"
    " AND source_hash IS NULL AND text_sha256 IS NULL"
    " AND draft_citation_id IS NULL AND start_char IS NULL"
    " AND end_char IS NULL AND quote IS NULL)"
)

_NEW_KIND = (
    "kind IN ('extraction','source_span','legacy_unanchored','synthesis_result',"
    "'figure')"
)
_NEW_SHAPE = (
    "(kind = 'extraction' AND accepted_value_id IS NOT NULL"
    " AND document_id IS NOT NULL AND source_hash IS NOT NULL"
    " AND draft_citation_id IS NULL AND start_char IS NULL"
    " AND end_char IS NULL AND quote IS NULL AND synthesis_result_id IS NULL"
    " AND figure_id IS NULL)"
    " OR (kind = 'source_span' AND document_id IS NOT NULL"
    " AND source_hash IS NOT NULL AND text_sha256 IS NOT NULL"
    " AND start_char IS NOT NULL AND end_char IS NOT NULL"
    " AND 0 <= start_char AND start_char < end_char AND quote IS NOT NULL"
    " AND accepted_value_id IS NULL AND draft_citation_id IS NULL"
    " AND synthesis_result_id IS NULL AND figure_id IS NULL)"
    " OR (kind = 'legacy_unanchored' AND draft_citation_id IS NOT NULL"
    " AND accepted_value_id IS NULL AND source_hash IS NULL"
    " AND text_sha256 IS NULL AND start_char IS NULL AND end_char IS NULL"
    " AND quote IS NULL AND synthesis_result_id IS NULL AND figure_id IS NULL)"
    " OR (kind = 'synthesis_result' AND synthesis_result_id IS NOT NULL"
    " AND accepted_value_id IS NULL AND document_id IS NULL"
    " AND source_hash IS NULL AND text_sha256 IS NULL"
    " AND draft_citation_id IS NULL AND start_char IS NULL"
    " AND end_char IS NULL AND quote IS NULL AND figure_id IS NULL)"
    " OR (kind = 'figure' AND figure_id IS NOT NULL"
    " AND accepted_value_id IS NULL AND document_id IS NULL"
    " AND source_hash IS NULL AND text_sha256 IS NULL"
    " AND draft_citation_id IS NULL AND start_char IS NULL"
    " AND end_char IS NULL AND quote IS NULL AND synthesis_result_id IS NULL)"
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


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def _replace_link_checks(kind: str, shape: str) -> None:
    op.drop_constraint("ck_research_claim_links_kind", _LINKS, type_="check")
    op.drop_constraint("ck_research_claim_links_shape", _LINKS, type_="check")
    op.create_check_constraint("ck_research_claim_links_kind", _LINKS, kind)
    op.create_check_constraint("ck_research_claim_links_shape", _LINKS, shape)


def upgrade() -> None:
    op.create_table(
        "research_run_manifests",
        _uuid("id"),
        _uuid("run_id"),
        _uuid("collection_id"),
        sa.Column("schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("completeness", sa.String(16), nullable=False),
        sa.Column("missing", postgresql.JSONB(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        _fk("run_id", "research_runs"),
        _fk("collection_id", "collections"),
        sa.UniqueConstraint("run_id", name="uq_research_run_manifests_run"),
        sa.CheckConstraint(
            "schema_version = 2", name="ck_research_run_manifests_schema"
        ),
        sa.CheckConstraint(
            "completeness IN ('complete','incomplete')",
            name="ck_research_run_manifests_state",
        ),
    )
    op.create_table(
        "research_run_artifacts",
        _uuid("id"),
        _uuid("run_id"),
        _uuid("collection_id"),
        _uuid("organization_id"),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("source_ref", postgresql.JSONB(), nullable=True),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        _fk("run_id", "research_runs"),
        _fk("collection_id", "collections"),
        _fk("organization_id", "organizations"),
        sa.UniqueConstraint("run_id", "role", "name", name="uq_research_run_artifacts"),
        sa.CheckConstraint(
            "role IN ('input','code','environment','output')",
            name="ck_research_run_artifacts_role",
        ),
        sa.CheckConstraint("byte_size >= 0", name="ck_research_run_artifacts_size"),
    )
    op.create_table(
        "research_figures",
        _uuid("id"),
        _uuid("collection_id"),
        sa.Column("figure_key", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("caption", sa.Text(), nullable=False),
        _uuid("output_artifact_id"),
        _uuid("run_id"),
        _uuid("manifest_id"),
        _uuid("supersedes_figure_id", nullable=True),
        _uuid("created_by_id"),
        sa.Column("actor_role", sa.String(16), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("output_artifact_id", "research_run_artifacts"),
        _fk("run_id", "research_runs"),
        _fk("manifest_id", "research_run_manifests"),
        _fk("supersedes_figure_id", "research_figures"),
        _fk("created_by_id", "users"),
        sa.UniqueConstraint(
            "supersedes_figure_id", name="uq_research_figures_supersedes"
        ),
        sa.CheckConstraint(
            "kind IN ('figure','table')", name="ck_research_figures_kind"
        ),
        sa.CheckConstraint("actor_role = 'editor'", name="ck_research_figures_actor"),
    )
    op.create_index(
        "uq_research_figures_initial",
        "research_figures",
        ["collection_id", "figure_key"],
        unique=True,
        postgresql_where=sa.text("supersedes_figure_id IS NULL"),
    )
    for table in _TABLES:
        _deny_data_api(table)
        op.execute(f"""
            CREATE TRIGGER trg_{table}_insert_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION prevent_research_insert_only_mutation()
            """)
    op.add_column(_LINKS, _uuid("figure_id", nullable=True))
    op.create_foreign_key(
        "fk_research_claim_links_figure",
        _LINKS,
        "research_figures",
        ["figure_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    _replace_link_checks(_NEW_KIND, _NEW_SHAPE)


def downgrade() -> None:
    bind = op.get_bind()
    linked = bind.execute(
        sa.text(f"SELECT 1 FROM {_LINKS} WHERE kind = 'figure'")
    ).first()
    recorded = bind.execute(sa.text("SELECT 1 FROM research_run_manifests")).first()
    if linked is not None or recorded is not None:
        raise RuntimeError(
            "figure links or run manifests exist; refusing to drop their evidence"
        )
    _replace_link_checks(_OLD_KIND, _OLD_SHAPE)
    op.drop_constraint("fk_research_claim_links_figure", _LINKS, type_="foreignkey")
    op.drop_column(_LINKS, "figure_id")
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER trg_{table}_insert_only ON {table}")
        op.drop_table(table)
