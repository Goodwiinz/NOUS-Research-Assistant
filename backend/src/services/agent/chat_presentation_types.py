"""Bounded, typed chat presentation facts for deterministic live replay.

Prompts remain in the canonical message table. Only the request identity and
display-safe output fragments enter the existing immutable run ledger.
"""

from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.services.agent._pii_redact import redact_pii
from src.services.agent.schemas import PageFilterResponse, PdfPageRange


class _Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatPresentationStarted(_Fact):
    presentation: Literal["alphaxiv-v3"] = "alphaxiv-v3"
    paper_id: UUID
    user_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(max_length=100)
    thinking: Literal["low", "medium", "high"] | None = None
    web_search: Literal["off", "full"]
    selection_page_range: PdfPageRange | None = None
    parent_message_id: UUID | None = None
    file_ids: list[UUID] = Field(default_factory=list, max_length=9)


class _TextFrame(_Fact):
    kind: Literal["token", "reasoning_delta", "status"]
    text: str = Field(max_length=1000)

    @field_validator("text", mode="before")
    @classmethod
    def redact_text(cls, value: Any) -> Any:
        return redact_pii(value) if isinstance(value, str) else value


class _ContextReset(_Fact):
    kind: Literal["context_reset"]


class _ContextItem(_Fact):
    kind: Literal["context_item"]
    source_position: int = Field(strict=True, ge=1)
    document_id: UUID
    page_number: int = Field(strict=True, ge=1)
    title: str = Field(max_length=500)

    @field_validator("title", mode="before")
    @classmethod
    def redact_title(cls, value: Any) -> Any:
        return redact_pii(value) if isinstance(value, str) else value


class _ContextComplete(_Fact):
    kind: Literal["context_complete"]
    page_filter: PageFilterResponse | None = None


class _Usage(_Fact):
    kind: Literal["usage"]
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class _Approval(_Fact):
    kind: Literal["confirmation", "approval_required"]


class ChatPresentationFrame(_Fact):
    frame: Annotated[
        Union[
            _TextFrame,
            _ContextReset,
            _ContextItem,
            _ContextComplete,
            _Usage,
            _Approval,
        ],
        Field(discriminator="kind"),
    ]
