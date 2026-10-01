"""External peer-review contracts (GOO-314). Every POST carries an
``idempotency_key``; anchor offsets are Python code points into the round's
reviewed ``GeneratedDraft.content`` (GOO-306's convention). Status and anchor
state are derived on read, never stored."""

from datetime import date, datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ResponseKind = Literal["change", "no_change"]
DecisionKind = Literal["assigned", "resolved", "reopened"]
CommentStatus = Literal["open", "responded", "resolved"]
AnchorStateName = Literal["exact", "carried", "unresolved_anchor", "general"]
DiffOp = Literal["replace", "insert", "delete"]
_KEY = Field(..., min_length=1, max_length=255)


class ReviewerCreate(BaseModel):
    """An external reviewer: a label such as "Reviewer 2", never a user."""

    label: str = Field(..., min_length=1, max_length=64)
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=255)


class RoundCreate(BaseModel):
    """A review round of one exact saved draft version (bound by hash)."""

    draft_id: UUID
    draft_content_hash: str = Field(..., min_length=64, max_length=64)
    label: str = Field(..., min_length=1, max_length=255)
    received_at: Optional[date] = Field(default=None)
    reviewers: List[ReviewerCreate] = Field(..., min_length=1, max_length=20)
    idempotency_key: str = _KEY


class AnchorCreate(BaseModel):
    """``quote == content[start:end]`` of the round's reviewed version."""

    start: int = Field(..., ge=0)
    end: int = Field(..., ge=1)
    quote: str = Field(..., min_length=1, max_length=4000)


class CommentCreate(BaseModel):
    """A new comment, or a new version superseding the comment's tip."""

    reviewer_id: UUID
    number: int = Field(..., ge=1, le=32767)
    body: str = Field(..., min_length=1, max_length=20000)
    anchor: Optional[AnchorCreate] = Field(default=None)
    supersedes_comment_id: Optional[UUID] = Field(default=None)
    idempotency_key: str = _KEY


class ResponseCreate(BaseModel):
    """``change`` links a later saved version (its anchored diff must touch
    the commented passage); ``no_change`` carries an attributed rationale."""

    kind: ResponseKind
    body: str = Field(..., min_length=1, max_length=20000)
    revised_draft_id: Optional[UUID] = Field(default=None)
    base_draft_id: Optional[UUID] = Field(default=None)
    rationale: Optional[str] = Field(default=None, max_length=20000)
    evidence_claim_version_ids: List[UUID] = Field(default_factory=list, max_length=20)
    supersedes_response_id: Optional[UUID] = Field(default=None)
    idempotency_key: str = _KEY


class DecisionCreate(BaseModel):
    """``assigned`` (EDIT) names a project member; ``resolved`` (ADJUDICATE)
    names the response tip it accepts; ``reopened`` (ADJUDICATE)."""

    kind: DecisionKind
    assignee_id: Optional[UUID] = Field(default=None)
    response_id: Optional[UUID] = Field(default=None)
    rationale: Optional[str] = Field(default=None, max_length=20000)
    supersedes_decision_id: Optional[UUID] = Field(default=None)
    idempotency_key: str = _KEY


class ReviewerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    round_id: UUID
    label: str
    display_name: Optional[str] = Field(default=None)
    created_by_id: UUID
    created_at: datetime


class RoundResponse(BaseModel):
    id: UUID
    collection_id: UUID
    draft_id: UUID
    draft_version: int
    draft_content_hash: str
    label: str
    received_at: Optional[date] = Field(default=None)
    created_by_id: UUID
    created_at: datetime
    reviewers: List[ReviewerResponse]


class RoundListResponse(BaseModel):
    rounds: List[RoundResponse]


class CommentVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    round_id: UUID
    reviewer_id: UUID
    number: int
    body: str
    draft_id: UUID
    draft_content_hash: str
    start_char: Optional[int] = Field(default=None)
    end_char: Optional[int] = Field(default=None)
    quote: Optional[str] = Field(default=None)
    quote_sha256: Optional[str] = Field(default=None)
    supersedes_comment_id: Optional[UUID] = Field(default=None)
    author_id: UUID
    created_at: datetime


class CommentResponse(CommentVersionResponse):
    comment_root_id: UUID


class DiffHunk(BaseModel):
    op: DiffOp
    old: List[int]
    new: List[int]
    old_text: str
    new_text: str


class EvidenceClaim(BaseModel):
    claim_version_id: UUID
    claim_id: UUID
    text: str
    assessment_stance: Optional[str] = Field(default=None)


class ResponseVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    comment_root_id: UUID
    kind: ResponseKind
    body: str
    revised_draft_id: Optional[UUID] = Field(default=None)
    revised_content_hash: Optional[str] = Field(default=None)
    base_draft_id: Optional[UUID] = Field(default=None)
    diff_sha256: Optional[str] = Field(default=None)
    rationale: Optional[str] = Field(default=None)
    evidence_claim_version_ids: List[UUID]
    supersedes_response_id: Optional[UUID] = Field(default=None)
    author_id: UUID
    created_at: datetime


class ResponseView(ResponseVersionResponse):
    """A response with its diff recomputed from the retained versions."""

    diff_verified: Optional[bool] = Field(default=None)
    hunks: List[DiffHunk] = Field(default_factory=list)
    evidence: List[EvidenceClaim] = Field(default_factory=list)


class DecisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    comment_root_id: UUID
    kind: DecisionKind
    assignee_id: Optional[UUID] = Field(default=None)
    response_id: Optional[UUID] = Field(default=None)
    rationale: Optional[str] = Field(default=None)
    actor_id: UUID
    actor_role: str
    supersedes_decision_id: Optional[UUID] = Field(default=None)
    created_at: datetime


class DraftRef(BaseModel):
    id: UUID
    version: int
    content_hash: str


class CommentDetail(BaseModel):
    """One comment's full history with derived status and anchor state
    against the target version (the original quote is always kept)."""

    comment_root_id: UUID
    reviewer_id: UUID
    reviewer_label: str
    number: int
    status: CommentStatus
    anchor_state: AnchorStateName
    anchor_start: Optional[int] = Field(default=None)
    anchor_end: Optional[int] = Field(default=None)
    current: CommentVersionResponse
    versions: List[CommentVersionResponse]
    response: Optional[ResponseView] = Field(default=None)
    responses: List[ResponseVersionResponse]
    assignment: Optional[DecisionResponse] = Field(default=None)
    resolution: Optional[DecisionResponse] = Field(default=None)
    decisions: List[DecisionResponse]


class RoundDetail(BaseModel):
    round: RoundResponse
    target: DraftRef
    comments: List[CommentDetail]


class DraftDiffResponse(BaseModel):
    """Sentence-level anchored diff between two saved versions."""

    model_config = ConfigDict(populate_by_name=True)

    from_: DraftRef = Field(..., alias="from")
    to: DraftRef
    hunks: List[DiffHunk]
