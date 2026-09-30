"""Create versioned research claims and their evidence tables (GOO-306).

Five insert-only tables, no backfill: existing draft citations are linked
explicitly (``legacy_unanchored``) by a person, never guessed. Composite
foreign keys on ``(id, collection_id)`` keep every row in one project. All
checks are frozen copies of ``src.models.research_claim`` (never import src
here). Downgrade drops the tables in reverse FK order; claims written after
the upgrade are lost.

Revision ID: c4e6a8b0d2f5
Revises: b8d0f2a4c6e9
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c4e6a8b0d2f5"
down_revision = "b8d0f2a4c6e9"
branch_labels = None
depends_on = None

_POSTGREST_ROLES = ("anon", "authenticated")
_UUID = postgresql.UUID(as_uuid=True)
_CLAIMS = "research_claims"
_VERSIONS = "research_claim_versions"
_LINKS = "research_claim_evidence_links"
_OBSERVATIONS = "research_claim_stance_observations"
_ASSESSMENTS = "research_claim_assessments"
_TABLES = (_CLAIMS, _VERSIONS, _LINKS, _OBSERVATIONS, _ASSESSMENTS)

_LINK_SHAPE = (
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


def _fk(
    column: str, target: str, ondelete: str = "RESTRICT"
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete)


def _composite(columns: list[str], table: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        columns,
        [f"{table}.id", f"{table}.collection_id"],
        ondelete="RESTRICT",
        name=name,
    )


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def _hash(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.String(length=64), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        _CLAIMS,
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("created_by_id"),
        _created_at(),
        _fk("collection_id", "collections"),
        _fk("created_by_id", "users"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "collection_id", name="uq_research_claims_scope"),
    )
    op.create_index("idx_research_claims_collection", _CLAIMS, ["collection_id"])
    op.create_table(
        _VERSIONS,
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("claim_id"),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        _uuid("attributed_to_user_id", nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        _hash("text_sha256"),
        _hash("normalized_hash"),
        _uuid("draft_id"),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        _hash("draft_content_hash"),
        sa.Column("start_char", sa.Integer(), nullable=False),
        sa.Column("end_char", sa.Integer(), nullable=False),
        _uuid("draft_review_id", nullable=True),
        _uuid("created_by_id"),
        _created_at(),
        _uuid("supersedes_claim_version_id", nullable=True),
        _composite(
            ["claim_id", "collection_id"], _CLAIMS, "fk_research_claim_versions_claim"
        ),
        _fk("attributed_to_user_id", "users"),
        _fk("draft_id", "generated_drafts"),
        _fk("draft_review_id", "draft_reviews"),
        _fk("created_by_id", "users"),
        _fk("supersedes_claim_version_id", _VERSIONS),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "collection_id", name="uq_research_claim_versions_scope"
        ),
        sa.UniqueConstraint(
            "claim_id", "version_no", name="uq_research_claim_versions_no"
        ),
        sa.UniqueConstraint(
            "supersedes_claim_version_id",
            name="uq_research_claim_versions_supersedes",
        ),
        sa.CheckConstraint(
            "(supersedes_claim_version_id IS NULL) = (version_no = 1)",
            name="ck_research_claim_versions_first",
        ),
        sa.CheckConstraint(
            "0 <= start_char AND start_char < end_char",
            name="ck_research_claim_versions_span",
        ),
        sa.CheckConstraint(
            "kind IN ('factual','interpretation')",
            name="ck_research_claim_versions_kind",
        ),
        sa.CheckConstraint(
            "(kind = 'interpretation') = (attributed_to_user_id IS NOT NULL)",
            name="ck_research_claim_versions_attribution",
        ),
    )
    op.create_index(
        "uq_research_claim_versions_initial",
        _VERSIONS,
        ["claim_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_claim_version_id IS NULL"),
    )
    op.create_index("idx_research_claim_versions_draft", _VERSIONS, ["draft_id"])
    op.create_table(
        _LINKS,
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("claim_version_id"),
        sa.Column("kind", sa.String(length=24), nullable=False),
        _uuid("accepted_value_id", nullable=True),
        _uuid("draft_citation_id", nullable=True),
        _uuid("document_id", nullable=True),
        _hash("source_hash", nullable=True),
        _hash("text_sha256", nullable=True),
        sa.Column("start_char", sa.Integer(), nullable=True),
        sa.Column("end_char", sa.Integer(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        _uuid("supersedes_link_id", nullable=True),
        _uuid("created_by_id"),
        _created_at(),
        _composite(
            ["claim_version_id", "collection_id"],
            _VERSIONS,
            "fk_research_claim_links_version",
        ),
        _fk("accepted_value_id", "extraction_accepted_values"),
        _fk("draft_citation_id", "draft_citations"),
        _fk("document_id", "documents"),
        _fk("supersedes_link_id", _LINKS),
        _fk("created_by_id", "users"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "collection_id", name="uq_research_claim_links_scope"
        ),
        sa.UniqueConstraint(
            "supersedes_link_id", name="uq_research_claim_links_supersedes"
        ),
        sa.CheckConstraint(
            "kind IN ('extraction','source_span','legacy_unanchored')",
            name="ck_research_claim_links_kind",
        ),
        sa.CheckConstraint(
            "status IN ('linked','withdrawn')", name="ck_research_claim_links_status"
        ),
        sa.CheckConstraint(
            "status <> 'withdrawn' OR supersedes_link_id IS NOT NULL",
            name="ck_research_claim_links_withdrawal",
        ),
        sa.CheckConstraint(_LINK_SHAPE, name="ck_research_claim_links_shape"),
    )
    op.create_index("idx_research_claim_links_version", _LINKS, ["claim_version_id"])
    op.create_table(
        _OBSERVATIONS,
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("link_id"),
        sa.Column("stance", sa.String(length=16), nullable=False),
        sa.Column("classifier_confidence", sa.Float(), nullable=False),
        sa.Column("justification_excerpt", sa.Text(), nullable=True),
        sa.Column("classifier_version", sa.String(length=50), nullable=False),
        sa.Column("inference_model_version", sa.String(length=100), nullable=True),
        _hash("source_content_hash"),
        _uuid("stance_classification_id", nullable=True),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True),
        _uuid("observed_by_id"),
        _created_at(),
        _composite(
            ["link_id", "collection_id"], _LINKS, "fk_research_claim_observations_link"
        ),
        _fk("stance_classification_id", "stance_classifications", "SET NULL"),
        _fk("observed_by_id", "users"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "stance IN ('supporting','opposing','neutral','not_addressed')",
            name="ck_research_claim_observations_stance",
        ),
        sa.CheckConstraint(
            "classifier_confidence >= 0.0 AND classifier_confidence <= 1.0",
            name="ck_research_claim_observations_confidence",
        ),
    )
    op.create_index("idx_research_claim_observations_link", _OBSERVATIONS, ["link_id"])
    op.create_table(
        _ASSESSMENTS,
        _uuid("id"),
        _uuid("collection_id"),
        _uuid("claim_version_id"),
        sa.Column("stance", sa.String(length=16), nullable=False),
        sa.Column("link_ids", postgresql.JSONB(), nullable=False),
        sa.Column("stance_observation_ids", postgresql.JSONB(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        _uuid("assessed_by_id"),
        sa.Column("actor_role", sa.String(length=16), nullable=False),
        _created_at(),
        _uuid("supersedes_assessment_id", nullable=True),
        _composite(
            ["claim_version_id", "collection_id"],
            _VERSIONS,
            "fk_research_claim_assessments_version",
        ),
        _fk("assessed_by_id", "users"),
        _fk("supersedes_assessment_id", _ASSESSMENTS),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "supersedes_assessment_id",
            name="uq_research_claim_assessments_supersedes",
        ),
        sa.CheckConstraint(
            "stance IN ('supporting','opposing','neutral','not_addressed',"
            "'unresolved')",
            name="ck_research_claim_assessments_stance",
        ),
        sa.CheckConstraint(
            "actor_role = 'adjudicator'", name="ck_research_claim_assessments_actor"
        ),
        sa.CheckConstraint(
            "char_length(rationale) BETWEEN 1 AND 2000",
            name="ck_research_claim_assessments_rationale",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(link_ids) = 'array' AND jsonb_array_length(link_ids) <= 20"
            " AND jsonb_typeof(stance_observation_ids) = 'array'"
            " AND jsonb_array_length(stance_observation_ids) <= 20",
            name="ck_research_claim_assessments_id_sets",
        ),
    )
    op.create_index(
        "uq_research_claim_assessments_initial",
        _ASSESSMENTS,
        ["claim_version_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_assessment_id IS NULL"),
    )
    for table in _TABLES:
        _deny_data_api(table)


def downgrade() -> None:
    # Reverse FK order; the indexes go with the tables.
    for table in reversed(_TABLES):
        op.drop_table(table)
