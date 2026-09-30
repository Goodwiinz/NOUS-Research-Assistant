"""Transport-neutral shapes for the scoped integration read gateway."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ToolInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)
    invocation_id: UUID


class ToolResult(BaseModel):
    content: list[dict[str, Any]]
    is_error: bool
    # Observed document/chunk/draft identities only; never invented citations.
    source_refs: list[dict[str, Any]]


class ToolDescriptorDTO(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
