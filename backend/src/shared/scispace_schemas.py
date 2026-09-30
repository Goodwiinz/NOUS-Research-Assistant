"""SciSpace integration schemas shared across features."""

import re
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SAFE_COLUMN_NAME_RE = re.compile(r"^[\w\s\-\(\)\/\.,:]+$")


# Feature 1: Extraction Matrix


class ExtractionColumn(BaseModel):
    """A column definition for the extraction matrix."""

    name: str = Field(
        ..., min_length=1, max_length=100, description="Column header name"
    )
    description: Optional[str] = Field(
        None, max_length=500, description="What to extract"
    )
    # GOO-304: typed fields. All optional, so the request change is additive.
    type: Literal["text", "number", "boolean", "categorical"] = Field(
        default="text", description="Value type of the extracted field"
    )
    unit: Optional[str] = Field(default=None, max_length=50)
    timepoint: Optional[str] = Field(default=None, max_length=100)
    categories: Optional[List[str]] = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="Allowed values; required for, and only for, categorical",
    )

    @field_validator("name")
    @classmethod
    def name_must_be_safe(cls, v: str) -> str:
        if not _SAFE_COLUMN_NAME_RE.match(v):
            raise ValueError("Column name contains disallowed characters")
        return v

    @model_validator(mode="after")
    def categories_iff_categorical(self) -> "ExtractionColumn":
        if (self.type == "categorical") != (self.categories is not None):
            raise ValueError("categories are required for, and only for, categorical")
        return self


class ExtractionCellResponse(BaseModel):
    """A single cell in the extraction matrix."""

    document_id: UUID
    column_name: str
    value: Optional[str] = None
    citation_snippet: Optional[str] = None
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)


class CreateMatrixRequest(BaseModel):
    """Request to create an extraction matrix."""

    name: str = Field(..., min_length=1, max_length=255)
    columns: List[ExtractionColumn] = Field(..., min_length=1, max_length=20)


class UpdateMatrixRequest(BaseModel):
    """Request to update an existing extraction matrix."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    columns: Optional[List[ExtractionColumn]] = Field(None, min_length=1, max_length=20)
    clear_stale_cells: bool = Field(
        False,
        description=(
            "Deprecated: nothing is deleted. When true and columns changed, the "
            "response lists documents holding values on removed columns."
        ),
    )


class TriggerExtractionRequest(BaseModel):
    """Request to trigger extraction on selected documents."""

    document_ids: List[UUID] = Field(..., min_length=1, max_length=100)


# GOO-304: versioned extraction forms, observations and accepted values.

HumanMissingness = Literal["not_reported", "not_applicable", "unavailable_text"]
AcceptedMissingness = Literal[
    "not_reported", "not_applicable", "unavailable_text", "unresolved_disagreement"
]


class ExtractionObservationCreate(BaseModel):
    """A reviewer's value (or missingness reason) for one field of one document."""

    document_id: UUID
    field_id: UUID
    form_version_id: UUID
    value: Any = Field(default=None)
    missingness: Optional[HumanMissingness] = Field(default=None)
    citation: Optional[str] = Field(default=None, max_length=2000)
    # GOO-305: pick one occurrence of a repeated verbatim citation.
    anchor_start: Optional[int] = Field(default=None, ge=0)
    idempotency_key: str = Field(..., min_length=1, max_length=255)


class ExtractionAcceptCreate(BaseModel):
    """An adjudicator's accepted value; it must equal a cited observation."""

    document_id: UUID
    field_id: UUID
    form_version_id: UUID
    observation_ids: List[UUID] = Field(..., min_length=1, max_length=20)
    value: Any = Field(default=None)
    missingness: Optional[AcceptedMissingness] = Field(default=None)
    rationale: str = Field(..., min_length=1, max_length=2000)
    supersedes_accepted_value_id: Optional[UUID] = Field(default=None)
    idempotency_key: str = Field(..., min_length=1, max_length=255)
    # GOO-305: resolve an ambiguous anchor, or accept an unverified one.
    anchor_start: Optional[int] = Field(default=None, ge=0)
    accept_unverified: bool = Field(default=False)


class ExtractionFormVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    matrix_id: UUID
    version_no: int
    provenance: str
    fields: List[Dict[str, Any]]
    protocol_version_id: Optional[UUID] = Field(default=None)
    content_hash: str
    created_by_id: Optional[UUID] = Field(default=None)
    created_at: datetime


AnchorStatus = Literal["verified", "ambiguous", "unverified", "location_unavailable"]
AnchorResolution = Literal[
    "verified", "disambiguated", "accepted_unverified", "not_applicable", "legacy"
]


class ExtractionAnchorOccurrence(BaseModel):
    """One place a repeated citation occurs, sliced server-side (code points)."""

    start_char: int
    end_char: int
    page: Optional[int] = Field(default=None)
    context_before: str
    context_after: str


class ExtractionAnchor(BaseModel):
    """Where an observation's verbatim citation sits in the retained text."""

    status: AnchorStatus
    start_char: Optional[int] = Field(default=None)
    end_char: Optional[int] = Field(default=None)
    page: Optional[int] = Field(default=None)
    occurrences: List[int] = Field(default_factory=list)
    occurrences_in_text: Optional[int] = Field(default=None)
    occurrence_contexts: List[ExtractionAnchorOccurrence] = Field(default_factory=list)


class ExtractionObservationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    form_version_id: UUID
    field_id: UUID
    document_id: UUID
    kind: str
    actor_user_id: UUID
    extractor_run_id: Optional[str] = Field(default=None)
    extractor_model: Optional[str] = Field(default=None)
    value: Any = Field(default=None)
    missingness: Optional[str] = Field(default=None)
    validation_state: str
    citation: Optional[str] = Field(default=None)
    source_hash: str
    created_at: datetime
    # GOO-305: null anchor on missingness rows; context only while the source
    # is unchanged; coverage only for machine rows.
    anchor: Optional[ExtractionAnchor] = Field(default=None)
    context_before: Optional[str] = Field(default=None)
    context_after: Optional[str] = Field(default=None)
    text_sha256: Optional[str] = Field(default=None)
    text_length: Optional[int] = Field(default=None)
    inspected_coverage: Optional[List[List[int]]] = Field(default=None)
    coverage_complete: Optional[bool] = Field(default=None)
    source_changed: bool = Field(default=False)


class ExtractionAcceptedValueResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    form_version_id: UUID
    field_id: UUID
    document_id: UUID
    value: Any = Field(default=None)
    missingness: Optional[str] = Field(default=None)
    observation_ids: List[UUID]
    accepted_by_id: UUID
    rationale: str
    source_hash: str
    supersedes_accepted_value_id: Optional[UUID] = Field(default=None)
    created_at: datetime
    # GOO-305 anchor snapshot; "legacy" for rows accepted before anchors.
    anchor_observation_id: Optional[UUID] = Field(default=None)
    anchor_resolution: Optional[AnchorResolution] = Field(default=None)
    anchor_start_char: Optional[int] = Field(default=None)
    anchor_end_char: Optional[int] = Field(default=None)
    text_sha256: Optional[str] = Field(default=None)
    source_changed: bool = Field(default=False)


class ExtractionCellObservationsResponse(BaseModel):
    observations: List[ExtractionObservationResponse]
    accepted_chain: List[ExtractionAcceptedValueResponse]


# Feature 3: Tone Engine

