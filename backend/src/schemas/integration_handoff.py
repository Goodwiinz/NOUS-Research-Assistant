"""Versioned, harness-written chat handoff contracts (Plan 06 slice 3).

A handoff is structured data the harness writes, never an LLM summary. Writes
use optimistic concurrency: the caller names the version it read, a stale
parent gets the latest version back (409) and must merge and retry. Rejected
content is returned to the caller, never stored.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

HandoffLine = Annotated[str, Field(min_length=1, max_length=500)]


class HandoffInvalid(Exception):
    """A result references a version outside the bound project."""


class HandoffResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_version_id: UUID
    summary: str = Field(min_length=1, max_length=500)


class HandoffCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handoff_id: UUID
    # None only for the first handoff in a chat.
    expected_parent_version: int | None = Field(ge=1)
    goal: str = Field(min_length=1, max_length=2000)
    decisions: list[HandoffLine] = Field(default_factory=list, max_length=50)
    remaining: list[HandoffLine] = Field(default_factory=list, max_length=50)
    results: list[HandoffResult] = Field(default_factory=list, max_length=50)
    harness_name: str = Field(min_length=1, max_length=64)
    harness_session_id: str | None = Field(default=None, min_length=1, max_length=128)


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


class HandoffConflict(Exception):
    """Stale parent or a replayed handoff_id with a different body.

    ``latest`` is None only when a parent was named but none exists yet.
    """

    def __init__(self, latest: HandoffDTO | None) -> None:
        super().__init__("Handoff conflicts with the latest version")
        self.latest = latest
