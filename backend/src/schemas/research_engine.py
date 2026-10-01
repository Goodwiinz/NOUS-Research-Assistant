"""Pydantic v2 schemas for the research engine API."""

import json
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional, TypeVar, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.services.research_engine import manifest_rules

# These limits are deliberately server-owned.  They protect both newly
# validated blueprints and the legacy JSONB rows that are revalidated by the
# execution path before a paid call is made.
MAX_BLUEPRINT_STEPS = 32
MAX_NESTED_PAYLOAD_BYTES = 32 * 1024
MAX_PROMPT_TEMPLATE_CHARS = 16 * 1024
MAX_REVIEW_ITEMS = 200
MAX_REVIEW_ID_CHARS = 512
MAX_REVIEW_SAFE_FIELD_CHARS = 64
# A projected pending review retains two bounded identities and one bounded safe
# field for every supported candidate. Six bytes per character covers JSON
# control escaping; the per-item and envelope allowances cover keys/markers.
MAX_PENDING_REVIEW_OUTPUT_BYTES = 4096 + MAX_REVIEW_ITEMS * (
    (2 * MAX_REVIEW_ID_CHARS * 6) + (MAX_REVIEW_SAFE_FIELD_CHARS * 6) + 512
)

_PayloadT = TypeVar("_PayloadT")


def serialized_payload_size(value: Any) -> int:
    """Return the compact JSON size used for request/runtime accounting."""
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("payload must be JSON serializable") from exc


def _bounded_payload(
    value: _PayloadT,
    field_name: str,
    max_bytes: int = MAX_NESTED_PAYLOAD_BYTES,
) -> _PayloadT:
    if serialized_payload_size(value) > max_bytes:
        raise ValueError(f"{field_name} exceeds the {max_bytes}-byte limit")
    return value


def _bounded_step_parameters(value: Dict[str, Any]) -> Dict[str, Any]:
    _bounded_payload(value, "step parameters")
    prompt = value.get("system_prompt_template")
    if prompt is not None and len(str(prompt)) > MAX_PROMPT_TEMPLATE_CHARS:
        raise ValueError(
            f"system prompt exceeds the {MAX_PROMPT_TEMPLATE_CHARS}-character limit"
        )
    return value


def validate_blueprint_runtime(blueprint: Dict[str, Any]) -> None:
    """Validate limits again for legacy JSONB rows before execution."""
    steps = blueprint.get("steps") or []
    if not isinstance(steps, list) or len(steps) > MAX_BLUEPRINT_STEPS:
        raise ValueError(f"blueprint exceeds the {MAX_BLUEPRINT_STEPS}-step limit")
    _bounded_payload(blueprint.get("parameters") or {}, "blueprint parameters")
    analyze_steps = 0
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("blueprint step must be an object")
        params = step.get("params") or step.get("parameters") or {}
        _bounded_payload(params, "step parameters")
        if step.get("type") == manifest_rules.STEP_TYPE:
            # GOO-312: a bad experiment plan fails before any sandbox runs.
            manifest_rules.parse_analyze_step(params)
            analyze_steps += 1
        # ponytail: one manifest binds one analyze step; chains of experiments
        # in one run wait for a pilot that needs them.
        if analyze_steps > 1:
            raise ValueError("multiple_analyze_steps")
        prompt = step.get("system_prompt_template")
        if prompt is None:
            prompt = params.get("system_prompt_template")
        if prompt is not None and len(str(prompt)) > MAX_PROMPT_TEMPLATE_CHARS:
            raise ValueError(
                f"system prompt exceeds the {MAX_PROMPT_TEMPLATE_CHARS}-character limit"
            )


# ============================================================================
# Enums
# ============================================================================


class StepType(str, Enum):
    """Types of steps in a research blueprint."""

    SEARCH = "search"
    SCREEN = "screen"
    EXTRACT = "extract"
    SYNTHESIZE = "synthesize"
    VERIFY = "verify"
    EXPORT = "export"
    # GOO-312: a computational experiment bound to the approved plan.
    ANALYZE = "analyze"


class ExportFormat(str, Enum):
    """Portable formats supported by the audited research export endpoint."""

    MARKDOWN = "markdown"
    JSON = "json"
    CSV = "csv"


class ExecutionMode(str, Enum):
    """Execution mode for a blueprint step."""

    DETERMINISTIC = "deterministic"
    EXPLORATORY = "exploratory"


