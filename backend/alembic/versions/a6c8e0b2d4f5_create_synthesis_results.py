"""Create insert-only synthesis results and the synthesis claim-link kind (GOO-311).

``synthesis_results`` gets GOO-309's ``prevent_research_insert_only_mutation()``
trigger (SQLSTATE 55000 on UPDATE or DELETE). ``research_claim_evidence_links``
gains a nullable ``synthesis_result_id`` and its kind/shape CHECKs admit a
fourth kind; existing rows satisfy the new CHECKs because the column is NULL.
All DDL is a frozen copy of ``src.models.research_synthesis`` and
``src.models.research_claim`` (never import src here). No data statements.

Downgrade refuses while any ``synthesis_result`` link exists (evidence is
never dropped silently), then restores the old CHECKs and drops the column,
trigger and table. The trigger function stays (``e2a4c6b8d0f1`` owns it).

Revision ID: a6c8e0b2d4f5
Revises: f4b6d8a0c2e3
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision = "a6c8e0b2d4f5"
down_revision = "f4b6d8a0c2e3"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_LINKS = "research_claim_evidence_links"

_OLD_KIND = "kind IN ('extraction','source_span','legacy_unanchored')"
_OLD_SHAPE = (
    "(kind = 'extraction' AND accepted_value_id IS NOT NULL"
    " AND document_id IS NOT NULL AND source_hash IS NOT NULL"
    " AND draft_citation_id IS NULL AND start_char IS NULL"
    " AND end_char IS NULL AND quote IS NULL)"
    " OR (kind = 'source_span' AND document_id IS NOT NULL"
    " AND source_hash IS NOT NULL AND text_sha256 IS NOT NULL"
    " AND start_char IS NOT NULL AND end_char IS NOT NULL"
    " AND 0 <= start_char AND start_char < end_char AND quote IS NOT NULL"
    " AND accepted_value_id IS NULL AND draft_citation_id IS NULL)"
    " OR (kind = 'legacy_unanchored' AND draft_citation_id IS NOT NULL"
    " AND accepted_value_id IS NULL AND source_hash IS NULL"
    " AND text_sha256 IS NULL AND start_char IS NULL AND end_char IS NULL"
    " AND quote IS NULL)"
)
_NEW_KIND = (
    "kind IN ('extraction','source_span','legacy_unanchored','synthesis_result')"
)
_NEW_SHAPE = (
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


def _float(name: str) -> sa.Column:
    return sa.Column(name, sa.Float(), nullable=True)


def _replace_link_checks(kind: str, shape: str) -> None:
    op.drop_constraint("ck_research_claim_links_kind", _LINKS, type_="check")
    op.drop_constraint("ck_research_claim_links_shape", _LINKS, type_="check")
    op.create_check_constraint("ck_research_claim_links_kind", _LINKS, kind)
    op.create_check_constraint("ck_research_claim_links_shape", _LINKS, shape)


def upgrade() -> None:
    op.create_table(
        "synthesis_results",
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("protocol_version_id"),
        _uuid("table_version_id"),
        sa.Column("outcome_key", sa.String(100), nullable=False),
        sa.Column("timepoint", sa.String(100), nullable=False),
        sa.Column("measure", sa.String(32), nullable=False),
        sa.Column("model", sa.String(32), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("estimator_version", sa.String(64), nullable=False),
        sa.Column("software", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("included", postgresql.JSONB(), nullable=False),
        sa.Column("excluded", postgresql.JSONB(), nullable=False),
        _float("estimate"),
        _float("se"),
        _float("ci_low"),
        _float("ci_high"),
        _float("q"),
        sa.Column("df", sa.Integer(), nullable=True),
        _float("tau2"),
        _float("i2"),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False),
        _uuid("executed_by_id"),
        sa.Column("actor_role", sa.String(16), nullable=False),
        _uuid("supersedes_result_id", nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        _fk("collection_id", "collections"),
        _fk("protocol_version_id", "research_protocol_versions"),
        _fk("table_version_id", "evidence_table_versions"),
        _fk("executed_by_id", "users"),
        _fk("supersedes_result_id", "synthesis_results"),
        sa.UniqueConstraint("supersedes_result_id", name="uq_synthesis_supersedes"),
        sa.CheckConstraint(
            "status IN ('computed','validation_failed')", name="ck_synthesis_status"
        ),
        sa.CheckConstraint(
            "(status = 'computed') = (estimate IS NOT NULL)",
            name="ck_synthesis_numbers",
        ),
        sa.CheckConstraint(
            "i2 IS NULL OR (i2 >= 0 AND i2 <= 1)", name="ck_synthesis_i2"
        ),
        sa.CheckConstraint("actor_role = 'reviewer'", name="ck_synthesis_actor_role"),
    )
    op.create_index(
        "uq_synthesis_initial",
        "synthesis_results",
        ["collection_id", "outcome_key", "timepoint"],
        unique=True,
        postgresql_where=sa.text("supersedes_result_id IS NULL"),
    )
    _deny_data_api("synthesis_results")
    op.execute("""
        CREATE TRIGGER trg_synthesis_results_insert_only
        BEFORE UPDATE OR DELETE ON synthesis_results
        FOR EACH ROW EXECUTE FUNCTION prevent_research_insert_only_mutation()
        """)
    op.add_column(_LINKS, _uuid("synthesis_result_id", nullable=True))
    op.create_foreign_key(
        "fk_research_claim_links_synthesis_result",
        _LINKS,
        "synthesis_results",
        ["synthesis_result_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    _replace_link_checks(_NEW_KIND, _NEW_SHAPE)


def downgrade() -> None:
    linked = (
        op.get_bind()
        .execute(sa.text(f"SELECT 1 FROM {_LINKS} WHERE kind = 'synthesis_result'"))
        .first()
    )
    if linked is not None:
        raise RuntimeError(
            "synthesis_result claim links exist; refusing to drop their evidence"
        )
    _replace_link_checks(_OLD_KIND, _OLD_SHAPE)
    op.drop_constraint(
        "fk_research_claim_links_synthesis_result", _LINKS, type_="foreignkey"
    )
    op.drop_column(_LINKS, "synthesis_result_id")
    op.execute("DROP TRIGGER trg_synthesis_results_insert_only ON synthesis_results")
    op.drop_table("synthesis_results")
