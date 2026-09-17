"""Pydantic v2 schemas for the research engine API."""

import json
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# These limits are deliberately server-owned.  They protect both newly
# validated blueprints and the legacy JSONB rows that are revalidated by the
# execution path before a paid call is made.
MAX_BLUEPRINT_STEPS = 32
MAX_NESTED_PAYLOAD_BYTES = 32 * 1024
MAX_PROMPT_TEMPLATE_CHARS = 16 * 1024


def _serialized_size(value: Any) -> int:
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


def _bounded_payload(value: Any, field_name: str) -> Any:
    if _serialized_size(value) > MAX_NESTED_PAYLOAD_BYTES:
        raise ValueError(
            f"{field_name} exceeds the {MAX_NESTED_PAYLOAD_BYTES}-byte limit"
        )
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


# ============================================================================
# Project Schemas
# ============================================================================


class ProjectCreate(BaseModel):
    """Schema for creating a research project."""

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    settings: Dict[str, Any] = Field(default_factory=dict)


class ProjectUpdate(BaseModel):
    """Schema for updating a research project."""

    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None


class ProjectResponse(BaseModel):
    """Schema for project API responses."""

    model_config = {"from_attributes": True}

    id: UUID
    name: str
    description: Optional[str] = None
    status: str
    settings: Dict[str, Any]
    created_at: datetime
    updated_at: datetime


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
        ..., min_length=1, max_length=MAX_BLUEPRINT_STEPS
    )
    parameters: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("steps")
    @classmethod
    def steps_not_empty(
        cls, v: List[BlueprintStepDefinition]
    ) -> List[BlueprintStepDefinition]:
        if len(v) == 0:
            raise ValueError("steps must not be empty")
        return v

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
    name: str
    template_source: Optional[str] = None
    version: int
    steps: List[BlueprintStepDefinition]
    parameters: Dict[str, Any]
    is_immutable: bool
    created_at: datetime
    updated_at: datetime


# ============================================================================
# Run Schemas
# ============================================================================


class RunCreate(BaseModel):
    """Schema for creating a research run."""

    parameters_override: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("parameters_override")
    @classmethod
    def parameters_are_bounded(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return _bounded_payload(value, "run parameters")


class RunResponse(BaseModel):
    """Schema for run API responses."""

    model_config = {"from_attributes": True}

    id: UUID
    blueprint_id: UUID
    blueprint_version: int
    status: RunStatus
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    total_tokens: int = 0
    created_at: datetime
    updated_at: datetime


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
