"""Pydantic v2 schemas for the research engine API."""

import json
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("blueprint step must be an object")
        params = step.get("params") or step.get("parameters") or {}
        _bounded_payload(params, "step parameters")
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
