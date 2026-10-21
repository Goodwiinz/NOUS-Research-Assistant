"""Observed history rows projected from canonical NOUS PDF conversations."""

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class _WireModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @field_serializer("selected_at", check_fields=False)
    def _selection_timestamp(self, value: datetime) -> str:
        # The shipped tree compares timestamp strings, not parsed dates.
        # Fixed precision is essential: "00Z" sorts after "00.000001Z".
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return (
            value.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )


class AlphaXivChatSummary(_WireModel):
    id: UUID
    title: str | None
    newest_message: datetime = Field(alias="newestMessage")


class AlphaXivHistoryMessage(_WireModel):
    id: UUID
    parent_message_id: UUID | None = Field(alias="parentMessageId")
    selected_at: datetime = Field(alias="selectedAt")
    type: Literal["input_text", "input_file", "output_text", "output_reasoning"]
    content: str
    kind: str | None = None
    # Native feedback is a 1–5 rating; no observed conversion to votes exists.
    feedback_type: None = Field(default=None, alias="feedbackType")


class AlphaXivSelectedMessage(_WireModel):
    id: UUID
    selected_at: datetime = Field(alias="selectedAt")


class AlphaXivSelection(_WireModel):
    """NOUS selection acknowledgement; the observed client ignores the body."""

    selections: list[AlphaXivSelectedMessage]


class AlphaXivContextWindowUsage(_WireModel):
    input_tokens: int = Field(alias="inputTokens", ge=0)
    context_window: int = Field(alias="contextWindow", gt=0)
