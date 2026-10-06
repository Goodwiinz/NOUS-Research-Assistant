"""Versioned, harness-written chat handoff contracts (Plan 06 slice 3).

A handoff is structured data the harness writes, never an LLM summary. Writes
use optimistic concurrency: the caller names the version it read, a stale
parent gets the latest version back (409) and must merge and retry. Rejected
content is returned to the caller, never stored.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

HandoffLine = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
]
HandoffGoal = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]


class HandoffInvalid(Exception):
    """A result references a version outside the bound project."""


class HandoffResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_version_id: UUID
    summary: HandoffLine


class HandoffCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handoff_id: UUID
    # None only for the first handoff in a chat.
    expected_parent_version: int | None = Field(ge=1)
    goal: HandoffGoal
    decisions: list[HandoffLine] = Field(default_factory=list, max_length=50)
    remaining: list[HandoffLine] = Field(default_factory=list, max_length=50)
    results: list[HandoffResult] = Field(default_factory=list, max_length=50)
    harness_name: str = Field(min_length=1, max_length=64)
    harness_session_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("results")
    @classmethod
    def _distinct_versions(cls, results: list[HandoffResult]) -> list[HandoffResult]:
        ids = [result.artifact_version_id for result in results]
        if len(ids) != len(set(ids)):
            raise ValueError("results must name each artifact version once")
        return results


class HandoffDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    thread_id: UUID
    project_id: UUID
    version: int
    handoff_id: UUID
    goal: str
    decisions: list[str]
    remaining: list[str]
    results: list[HandoffResult]
    harness_name: str
    harness_session_id: str | None
    created_at: datetime


class HandoffConflictBody(BaseModel):
    """The single 409 envelope; ``latest`` is None when no version exists or
    the conflicting writer could not be re-read."""

    detail: str
    latest: HandoffDTO | None


class HandoffConflict(Exception):
    """Stale parent or a replayed handoff_id with a different body.

    ``latest`` is None only when a parent was named but none exists yet.
    """

    def __init__(self, latest: HandoffDTO | None) -> None:
        super().__init__("Handoff conflicts with the latest version")
        self.latest = latest
