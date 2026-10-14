"""Observed alphaXiv v2 request shape with explicit NOUS identity semantics.

This is a compatibility surface, not a claim to alphaXiv backend parity.
Hosted UUIDs and credentials are not accepted as NOUS document authority.
"""

from typing import Any, Literal, Optional, Tuple
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from src.schemas.alphaxiv_files import AlphaXivFileReference


class AlphaXivChatFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=32000)
    paper_version_id: UUID = Field(
        alias="paperVersionId", description="An authorized NOUS document UUID"
    )
    llm_chat_id: Optional[UUID] = Field(default=None, alias="llmChatId")
    parent_message_id: Optional[UUID] = Field(default=None, alias="parentMessageId")
    model: str = Field(
        default="",
        max_length=100,
        description=(
            "A supported NOUS model, or pro to use the configured NOUS default "
            "deployment. The pro alias does not imply alphaXiv model equivalence."
        ),
    )
    selection_page_range: Optional[Tuple[StrictInt, StrictInt]] = Field(
        default=None,
        alias="selectionPageRange",
        description="Inclusive zero-based PDF page indices",
    )
    files: list[AlphaXivFileReference] = Field(default_factory=list, max_length=9)
    thinking: Any = None
    web_search: Optional[str] = Field(default=None, alias="webSearch", max_length=32)
    filter_model: Optional[str] = Field(
        default=None,
        alias="filterModel",
        max_length=100,
        description=(
            "Omitted/null selects the native Cohere page ranker, as does cohere. "
            "An explicit selectionPageRange takes precedence and bypasses ranking. "
            "Native short-document and provider fallback policies still apply."
        ),
    )
    custom_filter: Optional[dict[str, Any]] = Field(default=None, alias="customFilter")
    assistant_variant: Optional[str] = Field(
        default=None, alias="assistantVariant", max_length=64
    )
    overview_id: Optional[str] = Field(default=None, alias="overviewId", max_length=100)

    @field_validator("message")
    @classmethod
    def nonempty_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value

    @field_validator("selection_page_range")
    @classmethod
    def ordered_page_range(
        cls, value: Optional[Tuple[int, int]]
    ) -> Optional[Tuple[int, int]]:
        if value is not None and (value[0] < 0 or value[1] < value[0]):
            raise ValueError("selectionPageRange must be nonnegative and ordered")
        return value


class AlphaXivChatRequest(AlphaXivChatFields):
    protocol: Literal[2] = 2


class AlphaXivLiveChatRequest(AlphaXivChatFields):
    """The observed v3 request omits the v2 protocol selector."""


class AlphaXivLiveAck(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    llm_chat_id: UUID = Field(alias="llmChatId")
    run_id: UUID = Field(alias="runId")


class AlphaXivStopRequest(AlphaXivLiveAck):
    pass