class RunStatus(str, Enum):
    """Status of a research run."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class GroundingStatus(str, Enum):
    """Grounding verification status for evidence."""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    FAILED = "failed"


class ReviewKind(str, Enum):
    """Durable review gates supported by the Daily Research Brief contract."""

    SCREENING = "screening"
    EXTRACTION = "extraction"
    FINAL = "final"


class ReviewDecision(str, Enum):
    """Top-level reviewer disposition for a persisted stage output."""

    APPROVE = "approve"
    DECLINE = "decline"


class ConnectorFeatures(BaseModel):
    """Safe, behavioral search features for a research connector."""

    model_config = {"extra": "forbid"}

    full_text: bool
    date_filter: bool
    cursor: bool


class ConnectorCapabilityResponse(BaseModel):
    """Non-sensitive connector metadata returned to setup clients."""

    model_config = {"extra": "forbid"}

    id: str
    label: str
    daily_brief_eligible: bool
    available: bool
    features: ConnectorFeatures


# ============================================================================
# Project Schemas
# ============================================================================


class ProjectCreate(BaseModel):
    """Schema for creating a research project."""

    collection_id: Optional[UUID] = None
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    settings: Dict[str, Any] = Field(default_factory=dict)


class ProjectUpdate(BaseModel):
    """Schema for updating a research project."""

    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None


class ProjectLink(BaseModel):
    """One-time explicit link to an existing canonical Collection."""

    collection_id: UUID


class ProjectResponse(BaseModel):
    """Schema for project API responses."""

    model_config = {"from_attributes": True}

    id: UUID
    project_id: UUID
    collection_id: Optional[UUID] = None
    research_engine_project_id: Optional[UUID] = None
    blueprint_id: Optional[UUID] = None
    name: str
    description: Optional[str] = None
    status: str
    settings: Dict[str, Any]
    created_at: datetime
    updated_at: datetime


class LegacyProjectResponse(BaseModel):
    model_config = {"from_attributes": True}

    research_engine_project_id: UUID
    project_id: Optional[UUID] = None
    collection_id: Optional[UUID] = None
    name: str
    description: Optional[str] = None
    status: str


class ResearchProjectRoleCreate(BaseModel):
    user_id: UUID
    role: str


class ResearchProjectRoleResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    project_id: UUID
    user_id: UUID
    role: str
    assigned_by_id: UUID
    created_at: datetime


class ResearchQuestionVersionCreate(BaseModel):
    question: str = Field(..., min_length=1, max_length=10_000)
    hypothesis: Optional[str] = Field(default=None, max_length=10_000)
    scope: Optional[str] = Field(default=None, max_length=10_000)
    framework: Dict[str, Any] = Field(default_factory=dict)
    parent_version_id: Optional[UUID] = None

    @field_validator("framework")
    @classmethod
    def framework_is_bounded(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return _bounded_payload(value, "question framework")


class ResearchQuestionCreate(ResearchQuestionVersionCreate):
    pass


class ResearchQuestionVersionResponse(BaseModel):
    id: UUID
    question_id: UUID
    version: int
    parent_version_id: Optional[UUID] = None
    question: str
    hypothesis: Optional[str] = None
    scope: Optional[str] = None
    framework: Dict[str, Any]
    content_hash: str
    author_user_id: UUID
    created_at: datetime


class ResearchQuestionResponse(BaseModel):
    id: UUID
    project_id: UUID
    current_version_id: UUID
    current_version: ResearchQuestionVersionResponse
    versions: List[ResearchQuestionVersionResponse] = Field(default_factory=list)
    created_at: datetime


class ProtocolSnapshot(BaseModel):
    eligibility: Dict[str, Any] = Field(..., min_length=1)
    sources_search: Dict[str, Any] = Field(..., min_length=1)
    selection: Dict[str, Any] = Field(..., min_length=1)
    extraction: Dict[str, Any] = Field(..., min_length=1)
    appraisal_synthesis: Dict[str, Any] = Field(..., min_length=1)
    outcomes: Dict[str, Any] = Field(..., min_length=1)
    reviewer_mode: Dict[str, Any] = Field(..., min_length=1)

    @field_validator(
        "eligibility",
        "sources_search",
        "selection",
        "extraction",
        "appraisal_synthesis",
        "outcomes",
        "reviewer_mode",
    )
    @classmethod
    def section_is_bounded(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return _bounded_payload(value, "protocol section")


class ResearchProtocolCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    question_version_id: UUID
    blueprint_id: UUID
    snapshot: ProtocolSnapshot


class ResearchProtocolVersionCreate(BaseModel):
    question_version_id: UUID
    blueprint_id: UUID
    snapshot: ProtocolSnapshot
    parent_version_id: UUID
    amendment_reason: str = Field(..., min_length=1, max_length=10_000)


class ResearchProtocolVersionResponse(BaseModel):
    id: UUID
    protocol_id: UUID
    version: int
    parent_version_id: Optional[UUID] = None
    question_version_id: UUID
    blueprint_id: UUID
    execution_plan: Dict[str, Any]
    snapshot: ProtocolSnapshot
    content_hash: str
    status: str
    change_kind: str
    amendment_reason: Optional[str] = None
    author_user_id: UUID
    approved_by_user_id: Optional[UUID] = None
    approved_at: Optional[datetime] = None
    superseded_at: Optional[datetime] = None
    created_at: datetime
    can_approve: bool = False


class ResearchProtocolResponse(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    current_draft_version_id: Optional[UUID] = None
    current_approved_version_id: Optional[UUID] = None
    versions: List[ResearchProtocolVersionResponse] = Field(default_factory=list)
    can_edit: bool = False
    can_manage: bool = False
    can_approve: bool = False
    created_at: datetime
    updated_at: datetime


class ProtocolApprovalRequest(BaseModel):
    expected_protocol_version: int = Field(..., ge=1)
    expected_content_hash: str = Field(..., min_length=64, max_length=64)
    expected_current_approved_version_id: Optional[UUID] = None
    reason: str = Field(..., min_length=1, max_length=10_000)
    idempotency_key: str = Field(..., min_length=1, max_length=240)


class ProtocolApprovalResponse(BaseModel):
    decision_id: UUID
    protocol_id: UUID
    protocol_version_id: UUID
    content_hash: str
    actor_user_id: UUID
    actor_role: str
    reason: Optional[str] = None
    approved_at: datetime


# --- Report / study identity (GOO-299) ------------------------------------

StudyLinkStatus = Literal["proposed", "confirmed", "disputed"]


class ReportObservationResponse(BaseModel):
    source_id: UUID
    run_id: UUID
    match_method: str
    evidence: Dict[str, Any]


class ImportedRecordObservation(BaseModel):
    """An externally imported record (GOO-300) attached to this report."""

    import_record_id: UUID
    receipt_id: UUID
    match_method: str
    evidence: Dict[str, Any]


class ReportResponse(BaseModel):
    id: UUID
    title_snapshot: str
    identifiers: Dict[str, List[str]]
    study_id: Optional[UUID] = None
    study_link_status: Optional[StudyLinkStatus] = None
    study_link_rationale: Optional[str] = None
    merged_into_report_id: Optional[UUID] = None
    observations: List[ReportObservationResponse]
    imported_records: List[ImportedRecordObservation] = []


class ReportSuggestion(BaseModel):
    report_id: UUID
    reason: Literal["title_year"]


class ReportCandidatesResponse(BaseModel):
    """Read-only suggestions; titles and years never equate reports."""

    report_id: UUID
    suggested: List[ReportSuggestion]
    conflicts: List[Dict[str, Any]]


class StudyLinkRequest(BaseModel):
    study_id: Optional[UUID] = None
    status: StudyLinkStatus
    rationale: str = Field(..., min_length=1, max_length=10_000)
    idempotency_key: str = Field(..., min_length=1, max_length=240)


class ReportMergeRequest(BaseModel):
    surviving_report_id: UUID
    merged_report_ids: List[UUID] = Field(..., min_length=1)
    rationale: str = Field(..., min_length=1, max_length=10_000)
    idempotency_key: str = Field(..., min_length=1, max_length=240)


class ReportSplitRequest(BaseModel):
    source_ids: List[UUID] = []
    import_record_ids: List[UUID] = []
    rationale: str = Field(..., min_length=1, max_length=10_000)
    idempotency_key: str = Field(..., min_length=1, max_length=240)

    @model_validator(mode="after")
    def _moves_something(self) -> "ReportSplitRequest":
        if not self.source_ids and not self.import_record_ids:
            raise ValueError("name at least one source_id or import_record_id")
        return self


class IdentityEventResponse(BaseModel):
    seq: int
    event_type: str
    actor_user_id: UUID
    actor_role: str
    reason: Optional[str] = None
    payload: Dict[str, Any]
    occurred_at: datetime


# --- Screening queues (GOO-301) ---------------------------------------------

ScreeningStage = Literal["title_abstract", "full_text"]
ScreeningDecisionValue = Literal["include", "exclude", "uncertain"]
_IdempotencyKey = Annotated[str, Field(min_length=1, max_length=240)]


class ScreeningQueueCreate(BaseModel):
    protocol_version_id: UUID
    stage: ScreeningStage
    # None on title_abstract = every live report; full_text needs an explicit list.
    report_ids: Optional[List[UUID]] = Field(
        default=None, min_length=1, max_length=10_000
    )
    supersedes_queue_id: Optional[UUID] = None
    suggestion_step_id: Optional[UUID] = None
    idempotency_key: _IdempotencyKey


class ScreeningAssignmentCreate(BaseModel):
    reviewer_user_id: UUID
    idempotency_key: _IdempotencyKey


class ScreeningRevokeRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=10_000)
    idempotency_key: _IdempotencyKey


class ScreeningObservationCreate(BaseModel):
    report_id: UUID
    assignment_id: UUID
    criteria_hash: str = Field(..., min_length=64, max_length=64)
    decision: ScreeningDecisionValue
    exclusion_reason: Optional[str] = Field(default=None, min_length=1, max_length=200)
    note: Optional[str] = Field(default=None, max_length=10_000)
    supersedes_observation_id: Optional[UUID] = None
    idempotency_key: _IdempotencyKey


class ScreeningQueueResponse(BaseModel):
    """Counts only: decision breakdowns belong to GOO-302's reveal rules."""

    id: UUID
    stage: ScreeningStage
    protocol_version_id: UUID
    criteria_hash: str
    reviewer_mode: str
    supersedes_queue_id: Optional[UUID] = None
    created_by_id: UUID
    created_at: datetime
    report_count: int
    assignment_count: int
    observation_count: int
    suggestion_count: int
    # Set only on the create response that imported them.
    suggestions_skipped: Optional[int] = None
    stale: Optional[str] = None
    # GOO-302: counted from revealed (resolution tip) rows only.
    resolved_count: int = 0
    conflict_count: int = 0


class ScreeningAssignmentResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    queue_id: UUID
    reviewer_id: UUID
    assigned_by_id: UUID
    created_at: datetime
    revoked_at: Optional[datetime] = None
    revoked_by_id: Optional[UUID] = None


ScreeningBasis = Literal["single", "agreement", "conflict", "adjudicated", "reopened"]


class ScreeningResolutionResponse(BaseModel):
    """A derived or adjudicated outcome; never an editable field (GOO-302)."""

    model_config = {"from_attributes": True}

    id: UUID
    report_id: UUID
    basis: ScreeningBasis
    outcome: Optional[ScreeningDecisionValue] = None
    exclusion_reason: Optional[str] = None
    input_observation_ids: List[UUID]
    criteria_hash: str
    supersedes_resolution_id: Optional[UUID] = None
    created_at: datetime


class ScreeningObservationResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    queue_id: UUID
    report_id: UUID
    reviewer_id: UUID
    assignment_id: UUID
    decision: ScreeningDecisionValue
    exclusion_reason: Optional[str] = None
    note: Optional[str] = None
    supersedes_observation_id: Optional[UUID] = None
    created_at: datetime
    # Set only on the submit response whose observation revealed the report.
    resolution: Optional[ScreeningResolutionResponse] = None


class MyScreeningQueueInfo(BaseModel):
    id: UUID
    stage: ScreeningStage
    protocol_version_id: UUID
    criteria_hash: str
    exclusion_reasons: List[str]
    stale: Optional[str] = None


class MyScreeningQueueItem(BaseModel):
    report_id: UUID
    title_snapshot: str
    identifiers: Dict[str, List[str]]
    abstract: Optional[str] = None
    # The caller's own current observation.
    observation: Optional[ScreeningObservationResponse] = None
    # GOO-302: "revealed" iff the report has a resolution in this queue. Peers'
    # current observations appear in ``others`` only once revealed.
    reveal_state: Literal["hidden", "revealed"] = "hidden"
    others: List[ScreeningObservationResponse] = Field(default_factory=list)
    resolution: Optional[ScreeningResolutionResponse] = None


class ScreeningCounts(BaseModel):
    total: int
    screened: int
    remaining: int
    revealed: int = 0
    conflicts: int = 0


class MyScreeningQueueResponse(BaseModel):
    queue: MyScreeningQueueInfo
    assignment_id: UUID
    items: List[MyScreeningQueueItem]
    counts: ScreeningCounts


class ScreeningConflictResponse(BaseModel):
    report_id: UUID
    title_snapshot: str
    identifiers: Dict[str, List[str]]
    # The queue's pinned protocol reasons, for a full-text exclusion ruling.
    exclusion_reasons: List[str]
    resolution: ScreeningResolutionResponse
    observations: List[ScreeningObservationResponse]


class ScreeningAdjudicateRequest(BaseModel):
    """Resolve the exact conflict tip the adjudicator saw (stale inputs: 409)."""

    resolution_id: UUID
    input_observation_ids: List[UUID] = Field(..., min_length=1, max_length=10)
    criteria_hash: str = Field(..., min_length=64, max_length=64)
    decision: ScreeningDecisionValue
    exclusion_reason: Optional[str] = Field(default=None, min_length=1, max_length=200)
    rationale: str = Field(..., min_length=1, max_length=10_000)
    idempotency_key: _IdempotencyKey


class ScreeningReopenRequest(BaseModel):
    resolution_id: UUID
    rationale: str = Field(..., min_length=1, max_length=10_000)
    idempotency_key: _IdempotencyKey


class ScreeningEventResponse(IdentityEventResponse):
    """A screening event; a peer's hidden decision is ``redacted`` (GOO-302)."""

    redacted: bool = False


# --- Full-text acquisition + PRISMA flow (GOO-303) --------------------------

FulltextOutcome = Literal["requested", "retrieved", "unavailable"]


class FulltextRequestCreate(BaseModel):
    report_id: UUID
    idempotency_key: _IdempotencyKey


class FulltextAttemptCreate(BaseModel):
    """One attempt; ``previous_attempt_id`` must be the current head (or null)."""

    outcome: FulltextOutcome
    attempted_on: date
    reason: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    document_id: Optional[UUID] = None
    previous_attempt_id: Optional[UUID] = None
    idempotency_key: _IdempotencyKey

    @model_validator(mode="after")
    def _outcome_fields(self) -> "FulltextAttemptCreate":
        if (self.outcome == "retrieved") != (self.document_id is not None):
            raise ValueError("document_id is required for, and only for, retrieved")
        if self.outcome == "unavailable" and not (self.reason or "").strip():
            raise ValueError("unavailable needs a reason")
        # One day of slack: the actor reports their own local date.
        if self.attempted_on > datetime.now(timezone.utc).date() + timedelta(days=1):
            raise ValueError("attempted_on cannot be in the future")
        return self


class FulltextAttemptResponse(BaseModel):
    id: UUID
    outcome: FulltextOutcome
    reason: Optional[str] = None
    attempted_on: date
    actor_id: UUID
    document_id: Optional[UUID] = None
    document_content_hash: Optional[str] = None
    # False once the linked document is deleted or detached; id and hash stay.
    document_available: bool = False
    previous_attempt_id: Optional[UUID] = None
    created_at: datetime


class FulltextStateResponse(BaseModel):
    request_id: UUID
    report_id: UUID
    protocol_version_id: Optional[UUID] = None
    requested_by_id: UUID
    requested_at: datetime
    state: Literal["pending", "requested", "retrieved", "unavailable"]
    head_attempt_id: Optional[UUID] = None
    attempts: List[FulltextAttemptResponse]  # chain order, first attempt first


class PrismaCounts(BaseModel):
    records_identified: int
    records_by_source: Dict[str, int]
    records_by_import: Dict[str, int]
    import_rejected: int
    duplicates_removed: int
    unique_reports: int
    records_screened: int
    records_excluded: int
    records_awaiting_screening: int
    reports_sought: int
    reports_not_retrieved: int
    reports_awaiting_retrieval: int
    reports_assessed: int
    reports_excluded_by_reason: Dict[str, int]
    included_reports: int
    included_studies: int
    unconfirmed_study_links: int


