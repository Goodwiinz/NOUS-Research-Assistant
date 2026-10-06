"""Browser and grant shapes for explicitly shared project memories."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_SELECTED_MEMORIES = 25


class MemoryOption(BaseModel):
    id: UUID
    content: str
    source: str
    created_at: datetime


class ContextOptions(BaseModel):
    """What the owner can share with one connected device, and what is shared."""

    request_id: UUID
    project_id: UUID
    project_label: str
    memories: list[MemoryOption]
    selected_memory_ids: list[UUID]


class ContextSelectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    memory_ids: list[UUID] = Field(max_length=MAX_SELECTED_MEMORIES)
