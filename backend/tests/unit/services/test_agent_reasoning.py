"""Reasoning preflight and real client serialization, without provider requests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_openai import AzureChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from src.core.config import get_settings
from src.models.document import DocumentType, ProcessingStatus
from src.models.user import User
from src.schemas.alphaxiv import AlphaXivChatRequest
from src.services.agent import _nodes_llm, graph, llm_factory
from src.services.agent.reasoning import validate_reasoning_override
from src.services.agent.runtime_snapshot import turn_reset_fields
from src.services.agent.schemas import AgentExecuteRequest
from src.services.agent.state import AgentState
from src.services.agent.subgraphs import data_agent, research_agent, writing_agent
from src.services.compat import alphaxiv


@pytest.fixture(autouse=True)
def configured_responses(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def no_http(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Provider HTTP is forbidden in reasoning unit tests")

    monkeypatch.setattr(httpx.Client, "send", no_http)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    settings = get_settings()
    for name, value in {
        "AZURE_OPENAI_CHAT_ENDPOINT": "https://example.cognitiveservices.azure.com/",
        "AZURE_OPENAI_CHAT_API_KEY": "test-only-key",
        "AZURE_OPENAI_CHAT_API_VERSION": "2025-04-01-preview",
        "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME": "gpt-5-mini",
        "AZURE_OPENAI_SYNTHESIS_DEPLOYMENT": "gpt-5.6-luna",
        "AGENT_USE_RESPONSES_API": True,
        "AGENT_MAIN_REASONING_EFFORT": "medium",
        "AGENT_LIGHTWEIGHT_SYNTHESIS": True,
    }.items():
        monkeypatch.setattr(settings, name, value)
    graph._LLM_CACHE.clear()
    llm_factory.reset_llm_caches()
    yield
    graph._LLM_CACHE.clear()
    llm_factory.reset_llm_caches()


@pytest.mark.parametrize("model", ["", "gpt-5-mini", "gpt-5.6-luna"])
@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_supported_subset_serializes_native_request(model: str, effort: str) -> None:
    body = AgentExecuteRequest.model_validate(
        {
            "messages": [{"role": "user", "content": "Explain the PDF"}],
            "document_only": True,
            "model": model,
            "reasoning_effort": effort,
        }
    )
    assert body.model_dump(mode="json")["reasoning_effort"] == effort


@pytest.mark.parametrize(
    "value", [True, False, "none", "minimal", "xhigh", "max", "", 1, {}]
)
def test_unsupported_native_values_are_not_coerced(value: Any) -> None:
    with pytest.raises(ValidationError):
        AgentExecuteRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "Explain"}],
                "document_only": True,
                "reasoning_effort": value,
            }
        )


@pytest.mark.parametrize(
    "changes,model,document_only,reason",
    [
        ({}, "gpt-5-mini", False, "document-only"),
        ({}, "model-router", True, "supported NOUS"),
        ({"AGENT_USE_RESPONSES_API": False}, "gpt-5-mini", True, "Responses API"),
        (
            {"AZURE_OPENAI_CHAT_API_VERSION": "2024-12-01-preview"},
            "gpt-5-mini",
            True,
            "2025-04-01",
        ),
        (
            {"AZURE_OPENAI_CHAT_API_VERSION": "unknown"},
            "gpt-5-mini",
            True,
            "Azure API version",
        ),
        (
            {"AZURE_OPENAI_CHAT_DEPLOYMENT_NAME": "model-router"},
            "",
            True,
            "supported NOUS",
        ),
    ],
)
def test_capability_preflight_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
    model: str,
    document_only: bool,
    reason: str,
) -> None:
    for name, value in changes.items():
        monkeypatch.setattr(get_settings(), name, value)
    with pytest.raises(ValueError, match=reason):
        validate_reasoning_override("high", model=model, document_only=document_only)
    assert (
        validate_reasoning_override(None, model=model, document_only=document_only)
        is None
    )


@pytest.mark.parametrize(
    "endpoint",
    ["https://example.cognitiveservices.azure.com/", "https://example.invalid/v1"],
)
def test_real_constructor_payload_and_cache_partition(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setattr(get_settings(), "AZURE_OPENAI_CHAT_ENDPOINT", endpoint)
    low = graph._build_llm("gpt-5-mini", reasoning_effort_override="low")
    high = graph._build_llm("gpt-5-mini", reasoning_effort_override="high")
    default = graph._build_llm("gpt-5-mini")
    for client, effort in [(low, "low"), (high, "high"), (default, "medium")]:
        payload = client._get_request_payload([HumanMessage(content="Explain")])
        assert payload["reasoning"] == {"effort": effort, "summary": "auto"}
        assert payload["model"] == "gpt-5-mini"
        assert "input" in payload and "messages" not in payload
    assert low is graph._build_llm("gpt-5-mini", reasoning_effort_override="low")
    assert low is not high and high is not default and default is not low
    assert get_settings().AGENT_MAIN_REASONING_EFFORT == "medium"

    monkeypatch.setattr(get_settings(), "AGENT_USE_RESPONSES_API", False)
    legacy = graph._build_llm("gpt-5-mini")
    assert legacy is not default
    assert "reasoning" not in legacy._get_request_payload(
        [HumanMessage(content="Explain")]
    )
    with pytest.raises(ValueError, match="Responses API"):
        graph._build_llm("gpt-5-mini", reasoning_effort_override="high")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "node",
    [
        _nodes_llm.llm_node,
        _nodes_llm.force_synthesis_node,
        research_agent.research_llm_node,
        research_agent.research_force_synthesis_node,
        writing_agent.writing_llm_node,
        writing_agent.writing_force_synthesis_node,
        data_agent.data_llm_node,
        data_agent.data_force_synthesis_node,
    ],
    ids=lambda node: node.__name__,
)
async def test_every_answer_path_preserves_real_provider_payload(
    monkeypatch: pytest.MonkeyPatch, node: Any
) -> None:
    """Only provider I/O is replaced; builders, binding and serialization run."""
    payloads: list[dict[str, Any]] = []

    async def provider_stub(self: Any, messages: Any, **kwargs: Any) -> ChatResult:
        kwargs.pop("run_manager", None)
        payloads.append(self._get_request_payload(messages, **kwargs))
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(content=[{"type": "text", "text": "PDF answer"}])
                )
            ]
        )

    async def stream_stub(
        self: Any, messages: Any, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        kwargs.pop("run_manager", None)
        payloads.append(self._get_request_payload(messages, **kwargs))
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=[{"type": "text", "text": "PDF answer"}])
        )

    monkeypatch.setattr(AzureChatOpenAI, "_agenerate", provider_stub)
    monkeypatch.setattr(AzureChatOpenAI, "_astream", stream_stub)
    state = cast(
        AgentState,
        {
            **turn_reset_fields(),
            "messages": [
                HumanMessage(content="Explain the PDF results"),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "read_document", "args": {}, "id": "read"}],
                ),
                ToolMessage(content="PDF results", tool_call_id="read"),
            ],
            "document_only": True,
            "reasoning_effort": "high",
            "model": "gpt-5-mini",
            "page_context": {"type": "document"},
            "intent": "research",
            "thread_id": str(uuid4()),
        },
    )
    result = await node(state, {"configurable": {}})
    assert len(payloads) == 1
    assert payloads[0]["model"] == "gpt-5-mini"
    assert payloads[0]["reasoning"] == {"effort": "high", "summary": "auto"}
    assert result["messages"][-1].content == "PDF answer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "thinking",
    [True, False, "none", "minimal", "xhigh", "max", {"secret": "must not echo"}],
)
async def test_compat_unsupported_thinking_rejected_before_access(
    monkeypatch: pytest.MonkeyPatch, thinking: Any
) -> None:
    access = AsyncMock()
    monkeypatch.setattr(alphaxiv, "get_accessible_document_or_none", access)
    body = AlphaXivChatRequest.model_validate(
        {"message": "Explain", "paperVersionId": uuid4(), "thinking": thinking}
    )
    with pytest.raises(alphaxiv.CompatibilityError) as caught:
        await alphaxiv.prepare_request(body, AsyncMock(), cast(User, SimpleNamespace()))
    assert caught.value.status_code == 422
    assert "must not echo" not in str(caught.value)
    access.assert_not_awaited()


@pytest.mark.asyncio
async def test_compat_maps_effort_into_the_native_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = uuid4()
    monkeypatch.setattr(
        alphaxiv,
        "get_accessible_document_or_none",
        AsyncMock(
            return_value=SimpleNamespace(
                document_type=DocumentType.PDF,
                processing_status=ProcessingStatus.COMPLETED,
            )
        ),
    )
    body = AlphaXivChatRequest.model_validate(
        {
            "message": "Explain",
            "paperVersionId": document_id,
            "thinking": "high",
            "model": "gpt-5-mini",
        }
    )
    native = await alphaxiv.prepare_request(
        body,
        AsyncMock(),
        cast(User, SimpleNamespace(id=uuid4(), organization_id=uuid4())),
    )
    assert native.reasoning_effort == "high"
    assert native.document_only is True


@pytest.mark.asyncio
@pytest.mark.parametrize("unsupported", ["full", "responses_off", "model-router"])
async def test_compat_capability_failure_precedes_access(
    monkeypatch: pytest.MonkeyPatch, unsupported: str
) -> None:
    access = AsyncMock()
    monkeypatch.setattr(alphaxiv, "get_accessible_document_or_none", access)
    if unsupported == "responses_off":
        monkeypatch.setattr(get_settings(), "AGENT_USE_RESPONSES_API", False)
    body = AlphaXivChatRequest.model_validate(
        {
            "message": "Explain",
            "paperVersionId": uuid4(),
            "thinking": "high",
            "model": "model-router" if unsupported == "model-router" else "gpt-5-mini",
            "webSearch": "full" if unsupported == "full" else "off",
        }
    )
    with pytest.raises(alphaxiv.CompatibilityError) as caught:
        await alphaxiv.prepare_request(body, AsyncMock(), cast(User, SimpleNamespace()))
    assert caught.value.status_code == 422
    access.assert_not_awaited()


def test_new_turn_resets_checkpoint_reasoning() -> None:
    builder = StateGraph(AgentState)
    builder.add_node("observe", lambda state: {})
    builder.add_edge(START, "observe")
    builder.add_edge("observe", END)
    compiled = builder.compile(checkpointer=MemorySaver())
    config = cast(RunnableConfig, {"configurable": {"thread_id": str(uuid4())}})
    compiled.invoke(
        {"reasoning_effort": "high", "messages": [HumanMessage(content="first")]},
        config,
    )
    assert compiled.get_state(config).values["reasoning_effort"] == "high"
    compiled.invoke(
        {**turn_reset_fields(), "messages": [HumanMessage(content="second")]}, config
    )
    assert compiled.get_state(config).values["reasoning_effort"] is None
