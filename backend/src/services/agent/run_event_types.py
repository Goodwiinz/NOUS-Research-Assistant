"""Typed registry for the public run-event vocabulary (Hermes runtime).

One source of truth for every event type the durable store may persist and
the run API may serve. LangGraph callback names never appear here — the
graph translator (later PR) maps them into this vocabulary, and the legacy
SSE adapter is generated from these types, never a second translation path.

``validate_payload`` is the single gate between raw producer dicts and
durable JSONB: it validates against the per-type model, redacts tool args,
caps delta text, and enforces the total byte budget. Payloads are bounded
and display-safe by construction — IDs, summaries, status, capped excerpts;
never full tool results, prompts, or secrets.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.services.agent._pii_redact import redact_pii, redact_tool_args

# Bounds. Delta text is capped per event (batching adjacent fragments is the
# writer's job); the byte cap protects the event table and SSE frames from
# any payload the models would otherwise admit (e.g. long lists).
MAX_DELTA_TEXT_CHARS = 4000
MAX_PAYLOAD_BYTES = 16 * 1024


class PayloadTooLargeError(ValueError):
    """Serialized payload exceeds MAX_PAYLOAD_BYTES."""


class RunEventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_STOPPING = "run.stopping"
    ASSISTANT_DELTA = "assistant.delta"
    RETRIEVAL_CONTEXT = "retrieval.context"
    PLAN_UPDATED = "plan.updated"
    REFLECTION_COMPLETED = "reflection.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_RESOLVED = "approval.resolved"
    USAGE_UPDATED = "usage.updated"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"


TERMINAL_RUN_EVENTS: frozenset[RunEventType] = frozenset(
    {
        RunEventType.RUN_COMPLETED,
        RunEventType.RUN_FAILED,
        RunEventType.RUN_CANCELLED,
    }
)


class _Payload(BaseModel):
    """Payloads forbid extras: nothing undeclared can leak into durable rows."""

    model_config = ConfigDict(extra="forbid")


class RunCreatedPayload(_Payload):
    pass


class RunStartedPayload(_Payload):
    pass


class RunStoppingPayload(_Payload):
    reason: str | None = Field(default=None, max_length=200)
    request_id: str | None = Field(default=None, max_length=128)


class AssistantDeltaPayload(_Payload):
    """Redact before capping — same discipline as ToolCompletedPayload below.

    R7-L6: model output is echoed user content (an email the user pasted comes
    straight back out), and these rows are durable + replayed on resume.
    """

    text: str

    @field_validator("text")
    @classmethod
    def _cap_text(cls, value: str) -> str:
        return redact_pii(value)[:MAX_DELTA_TEXT_CHARS]


class RetrievalContextItem(_Payload):
    document_id: str | None = None
    chunk_id: str | None = None
    title: str | None = Field(default=None, max_length=500)
    score: float | None = None
    source: str | None = Field(default=None, max_length=200)


class RetrievalContextPayload(_Payload):
    items: list[RetrievalContextItem] = Field(default_factory=list, max_length=50)


class PlanStep(_Payload):
    title: str = Field(max_length=500)
    status: str | None = Field(default=None, max_length=32)


class PlanUpdatedPayload(_Payload):
    steps: list[PlanStep] = Field(default_factory=list, max_length=50)


class ReflectionCompletedPayload(_Payload):
    summary: str = Field(max_length=2000)


class ToolStartedPayload(_Payload):
    tool_call_id: str = Field(max_length=200)
    name: str = Field(max_length=200)
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("args")
    @classmethod
    def _redact(cls, value: dict[str, Any]) -> dict[str, Any]:
        # redact_tool_args is Any-typed (it recurses over arbitrary JSON); a
        # dict in always yields a dict out.
        redacted: dict[str, Any] = redact_tool_args(value)
        return redacted


class ToolCompletedPayload(_Payload):
    """Redact preview and error text before applying their length caps."""

    tool_call_id: str = Field(max_length=200)
    name: str = Field(max_length=200)
    status: str = Field(max_length=32)
    result_preview: str | None = Field(default=None, max_length=2000)
    error: str | None = Field(default=None, max_length=1000)

    @field_validator("result_preview", mode="before")
    @classmethod
    def _redact_result_preview(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        return redact_pii(value)[:2000]

    @field_validator("error", mode="before")
    @classmethod
    def _redact_error(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        return redact_pii(value)[:1000]


class ApprovalRequiredPayload(_Payload):
    approval_id: str = Field(max_length=64)
    tool_call_id: str = Field(max_length=200)
    name: str = Field(max_length=200)
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("args")
    @classmethod
    def _redact(cls, value: dict[str, Any]) -> dict[str, Any]:
        # redact_tool_args is Any-typed (it recurses over arbitrary JSON); a
        # dict in always yields a dict out.
        redacted: dict[str, Any] = redact_tool_args(value)
        return redacted


class ApprovalResolvedPayload(_Payload):
    approval_id: str = Field(max_length=64)
    decision: str = Field(max_length=32)


class UsageUpdatedPayload(_Payload):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class RunCompletedPayload(_Payload):
    assistant_message_id: str | None = None
    usage: UsageUpdatedPayload | None = None


class RunFailedPayload(_Payload):
    """Structured failure: machine code + client-safe message. extra="forbid"
    is the guarantee that stack traces / internals cannot ride along."""

    code: str = Field(max_length=64)
    message: str = Field(max_length=1000)


class RunCancelledPayload(_Payload):
    reason: str | None = Field(default=None, max_length=200)
    request_id: str | None = Field(default=None, max_length=128)
    # Same linkage ``run.completed`` carries: a cancelled run still persists a
    # stopped partial assistant row, and without this field the durable run
    # cannot name it (payloads are extra="forbid", so the cancellation path
    # could not pass one even when it had it). None when nothing streamed
    # before the abort, i.e. when there is no partial row to point at.
    assistant_message_id: str | None = None


PAYLOAD_MODELS: dict[RunEventType, type[_Payload]] = {
    RunEventType.RUN_CREATED: RunCreatedPayload,
    RunEventType.RUN_STARTED: RunStartedPayload,
    RunEventType.RUN_STOPPING: RunStoppingPayload,
    RunEventType.ASSISTANT_DELTA: AssistantDeltaPayload,
    RunEventType.RETRIEVAL_CONTEXT: RetrievalContextPayload,
    RunEventType.PLAN_UPDATED: PlanUpdatedPayload,
    RunEventType.REFLECTION_COMPLETED: ReflectionCompletedPayload,
    RunEventType.TOOL_STARTED: ToolStartedPayload,
    RunEventType.TOOL_COMPLETED: ToolCompletedPayload,
    RunEventType.APPROVAL_REQUIRED: ApprovalRequiredPayload,
    RunEventType.APPROVAL_RESOLVED: ApprovalResolvedPayload,
    RunEventType.USAGE_UPDATED: UsageUpdatedPayload,
    RunEventType.RUN_COMPLETED: RunCompletedPayload,
    RunEventType.RUN_FAILED: RunFailedPayload,
    RunEventType.RUN_CANCELLED: RunCancelledPayload,
}


def validate_payload(
    event_type: RunEventType | str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Validate, redact, and bound a payload for durable persistence.

    Raises ``ValueError`` for an unknown type, pydantic ``ValidationError``
    for a malformed payload, and ``PayloadTooLargeError`` past the byte cap.
    Returns the canonical dict to store (None fields dropped).
    """
    typed = RunEventType(event_type)  # ValueError on unknown — never persist it
    model = PAYLOAD_MODELS[typed]
    validated: dict[str, Any] = model.model_validate(payload).model_dump(
        mode="json", exclude_none=True
    )
    size = len(json.dumps(validated, separators=(",", ":")))
    if size > MAX_PAYLOAD_BYTES:
        raise PayloadTooLargeError(
            f"{typed.value} payload is {size} bytes (cap {MAX_PAYLOAD_BYTES})"
        )
    return validated


__all__ = [
    "MAX_DELTA_TEXT_CHARS",
    "MAX_PAYLOAD_BYTES",
    "PAYLOAD_MODELS",
    "PayloadTooLargeError",
    "RunEventType",
    "TERMINAL_RUN_EVENTS",
    "validate_payload",
]
