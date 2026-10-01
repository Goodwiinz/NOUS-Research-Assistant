"""Versioned manuscript claims and their evidence (GOO-306).

Five insert-only tables: nothing is updated or deleted. A claim is a bare
project-scoped identity; each version pins a draft passage by offsets and
content hash. Links, stance observations and assessments chain through
``supersedes_*`` (the tip is the row nobody supersedes). Composite foreign
keys on ``(id, collection_id)`` make a cross-project row impossible in the
database; the migration ``c4e6a8b0d2f5`` freezes the same DDL.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB

from .base import GUID, Base

KIND_CHECK = "kind IN ('factual','interpretation')"
ATTRIBUTION_CHECK = "(kind = 'interpretation') = (attributed_to_user_id IS NOT NULL)"
FIRST_VERSION_CHECK = "(supersedes_claim_version_id IS NULL) = (version_no = 1)"
SPAN_CHECK = "0 <= start_char AND start_char < end_char"
INITIAL_VERSION = "supersedes_claim_version_id IS NULL"
LINK_KIND_CHECK = (
    "kind IN ('extraction','source_span','legacy_unanchored','synthesis_result',"
    "'figure')"
)
LINK_STATUS_CHECK = "status IN ('linked','withdrawn')"
LINK_WITHDRAWAL_CHECK = "status <> 'withdrawn' OR supersedes_link_id IS NOT NULL"
LINK_SHAPE_CHECK = (
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
    # GOO-311: a pooled estimate cites a synthesis result, never a source.
    " OR (kind = 'synthesis_result' AND synthesis_result_id IS NOT NULL"
    " AND accepted_value_id IS NULL AND document_id IS NULL"
    " AND source_hash IS NULL AND text_sha256 IS NULL"
    " AND draft_citation_id IS NULL AND start_char IS NULL"
    " AND end_char IS NULL AND quote IS NULL AND figure_id IS NULL)"
    # GOO-312: a manuscript figure cites one exact run output.
    " OR (kind = 'figure' AND figure_id IS NOT NULL"
    " AND accepted_value_id IS NULL AND document_id IS NULL"
    " AND source_hash IS NULL AND text_sha256 IS NULL"
    " AND draft_citation_id IS NULL AND start_char IS NULL"
    " AND end_char IS NULL AND quote IS NULL AND synthesis_result_id IS NULL)"
)
STANCE_CHECK = "stance IN ('supporting','opposing','neutral','not_addressed')"
ASSESSMENT_STANCE_CHECK = (
    "stance IN ('supporting','opposing','neutral','not_addressed','unresolved')"
)
CONFIDENCE_CHECK = "classifier_confidence >= 0.0 AND classifier_confidence <= 1.0"
ADJUDICATOR_CHECK = "actor_role = 'adjudicator'"
RATIONALE_CHECK = "char_length(rationale) BETWEEN 1 AND 2000"
ID_SETS_CHECK = (
    "jsonb_typeof(link_ids) = 'array' AND jsonb_array_length(link_ids) <= 20"
    " AND jsonb_typeof(stance_observation_ids) = 'array'"
    " AND jsonb_array_length(stance_observation_ids) <= 20"
)
INITIAL_ASSESSMENT = "supersedes_assessment_id IS NULL"


def _fk(target: str, *, nullable: bool = False, ondelete: str = "RESTRICT") -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete=ondelete), nullable=nullable
    )


def _created_at() -> Column:
    return Column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )


def _composite(columns: list[str], table: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        columns,
        [f"{table}.id", f"{table}.collection_id"],
        ondelete="RESTRICT",
        name=name,
    )


class ResearchClaim(Base):
    """A stable claim identity; everything that can change lives in versions."""

    __tablename__ = "research_claims"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    created_by_id = _fk("users")
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint("id", "collection_id", name="uq_research_claims_scope"),
        Index("idx_research_claims_collection", "collection_id"),
    )


class ResearchClaimVersion(Base):
    """Immutable wording of a claim: ``text == draft.content[start:end]``."""

    __tablename__ = "research_claim_versions"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id: Column = Column(GUID(), nullable=False)
    claim_id: Column = Column(GUID(), nullable=False)
    version_no = Column(Integer, nullable=False)
    kind = Column(String(16), nullable=False, default="factual")
    attributed_to_user_id = _fk("users", nullable=True)
    text = Column(Text, nullable=False)
    text_sha256 = Column(String(64), nullable=False)
    normalized_hash = Column(String(64), nullable=False)
    draft_id = _fk("generated_drafts")
    draft_version = Column(Integer, nullable=False)
    draft_content_hash = Column(String(64), nullable=False)
    start_char = Column(Integer, nullable=False)
    end_char = Column(Integer, nullable=False)
    draft_review_id = _fk("draft_reviews", nullable=True)
    created_by_id = _fk("users")
    created_at = _created_at()
    supersedes_claim_version_id: Column = _fk("research_claim_versions", nullable=True)

    __table_args__ = (
        _composite(
            ["claim_id", "collection_id"],
            "research_claims",
            "fk_research_claim_versions_claim",
        ),
        UniqueConstraint(
            "id", "collection_id", name="uq_research_claim_versions_scope"
        ),
        UniqueConstraint(
            "claim_id", "version_no", name="uq_research_claim_versions_no"
        ),
        UniqueConstraint(
            "supersedes_claim_version_id",
            name="uq_research_claim_versions_supersedes",
        ),
        Index(
            "uq_research_claim_versions_initial",
            "claim_id",
            unique=True,
            postgresql_where=sql_text(INITIAL_VERSION),
            sqlite_where=sql_text(INITIAL_VERSION),
        ),
        CheckConstraint(FIRST_VERSION_CHECK, name="ck_research_claim_versions_first"),
        CheckConstraint(SPAN_CHECK, name="ck_research_claim_versions_span"),
        CheckConstraint(KIND_CHECK, name="ck_research_claim_versions_kind"),
        CheckConstraint(
            ATTRIBUTION_CHECK, name="ck_research_claim_versions_attribution"
        ),
        Index("idx_research_claim_versions_draft", "draft_id"),
    )


class ResearchClaimEvidenceLink(Base):
    """Append-only link from a claim version to evidence. Removing appends a
    ``withdrawn`` row; re-pointing appends a row superseding the old one."""

    __tablename__ = "research_claim_evidence_links"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id: Column = Column(GUID(), nullable=False)
    claim_version_id: Column = Column(GUID(), nullable=False)
    kind = Column(String(24), nullable=False)
    accepted_value_id = _fk("extraction_accepted_values", nullable=True)
    draft_citation_id = _fk("draft_citations", nullable=True)
    document_id = _fk("documents", nullable=True)
    source_hash = Column(String(64), nullable=True)
    text_sha256 = Column(String(64), nullable=True)
    start_char = Column(Integer, nullable=True)
    end_char = Column(Integer, nullable=True)
    quote = Column(Text, nullable=True)
    synthesis_result_id = _fk("synthesis_results", nullable=True)
    figure_id = _fk("research_figures", nullable=True)
    status = Column(String(16), nullable=False, default="linked")
    supersedes_link_id: Column = _fk("research_claim_evidence_links", nullable=True)
    created_by_id = _fk("users")
    created_at = _created_at()

    __table_args__ = (
        _composite(
            ["claim_version_id", "collection_id"],
            "research_claim_versions",
            "fk_research_claim_links_version",
        ),
        UniqueConstraint("id", "collection_id", name="uq_research_claim_links_scope"),
        UniqueConstraint(
            "supersedes_link_id", name="uq_research_claim_links_supersedes"
        ),
        CheckConstraint(LINK_KIND_CHECK, name="ck_research_claim_links_kind"),
        CheckConstraint(LINK_STATUS_CHECK, name="ck_research_claim_links_status"),
        CheckConstraint(
            LINK_WITHDRAWAL_CHECK, name="ck_research_claim_links_withdrawal"
        ),
        CheckConstraint(LINK_SHAPE_CHECK, name="ck_research_claim_links_shape"),
        Index("idx_research_claim_links_version", "claim_version_id"),
    )


class ResearchClaimStanceObservation(Base):
    """An immutable snapshot of the meter's stance for one link's source."""

    __tablename__ = "research_claim_stance_observations"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id: Column = Column(GUID(), nullable=False)
    link_id: Column = Column(GUID(), nullable=False)
    stance = Column(String(16), nullable=False)
    classifier_confidence = Column(Float, nullable=False)
    justification_excerpt = Column(Text, nullable=True)
    classifier_version = Column(String(50), nullable=False)
    inference_model_version = Column(String(100), nullable=True)
    source_content_hash = Column(String(64), nullable=False)
    # Provenance only: the meter row is a mutable cache; this row keeps values.
    stance_classification_id = _fk(
        "stance_classifications", nullable=True, ondelete="SET NULL"
    )
    classified_at = Column(DateTime(timezone=True), nullable=True)
    observed_by_id = _fk("users")
    created_at = _created_at()

    __table_args__ = (
        _composite(
            ["link_id", "collection_id"],
            "research_claim_evidence_links",
            "fk_research_claim_observations_link",
        ),
        CheckConstraint(STANCE_CHECK, name="ck_research_claim_observations_stance"),
        CheckConstraint(
            CONFIDENCE_CHECK, name="ck_research_claim_observations_confidence"
        ),
        Index("idx_research_claim_observations_link", "link_id"),
    )


class ResearchClaimAssessment(Base):
    """A human adjudicator's judgement of one claim version; one chain per
    version, whose tip is the current assessment."""

    __tablename__ = "research_claim_assessments"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id: Column = Column(GUID(), nullable=False)
    claim_version_id: Column = Column(GUID(), nullable=False)
    stance = Column(String(16), nullable=False)
    link_ids = Column(JSONB, nullable=False)
    stance_observation_ids = Column(JSONB, nullable=False)
    rationale = Column(Text, nullable=False)
    assessed_by_id = _fk("users")
    actor_role = Column(String(16), nullable=False, default="adjudicator")
    created_at = _created_at()
    supersedes_assessment_id: Column = _fk("research_claim_assessments", nullable=True)

    __table_args__ = (
        _composite(
            ["claim_version_id", "collection_id"],
            "research_claim_versions",
            "fk_research_claim_assessments_version",
        ),
        UniqueConstraint(
            "supersedes_assessment_id",
            name="uq_research_claim_assessments_supersedes",
        ),
        Index(
            "uq_research_claim_assessments_initial",
            "claim_version_id",
            unique=True,
            postgresql_where=sql_text(INITIAL_ASSESSMENT),
            sqlite_where=sql_text(INITIAL_ASSESSMENT),
        ),
        CheckConstraint(
            ASSESSMENT_STANCE_CHECK, name="ck_research_claim_assessments_stance"
        ),
        CheckConstraint(ADJUDICATOR_CHECK, name="ck_research_claim_assessments_actor"),
        CheckConstraint(
            RATIONALE_CHECK, name="ck_research_claim_assessments_rationale"
        ).ddl_if(dialect="postgresql"),
        # PostgreSQL-only: SQLite (OpenAPI generation, unit tests) lacks jsonb_*.
        CheckConstraint(
            ID_SETS_CHECK, name="ck_research_claim_assessments_id_sets"
        ).ddl_if(dialect="postgresql"),
    )
