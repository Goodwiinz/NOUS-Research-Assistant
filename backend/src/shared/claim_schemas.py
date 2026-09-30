"""Versioned claim, evidence link, stance snapshot and assessment contracts
(GOO-306). Every POST carries an ``idempotency_key``; offsets are Python
code points into ``GeneratedDraft.content`` / ``Document.content_text``."""

from datetime import datetime
from typing import Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ClaimKind = Literal["factual", "interpretation"]
ClaimLinkKind = Literal["extraction", "source_span", "legacy_unanchored"]
ClaimLinkStatus = Literal["linked", "withdrawn"]
ClaimStance = Literal["supporting", "opposing", "neutral", "not_addressed"]
ClaimAssessmentStance = Literal[
    "supporting", "opposing", "neutral", "not_addressed", "unresolved"
]


class ClaimCreate(BaseModel):
    """A new claim over ``draft.content[start_char:end_char]`` (exactly)."""

    draft_id: UUID
    start_char: int = Field(..., ge=0)
    end_char: int = Field(..., ge=1)
    text: str = Field(..., min_length=1, max_length=4000)
    kind: ClaimKind = Field(default="factual")
    idempotency_key: str = Field(..., min_length=1, max_length=255)


class ClaimVersionCreate(ClaimCreate):
    """A reworded passage; it must supersede the claim's current version."""

    supersedes_claim_version_id: UUID


class ClaimLinkCreate(BaseModel):
    """Link the claim's tip version to evidence, or withdraw/re-point a link.

    ``extraction`` takes ``accepted_value_id``; ``source_span`` takes
    ``document_id``, ``start_char``, ``end_char`` and ``quote``;
    ``legacy_unanchored`` takes ``draft_citation_id``. The server copies the
    source hashes. A ``withdrawn`` row supersedes a link and copies its target.
    """

    claim_version_id: UUID
    kind: ClaimLinkKind
    accepted_value_id: Optional[UUID] = Field(default=None)
    document_id: Optional[UUID] = Field(default=None)
    start_char: Optional[int] = Field(default=None, ge=0)
    end_char: Optional[int] = Field(default=None, ge=1)
    quote: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    draft_citation_id: Optional[UUID] = Field(default=None)
    status: ClaimLinkStatus = Field(default="linked")
    supersedes_link_id: Optional[UUID] = Field(default=None)
    idempotency_key: str = Field(..., min_length=1, max_length=255)


class StanceObservationCreate(BaseModel):
    """Snapshot the evidence meter's current stance for this link's source."""

    idempotency_key: str = Field(..., min_length=1, max_length=255)


class ClaimAssessmentCreate(BaseModel):
    """An adjudicator's judgement of the claim's tip version."""

    claim_version_id: UUID
    stance: ClaimAssessmentStance
    link_ids: List[UUID] = Field(default_factory=list, max_length=20)
    stance_observation_ids: List[UUID] = Field(default_factory=list, max_length=20)
    rationale: str = Field(..., min_length=1, max_length=2000)
    supersedes_assessment_id: Optional[UUID] = Field(default=None)
    idempotency_key: str = Field(..., min_length=1, max_length=255)


class ClaimVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    claim_id: UUID
    version_no: int
    kind: ClaimKind
    attributed_to_user_id: Optional[UUID] = Field(default=None)
    text: str
    text_sha256: str
    normalized_hash: str
    draft_id: UUID
    draft_version: int
    draft_content_hash: str
    start_char: int
    end_char: int
    draft_review_id: Optional[UUID] = Field(default=None)
    created_by_id: UUID
    created_at: datetime
    supersedes_claim_version_id: Optional[UUID] = Field(default=None)


class StanceObservationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    link_id: UUID
    stance: ClaimStance
    classifier_confidence: float
    justification_excerpt: Optional[str] = Field(default=None)
    classifier_version: str
    inference_model_version: Optional[str] = Field(default=None)
    source_content_hash: str
    stance_classification_id: Optional[UUID] = Field(default=None)
    classified_at: Optional[datetime] = Field(default=None)
    observed_by_id: UUID
    created_at: datetime


class ClaimLinkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    claim_version_id: UUID
    kind: ClaimLinkKind
    accepted_value_id: Optional[UUID] = Field(default=None)
    draft_citation_id: Optional[UUID] = Field(default=None)
    document_id: Optional[UUID] = Field(default=None)
    source_hash: Optional[str] = Field(default=None)
    text_sha256: Optional[str] = Field(default=None)
    start_char: Optional[int] = Field(default=None)
    end_char: Optional[int] = Field(default=None)
    quote: Optional[str] = Field(default=None)
    status: ClaimLinkStatus
    supersedes_link_id: Optional[UUID] = Field(default=None)
    created_by_id: UUID
    created_at: datetime
    # Derived on read (GOO-305's rule); never stored.
    source_changed: bool = Field(default=False)
    latest_observation: Optional[StanceObservationResponse] = Field(default=None)


class ClaimAssessmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    claim_version_id: UUID
    stance: ClaimAssessmentStance
    link_ids: List[UUID]
    stance_observation_ids: List[UUID]
    rationale: str
    assessed_by_id: UUID
    actor_role: str
    created_at: datetime
    supersedes_assessment_id: Optional[UUID] = Field(default=None)


class ClaimResponse(BaseModel):
    id: UUID
    collection_id: UUID
    created_by_id: UUID
    created_at: datetime
    version: ClaimVersionResponse


class ClaimSummary(BaseModel):
    """One claim: its tip version, or the version pinned to the filtered draft."""

    claim_id: UUID
    version: ClaimVersionResponse
    is_tip: bool
    links: List[ClaimLinkResponse]
    assessment: Optional[ClaimAssessmentResponse] = Field(default=None)
    citation_review_status: Optional[str] = Field(default=None)


class ClaimCounts(BaseModel):
    claims: int
    links_by_kind: Dict[str, int]
    legacy_unanchored: int
    assessed: int
    unassessed: int


class ClaimListResponse(BaseModel):
    items: List[ClaimSummary]
    counts: ClaimCounts


class ClaimDetailResponse(BaseModel):
    """The full history, each list in ``created_at, id`` order."""

    id: UUID
    collection_id: UUID
    created_by_id: UUID
    created_at: datetime
    versions: List[ClaimVersionResponse]
    links: List[ClaimLinkResponse]
    observations: List[StanceObservationResponse]
    assessments: List[ClaimAssessmentResponse]