class PrismaAmendment(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    event_id: UUID
    aggregate_type: str
    seq: int
    kind: str
    report_id: UUID
    from_: str = Field(..., alias="from")
    to: str


class PrismaChecks(BaseModel):
    screened_plus_awaiting_equals_unique: bool
    assessed_within_retrieved: bool
    included_plus_excluded_equals_assessed: bool


class PrismaVersions(BaseModel):
    corpus_hash: str
    protocol_version_ids: List[str]
    stream_heads: Dict[str, int]


class PrismaFlowBody(BaseModel):
    counts: PrismaCounts
    excluded_from_flow: Dict[str, int]
    amendments: List[PrismaAmendment]
    warnings: List[str]
    checks: PrismaChecks
    versions: PrismaVersions


class PrismaFlowResponse(BaseModel):
    """Recomputed from persisted rows on every call; nothing here is stored."""

    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(..., alias="schema")
    generated_at: datetime
    body_sha256: str
    body: PrismaFlowBody


# --- Plan-to-write journey (GOO-308) -----------------------------------------

JourneyStageKey = Literal["plan", "discover", "select", "extract", "write"]
JourneyStatus = Literal["not_started", "in_progress", "attention", "complete"]


class JourneyStage(BaseModel):
    """Derived from persisted rows on every call (``journey.derive_stages``)."""

    key: JourneyStageKey
    status: JourneyStatus
    facts: Dict[str, Union[int, bool, str, None]]
    blockers: List[str]


class JourneyResponse(BaseModel):
    stages: List[JourneyStage]
    current: Optional[JourneyStageKey] = None


# --- Study-design appraisal (GOO-309) ------------------------------------------

AppraisalResponseCode = Literal["Y", "PY", "PN", "N", "NI", "NA"]
AppraisalJudgment = Literal["low", "some_concerns", "high"]
AppraisalDesign = Literal[
    "randomized_parallel_group",
    "randomized_cluster",
    "randomized_crossover",
    "non_randomized_intervention",
    "cohort",
    "case_control",
    "cross_sectional",
    "other",
]
AppraisalStatus = Literal["awaiting_independent", "agreed", "conflict", "adjudicated"]


class AppraisalEvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["accepted_value", "observation"]
    id: UUID


class AppraisalDomain(BaseModel):
    """One RoB 2 domain; ``None`` means unknown and is never derived."""

    model_config = ConfigDict(extra="forbid")

    judgment: Optional[AppraisalJudgment] = None
    signals: Dict[str, Optional[AppraisalResponseCode]] = Field(default_factory=dict)
    rationale: Optional[str] = Field(None, min_length=1, max_length=4000)
    evidence: List[AppraisalEvidenceRef] = Field(default_factory=list, max_length=20)


class AppraisalSubmit(BaseModel):
    protocol_version_id: UUID
    instrument_key: str = Field(..., min_length=1, max_length=32)
    instrument_version: str = Field(..., min_length=1, max_length=32)
    study_id: Optional[UUID] = None
    report_id: Optional[UUID] = None
    outcome_key: str = Field(..., min_length=1, max_length=100)
    timepoint: str = Field(..., min_length=1, max_length=100)
    study_design: AppraisalDesign
    applicability: Literal["applicable", "not_applicable"]
    domains: Dict[str, AppraisalDomain] = Field(default_factory=dict, max_length=5)
    overall: Optional[AppraisalJudgment] = None
    supersedes_assessment_id: Optional[UUID] = None
    idempotency_key: str = Field(..., min_length=1, max_length=240)

    @model_validator(mode="after")
    def one_target(self) -> "AppraisalSubmit":
        if (self.study_id is None) == (self.report_id is None):
            raise ValueError("Name exactly one of study_id or report_id")
        return self


class AppraisalAdjudicate(AppraisalSubmit):
    resolves_assessment_ids: List[UUID] = Field(..., min_length=1, max_length=10)
    rationale: str = Field(..., min_length=1, max_length=4000)


class AppraisalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    collection_id: UUID
    protocol_version_id: UUID
    instrument_key: str
    instrument_version: str
    instrument_spec_hash: str
    study_id: Optional[UUID] = None
    report_id: Optional[UUID] = None
    target_key: str
    outcome_key: str
    timepoint: str
    study_design: str
    applicability: str
    domains: Dict[str, AppraisalDomain]
    overall: Optional[str] = None
    kind: Literal["independent", "adjudicated"]
    actor_role: str
    assessor_id: UUID
    assessor_name: Optional[str] = None
    resolves_assessment_ids: Optional[List[UUID]] = None
    rationale: Optional[str] = None
    input_hash: str
    supersedes_assessment_id: Optional[UUID] = None
    created_at: datetime
    superseded: bool = False
    stale: bool = False


class AppraisalEvidenceOption(BaseModel):
    """An accepted-value tip of this unit's documents that a domain may cite."""

    kind: Literal["accepted_value"] = "accepted_value"
    id: UUID
    document_id: UUID
    field_id: UUID
    value: Any = None
    missingness: Optional[str] = None
    quote: Optional[str] = None


class AppraisalResult(BaseModel):
    target_key: str
    study_id: Optional[UUID] = None
    report_id: Optional[UUID] = None
    outcome_key: str
    timepoint: str
    status: AppraisalStatus
    unresolved_domains: List[str]
    stale: bool
    mine: bool
    rows: List[AppraisalResponse]
    evidence_options: List[AppraisalEvidenceOption] = Field(default_factory=list)


class AppraisalInstrumentDomain(BaseModel):
    id: str
    name: str
    signals: List[str]


class AppraisalInstrument(BaseModel):
    key: str
    version: str
    spec_hash: str
    mode: str
    variant: str
    domains: List[AppraisalInstrumentDomain]
    responses: List[str]
    judgments: List[str]
    designs: List[str]
    applies_to: List[str]
    licence: str
    encoding: str
    source: str


class AppraisalListResponse(BaseModel):
    """Status, staleness and visibility are derived on every read."""

    protocol_version_id: Optional[UUID] = None
    instrument: Optional[AppraisalInstrument] = None
    outcomes: Dict[str, List[str]] = Field(default_factory=dict)
    results: List[AppraisalResult]


# --- Evidence tables, contradictions and outcome certainty (GOO-310) ----------

EvidenceCellState = Literal["value", "missingness", "missing", "conflict"]
ContradictionKind = Literal["opened", "resolved", "acknowledged", "dissent"]
ContradictionStatus = Literal["unresolved", "resolved", "acknowledged"]
CertaintyLevel = Literal["very_low", "low", "moderate", "high"]
CertaintyRating = Optional[Literal[0, -1, -2]]


class EvidenceTip(BaseModel):
    """One accepted value behind a cell, pinned to its source revision."""

    accepted_value_id: UUID
    document_id: UUID
    report_id: UUID
    source_hash: str
    text_sha256: Optional[str] = None
    value: Any = None
    missingness: Optional[str] = None


class EvidenceCell(BaseModel):
    """``missing`` (no accepted value) is its own state, never a blank."""

    state: EvidenceCellState
    value: Any = None
    missingness: Optional[str] = None
    tips: List[EvidenceTip]


class EvidenceRow(BaseModel):
    """Exactly one row per analysis unit (``study:<id>`` or ``report:<id>``)."""

    row_key: str
    unit: str
    report_ids: List[UUID]
    cells: Dict[str, EvidenceCell]


class EvidenceExcluded(BaseModel):
    document_id: UUID
    report_id: Optional[UUID] = None
    reason: Literal["study_link_unresolved", "no_report_identity"]


class EvidenceUnreviewedCell(BaseModel):
    """A machine or legacy matrix cell: shown, never written into a version."""

    document_id: UUID
    field_id: UUID
    column_name: str
    value: Any = None
    missingness: Optional[str] = None
    source: Literal["machine", "legacy"]
    review_state: Literal["unreviewed"] = "unreviewed"


class StanceSuggestion(BaseModel):
    id: UUID
    source_id: UUID
    stance: str
    confidence: float
    model_version: str
    inference_model_version: Optional[str] = None


class StanceSuggestionGroup(BaseModel):
    """Model output grouped by claim; it never creates or resolves anything."""

    claim_hash: str
    claim_text: Optional[str] = None
    review_state: Literal["unreviewed_model_suggestion"]
    suggestions: List[StanceSuggestion]


class EvidenceTablePreview(BaseModel):
    protocol_version_id: UUID
    outcome_key: str
    timepoint: str
    matrix_id: UUID
    form_version_id: UUID
    field_ids: List[UUID]
    rows: List[EvidenceRow]
    excluded: List[EvidenceExcluded]
    content_hash: str
    tip_id: Optional[UUID] = None
    differs_from_tip: bool
    unreviewed_cells: List[EvidenceUnreviewedCell]
    stance_suggestions: List[StanceSuggestionGroup]


class EvidenceTableCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome_key: str = Field(..., min_length=1, max_length=100)
    timepoint: str = Field(..., min_length=1, max_length=100)
    matrix_id: UUID
    field_ids: List[UUID] = Field(..., min_length=1, max_length=50)
    supersedes_table_id: Optional[UUID] = None
    idempotency_key: str = Field(..., min_length=1, max_length=240)

    @field_validator("field_ids")
    @classmethod
    def unique_fields(cls, value: List[UUID]) -> List[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("field_ids must be unique")
        return value


class EvidenceTableResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    collection_id: UUID
    protocol_version_id: UUID
    outcome_key: str
    timepoint: str
    matrix_id: UUID
    form_version_id: UUID
    field_ids: List[UUID]
    rows: List[EvidenceRow]
    excluded: List[EvidenceExcluded]
    content_hash: str
    created_by_id: UUID
    supersedes_table_id: Optional[UUID] = None
    created_at: datetime
    superseded: bool = False
    stale: bool = False


class ContradictionCreate(BaseModel):
    """``opened`` names a table version, a field and 2+ of its cell values;
    every later row names its group and the chain tip it follows."""

    model_config = ConfigDict(extra="forbid")

    kind: ContradictionKind
    contradiction_id: Optional[UUID] = None
    previous_id: Optional[UUID] = None
    table_version_id: Optional[UUID] = None
    field_id: Optional[UUID] = None
    accepted_value_ids: Optional[List[UUID]] = Field(None, max_length=50)
    stance_classification_ids: List[UUID] = Field(default_factory=list, max_length=20)
    explanation: str = Field(..., min_length=1, max_length=4000)
    idempotency_key: str = Field(..., min_length=1, max_length=240)

    @model_validator(mode="after")
    def shape(self) -> "ContradictionCreate":
        if self.kind == "opened":
            members = self.accepted_value_ids or []
            if (
                self.table_version_id is None
                or self.field_id is None
                or len(members) < 2
                or len(set(members)) != len(members)
            ):
                raise ValueError(
                    "An opened contradiction names a table version, a field"
                    " and 2+ distinct accepted values"
                )
            if self.contradiction_id is not None or self.previous_id is not None:
                raise ValueError("An opened contradiction starts a new chain")
        else:
            if self.contradiction_id is None or self.previous_id is None:
                raise ValueError("Name the contradiction and the row it follows")
            if (
                self.table_version_id is not None
                or self.field_id is not None
                or self.accepted_value_ids is not None
                or self.stance_classification_ids
            ):
                raise ValueError("Only an opened contradiction cites members")
        return self


class ContradictionRowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    collection_id: UUID
    contradiction_id: UUID
    table_version_id: UUID
    field_id: UUID
    accepted_value_ids: Optional[List[UUID]] = None
    kind: ContradictionKind
    explanation: str
    actor_id: UUID
    actor_name: Optional[str] = None
    actor_role: str
    suggestion: Optional[Dict[str, Any]] = None
    previous_id: Optional[UUID] = None
    created_at: datetime


class ContradictionDissent(BaseModel):
    """A dissent row, or a resolution a later one superseded."""

    id: UUID
    contradiction_id: UUID
    kind: ContradictionKind
    actor_id: UUID
    actor_role: str
    explanation: str
    superseded: bool


class ContradictionResponse(BaseModel):
    """One group: the derived status, every row and the dissent kept visible."""

    contradiction_id: UUID
    table_version_id: UUID
    field_id: UUID
    status: ContradictionStatus
    rows: List[ContradictionRowResponse]
    dissent: List[ContradictionDissent]
    stale: bool = False


class CertaintyRatings(BaseModel):
    """GRADE's five downgrade domains; ``None`` is unknown, never derived."""

    model_config = ConfigDict(extra="forbid")

    risk_of_bias: CertaintyRating = None
    inconsistency: CertaintyRating = None
    indirectness: CertaintyRating = None
    imprecision: CertaintyRating = None
    publication_bias: CertaintyRating = None


class CertaintyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_version_id: UUID
    starting_level: Literal["high", "low"]
    ratings: CertaintyRatings
    level: Optional[CertaintyLevel] = None
    appraisal_assessment_ids: List[UUID] = Field(default_factory=list, max_length=200)
    contradiction_ids: List[UUID] = Field(default_factory=list, max_length=200)
    rationale: str = Field(..., min_length=1, max_length=4000)
    supersedes_certainty_id: Optional[UUID] = None
    idempotency_key: str = Field(..., min_length=1, max_length=240)


class CertaintyResponse(BaseModel):
    """Certainty only; agreement (contradictions) and model confidence live
    elsewhere. Unresolved contradictions and dissent ride along, always."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    collection_id: UUID
    table_version_id: UUID
    outcome_key: str
    timepoint: str
    method_key: str
    method_version: str
    starting_level: str
    domains: Dict[str, CertaintyRating]
    level: Optional[CertaintyLevel] = None
    appraisal_assessment_ids: List[UUID]
    contradiction_ids: List[UUID]
    rationale: str
    assessed_by_id: UUID
    assessor_name: Optional[str] = None
    actor_role: str
    input_hash: str
    supersedes_certainty_id: Optional[UUID] = None
    created_at: datetime
    superseded: bool = False
    stale: bool = False
    unresolved_contradictions: List[UUID] = Field(default_factory=list)
    dissent: List[ContradictionDissent] = Field(default_factory=list)


class EvidenceOutcome(BaseModel):
    outcome_key: str
    timepoint: str
    tables: List[EvidenceTableResponse]
    contradictions: List[ContradictionResponse]
    certainty: List[CertaintyResponse]


class EvidenceCertaintyMethod(BaseModel):
    method: str
    version: str
    domains: List[str]
    levels: List[str]
    starting_levels: List[str]


class EvidenceOutcomeListResponse(BaseModel):
    """Every version, chain and stale flag; staleness is derived on read."""

    protocol_version_id: Optional[UUID] = None
    certainty_method: Optional[EvidenceCertaintyMethod] = None
    outcomes: List[EvidenceOutcome]


# --- Quantitative synthesis (GOO-311) ----------------------------------------

SynthesisStatus = Literal["computed", "validation_failed"]
_SHA256 = r"^[0-9a-f]{64}$"


class SynthesisRoles(BaseModel):
    """Which evidence-table field carries each arm statistic."""

    model_config = ConfigDict(extra="forbid")

    mean_i: UUID
    sd_i: UUID
    n_i: UUID
    mean_c: UUID
    sd_c: UUID
    n_c: UUID


class SynthesisInputs(BaseModel):
    mean_i: float
    sd_i: float
    n_i: int
    mean_c: float
    sd_c: float
    n_c: int


class SynthesisIncluded(BaseModel):
    """One analysis unit; ``g``, ``v`` and weights only once computed."""

    unit: str
    report_ids: List[UUID]
    accepted_value_ids: List[UUID]
    inputs: SynthesisInputs
    g: Optional[float] = None
    v: Optional[float] = None
    w_fixed: Optional[float] = None
    w_random: Optional[float] = None


class SynthesisExclusion(BaseModel):
    """A structured reason; run-level failures name no unit."""

    unit: Optional[str] = None
    report_ids: List[str]
    reason: str
    detail: str


class SynthesisSelection(BaseModel):
    measure: str
    model: str
    outcome_key: str
    timepoint: str


class SynthesisPreview(BaseModel):
    """Exactly the input set ``POST`` would use, with zero writes."""

    selection: SynthesisSelection
    table_version_id: UUID
    protocol_version_id: UUID
    config: Dict[str, Any]
    config_hash: str
    estimator_version: str
    included: List[SynthesisIncluded]
    excluded: List[SynthesisExclusion]
    run_failures: List[SynthesisExclusion]
    input_hash: str
    tip_id: Optional[UUID] = None
    tip_input_hash: Optional[str] = None


class SynthesisExecute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_version_id: UUID
    roles: SynthesisRoles
    expected_input_hash: str = Field(..., pattern=_SHA256)
    supersedes_result_id: Optional[UUID] = None
    idempotency_key: str = Field(..., min_length=1, max_length=240)


class SynthesisResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    collection_id: UUID
    protocol_version_id: UUID
    table_version_id: UUID
    outcome_key: str
    timepoint: str
    measure: str
    model: str
    config: Dict[str, Any]
    config_hash: str
    estimator_version: str
    software: Dict[str, Any]
    status: SynthesisStatus
    included: List[SynthesisIncluded]
    excluded: List[SynthesisExclusion]
    estimate: Optional[float] = None
    se: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    q: Optional[float] = None
    df: Optional[int] = None
    tau2: Optional[float] = None
    i2: Optional[float] = None
    input_hash: str
    result_hash: str
    executed_by_id: UUID
    actor_role: str
    supersedes_result_id: Optional[UUID] = None
    created_at: datetime
    superseded: bool = False
    stale: bool = False


class SynthesisListResponse(BaseModel):
    """Every result, stale and failed ones included; staleness is derived."""

    selection: Optional[SynthesisSelection] = None
    selection_error: Optional[str] = None
    results: List[SynthesisResultResponse]


# --- Search import / corpus (GOO-300) --------------------------------------


class ImportDeclaration(BaseModel):
    """What the importer declares about the search; never inferred from the file."""

    database: str = Field(..., min_length=1, max_length=200)
    query_text: Optional[str] = Field(default=None, min_length=1, max_length=20_000)
    search_date: Optional[date] = None
    exported_at: Optional[datetime] = None
    redistribution: Literal["restricted", "allowed"] = "restricted"
    notes: Optional[str] = Field(default=None, max_length=2000)


class CitationChaseDeclaration(BaseModel):
    """What a citation-chase receipt records as requested (server-set licence)."""

    seed_report_id: UUID
    direction: Literal["backward", "forward"]
    requested_limit: int
    redistribution: Literal["allowed"]


class ImportReceiptResponse(BaseModel):
    id: UUID
    kind: Literal["file_import", "citation_chase"]
    version: int
    previous_receipt_id: Optional[UUID] = None
    # Exactly the validated declaration: an import's, or a chase's request.
    declared: Union[ImportDeclaration, CitationChaseDeclaration]
    observed: Dict[str, Any]
    parsed_count: int
    accepted_count: int
    rejected_count: int
    replayed: bool = False
    created_at: datetime


class ImportRecordResponse(BaseModel):
    id: UUID
    record_index: int
    status: Literal["accepted", "rejected"]
    rejection_reason: Optional[str] = None
    parsed: Dict[str, Any]
    report_id: Optional[UUID] = None
    # Omitted (null) when the receipt's redistribution is "restricted".
    raw: Optional[str] = None


class ImportReceiptDetail(ImportReceiptResponse):
    records: List[ImportRecordResponse]


class CitationChaseRequest(BaseModel):
    seed_report_id: UUID
    direction: Literal["backward", "forward"]
    # 50 mirrors step_executor.MAX_CONNECTOR_RESULTS (pinned by a unit test).
    max_results: int = Field(default=50, ge=1, le=50)
    idempotency_key: str = Field(..., min_length=1, max_length=240)


COVERAGE_STATEMENT = (
    "Coverage lists what was searched, imported and chased for this project. "
    "It is not exhaustive and does not prove that no other relevant records exist."
)


class CoverageRequest(BaseModel):
    """Known records to check, each ``{kind: value}`` (e.g. ``{"doi": "10.1/x"}``)."""

    known: List[Dict[str, str]] = Field(default_factory=list, max_length=1000)


class CoverageResponse(BaseModel):
    found: List[Dict[str, Any]]
    missing: List[Dict[str, str]]
    recall: Optional[float] = None
    searched: List[Dict[str, Any]]
    not_searched: List[str]
    citation_chasing: Optional[Dict[str, Any]] = None
    exhaustive: Literal[False] = False
    statement: str = COVERAGE_STATEMENT


class ProtocolRegistrationCreate(BaseModel):
    protocol_version_id: UUID
    protocol_version_hash: str = Field(..., min_length=64, max_length=64)
    provider: str = Field(..., min_length=1, max_length=100)
    status: str
    idempotency_key: str = Field(..., min_length=1, max_length=255)
    external_identifier: Optional[str] = Field(default=None, max_length=255)
    url: Optional[str] = Field(default=None, max_length=2048)
    receipt: Optional[Dict[str, Any]] = None
    failure_reason: Optional[str] = Field(default=None, max_length=10_000)


class ProtocolRegistrationResponse(ProtocolRegistrationCreate):
    id: UUID
    collection_id: UUID
    recorded_by_user_id: UUID
    created_at: datetime


class ProtocolDeviationCreate(BaseModel):
    protocol_version_id: UUID
    run_id: Optional[UUID] = None
    output_reference: Optional[UUID] = None
    observed_difference: str = Field(..., min_length=1, max_length=20_000)
    rationale: str = Field(..., min_length=1, max_length=20_000)
    disposition: str = Field(..., min_length=1, max_length=32)

    @model_validator(mode="after")
    def output_requires_run(self) -> "ProtocolDeviationCreate":
        if self.output_reference is not None and self.run_id is None:
            raise ValueError("output_reference requires run_id")
        return self


class ProtocolDeviationResponse(ProtocolDeviationCreate):
    id: UUID
    collection_id: UUID
    actor_user_id: UUID
    created_at: datetime


# ============================================================================
# Blueprint Schemas
# ============================================================================


class BlueprintStepDefinition(BaseModel):
    """Definition of a single step in a research blueprint."""

    type: StepType
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)
    model_id: Optional[str] = None
    model_version: Optional[str] = None
    mode: ExecutionMode = ExecutionMode.DETERMINISTIC
    temperature: float = Field(default=0.0, ge=0, le=2)
    seed: Optional[int] = None
    system_prompt_template: Optional[str] = Field(
        default=None, max_length=MAX_PROMPT_TEMPLATE_CHARS
    )

    @field_validator("parameters")
    @classmethod
    def parameters_are_bounded(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return _bounded_step_parameters(value)

    @model_validator(mode="after")
    def analyze_plan_is_valid(self) -> "BlueprintStepDefinition":
        if self.type == StepType.ANALYZE:  # GOO-312
            manifest_rules.parse_analyze_step(self.parameters)
        return self


class BlueprintCreate(BaseModel):
    """Schema for creating a research blueprint."""

    name: str
    template_source: Optional[str] = None
    steps: List[BlueprintStepDefinition] = Field(
        default_factory=list, max_length=MAX_BLUEPRINT_STEPS
    )
    parameters: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def custom_blueprints_have_steps(self) -> "BlueprintCreate":
        if not self.steps and not self.template_source:
            raise ValueError("steps must not be empty")
        return self

    @field_validator("parameters")
    @classmethod
    def parameters_are_bounded(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return _bounded_payload(value, "blueprint parameters")


class BlueprintUpdate(BaseModel):
    """Schema for updating a research blueprint."""

    name: Optional[str] = None
    steps: Optional[List[BlueprintStepDefinition]] = Field(
        default=None, max_length=MAX_BLUEPRINT_STEPS
    )
    parameters: Optional[Dict[str, Any]] = None

    @field_validator("parameters")
    @classmethod
    def parameters_are_bounded(
        cls, value: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        return _bounded_payload(value, "blueprint parameters")


class BlueprintResponse(BaseModel):
    """Schema for blueprint API responses."""

    model_config = {"from_attributes": True}

    id: UUID
    project_id: UUID
    research_engine_project_id: UUID
    name: str
    template_source: Optional[str] = None
    version: int
    steps: List[BlueprintStepDefinition]
    parameters: Dict[str, Any]
    is_immutable: bool
    created_at: datetime
    updated_at: datetime


class BlueprintTemplateDetailResponse(BaseModel):
    """Validated full content of one server-owned blueprint template."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2000)
    template_source: str = Field(min_length=1, max_length=100)
    contract_version: int = Field(ge=1)
    parameters: Dict[str, Any]
    constraints: Dict[str, Any]
    coverage: Dict[str, Any]
    steps: List[BlueprintStepDefinition] = Field(
        min_length=1, max_length=MAX_BLUEPRINT_STEPS
    )


# ============================================================================
# Run Schemas
# ============================================================================


BoundedCriterion = Annotated[str, Field(min_length=1, max_length=500)]


class DailyBriefScopeConfirmation(BaseModel):
    """User-confirmed, bounded scope for a Daily Research Brief run."""

    model_config = ConfigDict(extra="forbid")

    research_question: str = Field(min_length=1, max_length=2000)
    inclusion_criteria: List[BoundedCriterion] = Field(min_length=1, max_length=25)
    exclusion_criteria: List[BoundedCriterion] = Field(
        default_factory=list, max_length=25
    )
    providers: List[str] = Field(min_length=1, max_length=4)
    limit_per_provider: int = Field(strict=True, ge=1, le=50)
    notes: str = Field(default="", max_length=2000)
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def confirmation_is_actual_true(cls, value: Any) -> Any:
        if type(value) is not bool or value is not True:
            raise ValueError("confirmed must be the boolean true")
        return value

    @field_validator("providers")
    @classmethod
    def providers_are_canonical_and_eligible(cls, value: List[str]) -> List[str]:
        from src.services.research_engine.connectors.registry import (
            normalize_connector_selection,
        )

        return list(normalize_connector_selection(value, daily_brief_only=True))


class RunCreate(BaseModel):
    """Schema for creating a research run."""

    protocol_version_id: Optional[UUID] = None
    parameters_override: Dict[str, Any] = Field(default_factory=dict)
    scope_confirmation: Optional[DailyBriefScopeConfirmation] = None

    @field_validator("parameters_override")
    @classmethod
    def parameters_are_bounded(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return _bounded_payload(value, "run parameters")


class RunResumeRequest(BaseModel):
    """Bounded authorization supplied only for exceptional run continuation."""

    model_config = ConfigDict(extra="forbid")

    continue_unverified: bool = False
    output_hash: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class ScreeningItemDecision(BaseModel):
    """A review decision for exactly one persisted screening source part."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(strict=True, min_length=1, max_length=MAX_REVIEW_ID_CHARS)
    part_id: str = Field(strict=True, min_length=1, max_length=MAX_REVIEW_ID_CHARS)
    decision: Literal["include", "exclude", "unresolved"]
    reason: Optional[str] = Field(
        default=None, strict=True, min_length=1, max_length=500
    )

    @model_validator(mode="after")
    def exclusion_has_a_reason(self) -> "ScreeningItemDecision":
        if self.decision == "exclude" and not (self.reason and self.reason.strip()):
            raise ValueError("exclude decisions require a reason")
        return self


class ExtractionItemDecision(BaseModel):
    """A review decision for exactly one persisted extraction source part."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(strict=True, min_length=1, max_length=MAX_REVIEW_ID_CHARS)
    part_id: str = Field(strict=True, min_length=1, max_length=MAX_REVIEW_ID_CHARS)
    decision: Literal["accept", "reject", "unresolved"]
    reason: Optional[str] = Field(
        default=None, strict=True, min_length=1, max_length=500
    )

    @model_validator(mode="after")
    def rejection_has_a_reason(self) -> "ExtractionItemDecision":
        if self.decision == "reject" and not (self.reason and self.reason.strip()):
            raise ValueError("reject decisions require a reason")
        return self


class ScreeningReviewDecisionPayload(BaseModel):
    """Exact-set screening decisions submitted for one stage output."""

    model_config = ConfigDict(extra="forbid")

    items: List[ScreeningItemDecision] = Field(
        min_length=1, max_length=MAX_REVIEW_ITEMS
    )


class ExtractionReviewDecisionPayload(BaseModel):
    """Exact-set extraction decisions submitted for one stage output."""

    model_config = ConfigDict(extra="forbid")

    items: List[ExtractionItemDecision] = Field(
        min_length=1, max_length=MAX_REVIEW_ITEMS
    )


class FinalReviewDecisionPayload(BaseModel):
    """Final review carries no client-authored report or evidence fields."""

    model_config = ConfigDict(extra="forbid")


ReviewDecisionPayload = (
    ScreeningReviewDecisionPayload
    | ExtractionReviewDecisionPayload
    | FinalReviewDecisionPayload
)


class StageReviewRequest(BaseModel):
    """Strict exact-hash decision for one persisted review gate."""

    model_config = ConfigDict(extra="forbid")

    review_kind: ReviewKind
    output_hash: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    decision: ReviewDecision
    decision_payload: ReviewDecisionPayload
    note: Optional[str] = Field(default=None, strict=True, max_length=2000)

    @model_validator(mode="before")
    @classmethod
    def select_payload_model(cls, value: Any) -> Any:
        """Parse the payload with the model selected by the sibling kind."""

        if not isinstance(value, dict) or "decision_payload" not in value:
            return value
        try:
            review_kind = ReviewKind(value.get("review_kind"))
        except (TypeError, ValueError):
            return value
        payload_model: type[BaseModel]
        if review_kind == ReviewKind.SCREENING:
            payload_model = ScreeningReviewDecisionPayload
        elif review_kind == ReviewKind.EXTRACTION:
            payload_model = ExtractionReviewDecisionPayload
        else:
            payload_model = FinalReviewDecisionPayload
        selected = dict(value)
        selected["decision_payload"] = payload_model.model_validate(
            value["decision_payload"]
        )
        return selected

    @model_validator(mode="after")
    def payload_matches_review_kind(self) -> "StageReviewRequest":
        expected: type[BaseModel]
        if self.review_kind == ReviewKind.SCREENING:
            expected = ScreeningReviewDecisionPayload
        elif self.review_kind == ReviewKind.EXTRACTION:
            expected = ExtractionReviewDecisionPayload
        else:
            expected = FinalReviewDecisionPayload
        if not isinstance(self.decision_payload, expected):
            raise ValueError("decision_payload does not match review_kind")
        return self


class ReviewDescriptor(BaseModel):
    """Content-free durable descriptor for the current review gate."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    step_index: int = Field(ge=0)
    stage_type: Literal["screen", "extract", "export"]
    review_kind: ReviewKind
    contract_version: int = Field(ge=1)
    output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["pending", "approved"] = "pending"
    created_at: Optional[datetime] = None
    review_id: Optional[UUID] = None


class StageReviewResponse(BaseModel):
    """The immutable accepted ledger row plus replay metadata."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    run_id: UUID
    step_index: int = Field(ge=0)
    stage_type: Literal["screen", "extract", "export"]
    review_kind: ReviewKind
    reviewer_id: UUID
    output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: ReviewDecision
    decision_payload: Dict[str, Any]
    note: Optional[str] = Field(default=None, max_length=2000)
    created_at: datetime
    replay: bool = False


class ReviewValidationVocabulary(BaseModel):
    """Bounded decision vocabulary used to render a pending gate."""

    model_config = ConfigDict(extra="forbid")

    item_decisions: List[str] = Field(default_factory=list, max_length=3)
    reason_required_for: List[str] = Field(default_factory=list, max_length=1)


class PendingReviewResponse(BaseModel):
    """Owned pending review state, optionally including bounded stage output."""

    model_config = ConfigDict(extra="forbid")

    pending: bool
    descriptor: Optional[ReviewDescriptor] = None
    stage_output: Optional[Dict[str, Any]] = None
    accepted_review: Optional[StageReviewResponse] = None
    validation: Optional[ReviewValidationVocabulary] = None

    @model_validator(mode="after")
    def pending_fields_are_consistent(self) -> "PendingReviewResponse":
        if self.pending and (self.descriptor is None or self.stage_output is None):
            raise ValueError("pending reviews require a descriptor and stage output")
        if not self.pending and any(
            value is not None
            for value in (
                self.descriptor,
                self.stage_output,
                self.accepted_review,
                self.validation,
            )
        ):
            raise ValueError("non-pending review responses cannot carry gate data")
        if self.stage_output is not None:
            projection = self.stage_output.get("review_projection")
            projected = (
                isinstance(projection, dict)
                and projection.get("projected") is True
                and projection.get("truncated") is True
                and projection.get("identity_complete") is True
            )
            _bounded_payload(
                self.stage_output,
                "pending stage output",
                (
                    MAX_PENDING_REVIEW_OUTPUT_BYTES
                    if projected
                    else MAX_NESTED_PAYLOAD_BYTES
                ),
            )
        return self


class RunResponse(BaseModel):
    """Schema for run API responses."""

    model_config = {"from_attributes": True}

    id: UUID
    blueprint_id: UUID
    project_id: UUID
    research_engine_project_id: UUID
    protocol_version_id: Optional[UUID] = None
    effective_plan_hash: Optional[str] = None
    conformance_status: str = "legacy_unbound"
    blueprint_version: int
    status: RunStatus
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    total_tokens: int = 0
    created_at: datetime
    updated_at: datetime
    pause_reason: Optional[
        Literal["user_paused", "review_required", "verification_failed"]
    ] = None
    review_kind: Optional[ReviewKind] = None
    step_index: Optional[int] = None
    output_hash: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def attach_content_free_pause_descriptor(cls, value: Any) -> Any:
        """Project a durable manifest gate without serializing its content."""

        from src.services.research_engine.run_lifecycle import (
            ResearchRunLifecycleService,
        )

        manifest = (
            value.get("reproducibility_manifest")
            if isinstance(value, dict)
            else getattr(value, "reproducibility_manifest", None)
        )
        status_value = (
            value.get("status")
            if isinstance(value, dict)
            else getattr(value, "status", None)
        )
        run_id = (
            value.get("id") if isinstance(value, dict) else getattr(value, "id", None)
        )
        projected: dict[str, Any] | None = None
        if not isinstance(value, dict):
            projected = {
                field: getattr(value, field)
                for field in (
                    "id",
                    "blueprint_id",
                    "project_id",
                    "research_engine_project_id",
                    "protocol_version_id",
                    "effective_plan_hash",
                    "conformance_status",
                    "blueprint_version",
                    "status",
                    "started_at",
                    "completed_at",
                    "total_tokens",
                    "created_at",
                    "updated_at",
                )
            }
        if manifest is None or run_id is None:
            return projected if projected is not None else value

        descriptor = ResearchRunLifecycleService.pause_descriptor(
            type(
                "RunDescriptorProjection",
                (),
                {
                    "id": run_id,
                    "status": (
                        status_value.value
                        if isinstance(status_value, Enum)
                        else status_value
                    ),
                    "reproducibility_manifest": manifest,
                },
            )()
        )
        if descriptor is None:
            return projected if projected is not None else value

        if isinstance(value, dict):
            projected = dict(value)
        assert projected is not None
        projected.update(descriptor.to_dict())
        return projected


# ============================================================================
# Step Schemas
# ============================================================================


class QualityMark(BaseModel):
    """Quality check result for a step."""

    check_type: str
    passed: bool
    details: Optional[str] = None


class StepResponse(BaseModel):
    """Schema for step API responses."""

    model_config = {"from_attributes": True}

    id: UUID
    run_id: UUID
    step_index: int
    step_type: StepType
    mode: ExecutionMode
    inputs_hash: Optional[str] = None
    outputs_hash: Optional[str] = None
    full_prompt: Optional[str] = None
    model_id: Optional[str] = None
    model_version: Optional[str] = None
    temperature: float
    seed: Optional[int] = None
    output: Optional[Dict[str, Any]] = None
    quality_marks: Optional[List[QualityMark]] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    token_count: int = 0


# ============================================================================
# Source Schemas
# ============================================================================


class SourceResponse(BaseModel):
    """Schema for source API responses."""

    model_config = {"from_attributes": True}

    id: UUID
    run_id: UUID
    connector_type: str
    external_id: Optional[str] = None
    title: str
    authors: Optional[List[str]] = None
    abstract: Optional[str] = None
    url: Optional[str] = None
    content_hash: Optional[str] = None


# ============================================================================
# Evidence Schemas
# ============================================================================


class EvidenceResponse(BaseModel):
    """Schema for evidence API responses."""

    model_config = {"from_attributes": True}

    id: UUID
    step_id: UUID
    source_id: UUID
    claim_text: str
    confidence: Optional[float] = None
    grounding_status: GroundingStatus
    page_reference: Optional[str] = None
