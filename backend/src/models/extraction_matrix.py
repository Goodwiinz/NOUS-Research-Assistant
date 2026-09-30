"""ExtractionMatrix and ExtractionCell models for Literature Review Matrix.

GOO-304 adds immutable form versions under a matrix (the matrix row stays the
form identity), insert-only observations and an insert-only accepted-value
chain per ``(document, field)``. ``extraction_cells`` is frozen: nothing
writes it after migration ``a3c5e7f9b1d4``; it is the last read fallback.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from .base import GUID, Base, BaseModel

# Mirrors extraction_rules.MISSINGNESS_VALUES; the migration freezes the same list.
_MISSINGNESS_CHECK = (
    "missingness IS NULL OR missingness IN ('not_reported','not_applicable',"
    "'unavailable_text','extraction_error','unresolved_disagreement')"
)
_VALUE_XOR_MISSINGNESS = "(value IS NULL) <> (missingness IS NULL)"
_INITIAL_ACCEPTED = "supersedes_accepted_value_id IS NULL"


def _fk(target: str, *, nullable: bool = False) -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=nullable
    )


def _created_at() -> Column:
    return Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ExtractionMatrix(BaseModel):
    """A structured extraction matrix for comparing documents in a project."""

    __tablename__ = "extraction_matrices"

    project_id = Column(
        GUID(),
        ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(255), nullable=False)
    columns = Column(JSONB, nullable=False, server_default="[]")

    project = relationship("Collection", backref="extraction_matrices")
    cells = relationship(
        "ExtractionCell", back_populates="matrix", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<ExtractionMatrix(id={self.id}, name={self.name})>"


class ExtractionCell(BaseModel):
    """A single extracted value in the matrix grid."""

    __tablename__ = "extraction_cells"
    __table_args__ = (
        UniqueConstraint(
            "matrix_id",
            "document_id",
            "column_name",
            name="uq_cell_matrix_doc_col",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="ck_cell_confidence_range",
        ),
    )

    matrix_id = Column(
        GUID(),
        ForeignKey("extraction_matrices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id = Column(
        GUID(),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    column_name = Column(String(100), nullable=False)
    value = Column(Text, nullable=True)
    citation_snippet = Column(Text, nullable=True)
    confidence = Column(Float, nullable=True)

    matrix = relationship("ExtractionMatrix", back_populates="cells")
    document = relationship("Document")

    def __repr__(self):
        return f"<ExtractionCell(matrix={self.matrix_id}, col={self.column_name})>"


class ExtractionFormVersion(Base):
    """Immutable ordered field list for one matrix; current = max(version_no)."""

    __tablename__ = "extraction_form_versions"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    matrix_id = _fk("extraction_matrices")
    version_no = Column(Integer, nullable=False)
    provenance = Column(String(24), nullable=False)
    # [{field_id, name, description, type, unit, timepoint, categories}]
    fields = Column(JSONB, nullable=False)
    protocol_version_id = _fk("research_protocol_versions", nullable=True)
    content_hash = Column(String(64), nullable=False)
    created_by_id = _fk("users", nullable=True)
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "matrix_id", "version_no", name="uq_extraction_form_version_no"
        ),
        CheckConstraint(
            "provenance IN ('legacy_unversioned','authored')",
            name="ck_extraction_form_version_provenance",
        ),
        # Legacy rows are attributed to nobody; authored rows to someone.
        CheckConstraint(
            "(provenance = 'legacy_unversioned') = (created_by_id IS NULL)",
            name="ck_extraction_form_version_author",
        ),
        CheckConstraint(
            "provenance <> 'legacy_unversioned' OR version_no = 1",
            name="ck_extraction_form_version_legacy_first",
        ),
        CheckConstraint("version_no >= 1", name="ck_extraction_form_version_no"),
    )


class ExtractionObservation(Base):
    """Insert-only machine or human value for one field of one document.

    ``actor_user_id`` is always a person: the author, or for a machine row the
    user who started the run.
    """

    __tablename__ = "extraction_observations"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    form_version_id = _fk("extraction_form_versions")
    field_id = Column(GUID(), nullable=False)
    document_id = _fk("documents")
    kind = Column(String(16), nullable=False)
    actor_user_id = _fk("users")
    extractor_run_id = Column(String(64), nullable=True)
    extractor_model = Column(String(100), nullable=True)
    value = Column(JSONB(none_as_null=True), nullable=True)
    missingness = Column(String(32), nullable=True)
    validation_state = Column(String(16), nullable=False)
    citation = Column(Text, nullable=True)
    source_hash = Column(String(64), nullable=False)
    created_at = _created_at()

    __table_args__ = (
        CheckConstraint(
            "kind IN ('machine','human')", name="ck_extraction_observation_kind"
        ),
        CheckConstraint(
            "(kind = 'machine') = (extractor_run_id IS NOT NULL)"
            " AND (kind = 'machine') = (extractor_model IS NOT NULL)",
            name="ck_extraction_observation_extractor",
        ),
        CheckConstraint(_MISSINGNESS_CHECK, name="ck_extraction_observation_missing"),
        CheckConstraint(_VALUE_XOR_MISSINGNESS, name="ck_extraction_observation_xor"),
        CheckConstraint(
            "validation_state IN ('valid','invalid')",
            name="ck_extraction_observation_validation",
        ),
        Index(
            "idx_extraction_observation_cell", "document_id", "field_id", "created_at"
        ),
    )


class ExtractionAcceptedValue(Base):
    """Insert-only accepted-value chain per ``(document, field)``; the tip is
    the row nobody supersedes. Only an ADJUDICATOR writes one."""

    __tablename__ = "extraction_accepted_values"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    form_version_id = _fk("extraction_form_versions")
    field_id = Column(GUID(), nullable=False)
    document_id = _fk("documents")
    value = Column(JSONB(none_as_null=True), nullable=True)
    missingness = Column(String(32), nullable=True)
    # Read only as a set (like screening_resolutions.input_observation_ids).
    observation_ids = Column(JSONB, nullable=False)
    accepted_by_id = _fk("users")
    rationale = Column(Text, nullable=False)
    source_hash = Column(String(64), nullable=False)
    supersedes_accepted_value_id: Column = _fk(
        "extraction_accepted_values", nullable=True
    )
    created_at = _created_at()

    __table_args__ = (
        CheckConstraint(_MISSINGNESS_CHECK, name="ck_extraction_accepted_missing"),
        CheckConstraint(
            "missingness IS NULL OR missingness <> 'extraction_error'",
            name="ck_extraction_accepted_not_error",
        ),
        CheckConstraint(_VALUE_XOR_MISSINGNESS, name="ck_extraction_accepted_xor"),
        # PostgreSQL-only: SQLite (OpenAPI generation, unit tests) lacks jsonb_*.
        CheckConstraint(
            "jsonb_typeof(observation_ids) = 'array'"
            " AND jsonb_array_length(observation_ids) BETWEEN 1 AND 20",
            name="ck_extraction_accepted_citations",
        ).ddl_if(dialect="postgresql"),
        UniqueConstraint(
            "supersedes_accepted_value_id", name="uq_extraction_accepted_supersedes"
        ),
        Index(
            "uq_extraction_accepted_initial",
            "document_id",
            "field_id",
            unique=True,
            postgresql_where=text(_INITIAL_ACCEPTED),
            sqlite_where=text(_INITIAL_ACCEPTED),
        ),
    )