_ALLOWED_MODELS = {"gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "claude-sonnet-4-6"}


class ToneOption(str, Enum):
    """Available tone adjustment options."""

    ACADEMIC = "academic"
    SIMPLIFIED = "simplified"
    CONCISE = "concise"
    EXPANDED = "expanded"


class RewriteRequest(BaseModel):
    """Request to rewrite text with a specific tone."""

    text: str = Field(
        ...,
        min_length=20,
        max_length=50_000,
        description="Text to rewrite (min 20, max 50000 chars)",
    )
    tone: ToneOption
    preserve_citations: bool = Field(True, description="Maintain citation markers")
    model: Optional[str] = Field(None, description="LLM model override")

    @field_validator("text")
    @classmethod
    def text_not_too_short(cls, v: str) -> str:
        if len(v.split()) < 5:
            raise ValueError("Text must contain at least 5 words")
        return v

    @field_validator("model")
    @classmethod
    def model_must_be_allowed(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _ALLOWED_MODELS:
            raise ValueError(f"model must be one of {sorted(_ALLOWED_MODELS)}")
        return v


class RewriteResponse(BaseModel):
    """Response from the tone engine."""

    original: str
    rewritten: str
    tone_applied: ToneOption
    citations_preserved: List[str] = Field(default_factory=list)


# Feature 5: Integrity Detector


class IntegritySegmentScore(BaseModel):
    """AI detection score for a text segment."""

    text_preview: str = Field(..., max_length=200)
    ai_probability: float = Field(..., ge=0.0, le=1.0)


class IntegrityScoreResponse(BaseModel):
    """Full integrity score for a document."""

    document_id: str
    ai_probability: float = Field(..., ge=0.0, le=1.0)
    human_probability: float = Field(..., ge=0.0, le=1.0)
    method: str = "roberta-base-openai-detector"
    analyzed_at: Optional[datetime] = None
    segment_scores: List[IntegritySegmentScore] = Field(default_factory=list)


# Feature 6: AI Writer


class WriterAction(str, Enum):
    """Available AI writer actions."""

    COMPLETE = "complete"
    GENERATE_SECTION = "generate_section"
    GENERATE_OUTLINE = "generate_outline"


class SectionType(str, Enum):
    """Section types for generation."""

    INTRODUCTION = "introduction"
    METHODOLOGY = "methodology"
    RESULTS = "results"
    DISCUSSION = "discussion"
    CONCLUSION = "conclusion"
    ABSTRACT = "abstract"
    CUSTOM = "custom"


class StyleOption(str, Enum):
    """Writing style options."""

    ACADEMIC = "academic"
    TECHNICAL = "technical"
    SUMMARY = "summary"


class WriteRequest(BaseModel):
    """Request for AI text generation."""

    action: WriterAction
    cursor_context: str = Field(
        ..., min_length=1, max_length=50_000, description="Text around cursor position"
    )
    section_type: Optional[SectionType] = None
    style: StyleOption = StyleOption.ACADEMIC
    document_ids: Optional[List[UUID]] = Field(
        None, max_length=50, description="Source documents for context"
    )

    @field_validator("cursor_context")
    @classmethod
    def context_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("cursor_context must contain non-whitespace characters")
        return v


class WriteResponse(BaseModel):
    """Response from the AI writer."""

    generated: str
    action: WriterAction
    section_type: Optional[SectionType] = None
    citations_used: List[str] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0)


class OutlineSection(BaseModel):
    """A section in a generated outline."""

    title: str = Field(..., min_length=1, max_length=200)
    section_type: SectionType
    description: str = Field(..., max_length=1000)
    suggested_word_count: int = Field(..., ge=50, le=10_000)


class OutlineRequest(BaseModel):
    """Request for outline generation."""

    research_question: str = Field(
        ..., min_length=10, max_length=1000, description="Research question to outline"
    )
    style: StyleOption = StyleOption.ACADEMIC
    document_ids: Optional[List[UUID]] = Field(
        None, max_length=50, description="Source documents for context"
    )
    section_types: Optional[List[SectionType]] = Field(
        None, description="Desired section types to include"
    )

    @field_validator("research_question")
    @classmethod
    def question_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("research_question must contain non-whitespace characters")
        return v


class OutlineResponse(BaseModel):
    """Response from outline generation."""

    research_question: str
    sections: List[OutlineSection] = Field(default_factory=list)
    style: StyleOption
    total_suggested_words: int = Field(..., ge=0)
