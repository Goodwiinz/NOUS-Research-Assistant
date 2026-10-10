"""Browser and grant shapes for explicitly shared project memories."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_SELECTED_MEMORIES = 25


class MemoryOption(BaseModel):
    id: UUID
    content: str
    source: str
    created_at: datetime


class SkillOption(BaseModel):
    version_id: UUID
    name: str
    description: str
    version: int
    content_hash: str


class SelectedSkillLoad(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill_name: str = Field(min_length=1, max_length=128)


class ContextOptions(BaseModel):
    """What the owner can share with one connected device, and what is shared."""

    request_id: UUID
    project_id: UUID
    project_label: str
    memories: list[MemoryOption]
    selected_memory_ids: list[UUID]
    skills: list[SkillOption] = Field(default_factory=list)
    selected_skill_version_ids: list[UUID] = Field(default_factory=list)
    skill_snapshot_status: Literal["none", "ready", "unavailable"] = "none"
    skill_snapshot_expires_at: datetime | None = None


class ContextSelectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    memory_ids: list[UUID] = Field(max_length=MAX_SELECTED_MEMORIES)
    # Omission preserves skills for existing memory-only clients; [] explicitly clears.
    skill_version_ids: list[UUID] | None = Field(default=None, max_length=32)
    refresh_skills: bool = False
