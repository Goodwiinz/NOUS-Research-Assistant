"""Network-free routing, provider-boundary and cancellation regressions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import SecretStr

from src.core.config import settings
from src.services.agent import classifier
from src.services.agent import typesafe_classifier as typesafe
from src.services.agent._nodes_classify import _extract_prior_tool
from src.services.agent.agent_execution_service import _page_context_to_dict
from src.services.agent.schemas import PageContextRequest


@pytest.fixture
async def provider(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[dict[str, Any], list[httpx.Request]]]:
    monkeypatch.setattr(settings, "AGENT_INTENT_PROVIDER", "typesafe")
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", SecretStr("test-only-secret"))
    monkeypatch.setattr(settings, "TYPESAFE_MIN_CONFIDENCE", 0.8)
    monkeypatch.setattr(settings, "TYPESAFE_TIMEOUT_SECONDS", 0.1)
    reply: dict[str, Any] = {
        "status": 200,
        "payload": {
            "model": "jev-1.13.0",
            "answers": {
                "intent": {
                    "type": "choice",
                    "choice": "writing",
                    "confidence": 0.95,
                    "probabilities": {
                        "research": 0.01,
                        "writing": 0.97,
                        "general": 0.01,
                        "knowledge_graph": 0.0,
                        "unresolved": 0.01,
                    },
                }
            },
            "usage": {"input_tokens": 100, "output_tokens": 10},
        },
    }
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(reply["status"], json=reply["payload"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(typesafe, "_client", client)
        yield reply, requests


@pytest.mark.asyncio
async def test_accepted_typesafe_skips_azure(
    provider: tuple[dict[str, Any], list[httpx.Request]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    azure = AsyncMock()
    monkeypatch.setattr(classifier, "classify_intent_llm", azure)
    result = await classifier.classify_intent_with_fallback(
        "Export knowledge base references", {}
    )
    assert (result.intent, result.source) == ("writing", "typesafe")
    azure.assert_not_awaited()
    request = provider[1][0]
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == "Bearer test-only-secret"
    body = json.loads(request.content)
    assert body["questions"]["intent"]["criteria"] == classifier.routing_criteria()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        401,
        403,
        422,
        429,
        529,
        503,
        "invalid",
        "low",
        "unresolved",
        "missing_key",
        "uncalibrated",
    ],
)
async def test_provider_failure_uses_azure_once(
    provider: tuple[dict[str, Any], list[httpx.Request]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: int | str,
) -> None:
    reply, requests = provider
    answer = reply["payload"]["answers"]["intent"]
    if isinstance(failure, int):
        reply["status"] = failure
    elif failure == "invalid":
        answer["confidence"] = "PRIVATE_RESPONSE_MUST_NOT_BE_LOGGED"
    elif failure == "low":
        answer["confidence"] = 0.2
    elif failure == "unresolved":
        answer["choice"] = "unresolved"
        answer["probabilities"]["writing"], answer["probabilities"]["unresolved"] = (
            0.01,
            0.97,
        )
    elif failure == "missing_key":
        monkeypatch.setattr(settings, "TYPESAFE_API_KEY", None)
    else:
        monkeypatch.setattr(settings, "TYPESAFE_MIN_CONFIDENCE", None)
    azure = AsyncMock(
        return_value=classifier.ClassificationResult("research", 0.9, "fallback", "llm")
    )
    monkeypatch.setattr(classifier, "classify_intent_llm", azure)
    result = await classifier.classify_intent_with_fallback("Find papers", {})
    assert result.source == "llm"
    azure.assert_awaited_once()
    assert len(requests) == (0 if failure in ("missing_key", "uncalibrated") else 1)
    assert "PRIVATE_RESPONSE_MUST_NOT_BE_LOGGED" not in caplog.text
    assert "test-only-secret" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "noul"),
        ("choice", "not-a-route"),
        ("confidence", float("nan")),
        ("confidence", True),
        ("confidence", 1.1),
        ("probabilities", {"writing": 1.0}),
    ],
)
async def test_invalid_answers_are_rejected(
    provider: tuple[dict[str, Any], list[httpx.Request]],
    field: str,
    value: Any,
) -> None:
    payload = provider[0]["payload"]["answers"]["intent"].copy()
    payload[field] = value
    with pytest.raises(ValueError):
        typesafe.ChoiceAnswer.model_validate(payload)


@pytest.mark.asyncio
async def test_rounded_distribution_and_argmax(
    provider: tuple[dict[str, Any], list[httpx.Request]],
) -> None:
    answer = provider[0]["payload"]["answers"]["intent"]
    answer["probabilities"]["writing"] = 0.96  # rounded sum .99 is valid
    typesafe.ChoiceAnswer.model_validate(answer)
    answer["choice"] = "general"
    with pytest.raises(ValueError):
        typesafe.ChoiceAnswer.model_validate(answer)
    answer["choice"] = "writing"
    answer["probabilities"]["writing"] = 0.5
    with pytest.raises(ValueError):
        typesafe.ChoiceAnswer.model_validate(answer)


@pytest.mark.asyncio
async def test_shared_deadline_and_transport_fallback(
    provider: tuple[dict[str, Any], list[httpx.Request]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert typesafe._client is not None

    async def slow_post(*args: Any, **kwargs: Any) -> httpx.Response:
        await asyncio.sleep(1)
        raise AssertionError("must be cancelled")

    monkeypatch.setattr(typesafe._client, "post", slow_post)
    monkeypatch.setattr(settings, "TYPESAFE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(classifier, "_CLASSIFIER_LLM_TIMEOUT_SECONDS", 0.04)
    azure = AsyncMock(side_effect=slow_post)
    monkeypatch.setattr(classifier, "classify_intent_llm", azure)
    result = await asyncio.wait_for(
        classifier.classify_intent_with_fallback("find arxiv papers", {}), 0.2
    )
    assert result.source == "keyword"
    azure.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_does_not_invoke_azure(
    provider: tuple[dict[str, Any], list[httpx.Request]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert typesafe._client is not None
    entered = asyncio.Event()

    async def blocked_post(*args: Any, **kwargs: Any) -> httpx.Response:
        entered.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    monkeypatch.setattr(typesafe._client, "post", blocked_post)
    azure = AsyncMock()
    monkeypatch.setattr(classifier, "classify_intent_llm", azure)
    task = asyncio.create_task(
        classifier.classify_intent_with_fallback("Find papers", {})
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    azure.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_lifecycle_is_reusable(
    provider: tuple[dict[str, Any], list[httpx.Request]],
) -> None:
    old = typesafe._client
    typesafe.start_typesafe_client()
    assert typesafe._client is old
    await typesafe.close_typesafe_client()
    assert old is not None and old.is_closed
    assert typesafe._client is None
    await typesafe.close_typesafe_client()
    typesafe.start_typesafe_client()
    assert typesafe._client is not old
    await typesafe.close_typesafe_client()


def test_context_adapter_and_tool_batch_preserve_relevant_outcomes() -> None:
    page = _page_context_to_dict(
        PageContextRequest(
            type="documents",
            metadata={
                "paper_id": "private-id",
                "paper_title": "Selected paper",
                "secret": "excluded",
            },
        )
    )
    messages = [
        HumanMessage(content="Search, then export"),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "a", "name": "search_arxiv", "args": {}},
                {"id": "b", "name": "export_bibliography", "args": {}},
            ],
        ),
        ToolMessage(content='{"status":"success"}', tool_call_id="a"),
        ToolMessage(content="private tool body", tool_call_id="b", status="error"),
        AIMessage(content="Export failed."),
        HumanMessage(content="try again"),
    ]
    state = classifier.build_routing_state(
        "try again", page, "Export failed.", _extract_prior_tool(messages)
    )
    assert state["page"]["paper_selected"] is True
    assert state["page"]["paper_title"] == "Selected paper"
    assert state["prior_tools"] == [
        {"name": "search_arxiv", "status": "success"},
        {"name": "export_bibliography", "status": "error"},
    ]
    for private in ("private-id", "excluded", "private tool body"):
        assert private not in json.dumps(state)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,previous,context",
    [
        ("Make it shorter", "I saved the draft", {}),
        ("Who connects these?", "We selected two graph nodes", {}),
        ("hi", "Should I continue?", {}),
        ("Summarize it", "", {"metadata": {"paper_id": "p"}}),
    ],
)
async def test_short_contextual_queries_reach_semantics(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    previous: str,
    context: dict[str, Any],
) -> None:
    monkeypatch.setattr(settings, "AGENT_INTENT_PROVIDER", "azure")
    azure = AsyncMock(
        return_value=classifier.ClassificationResult("writing", 0.9, "context", "llm")
    )
    monkeypatch.setattr(classifier, "classify_intent_llm", azure)
    result = await classifier.classify_intent_with_fallback(query, context, previous)
    azure.assert_awaited_once_with(query, context, previous, None)
    assert result.source == "llm"


def test_rubric_tracks_subgraph_capabilities() -> None:
    criteria = classifier.routing_criteria()
    assert "create_project_note" in criteria["research"]
    assert "create_project" in criteria["writing"]
    assert "export_bibliography" in criteria["writing"]
    assert "export_bibliography" not in criteria["general"]
    assert "find_entity_paths" in criteria["knowledge_graph"]


@pytest.mark.asyncio
async def test_typesafe_threshold_does_not_reuse_azure_confidence(
    provider: tuple[dict[str, Any], list[httpx.Request]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "TYPESAFE_MIN_CONFIDENCE", 0.5)
    provider[0]["payload"]["answers"]["intent"]["confidence"] = 0.55
    azure = AsyncMock()
    monkeypatch.setattr(classifier, "classify_intent_llm", azure)
    result = await classifier.classify_intent_with_fallback("Export references", {})
    assert result.source == "typesafe"
    assert result.confidence == 0.55
    azure.assert_not_awaited()


@pytest.mark.asyncio
async def test_azure_mode_never_calls_typesafe(
    provider: tuple[dict[str, Any], list[httpx.Request]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_INTENT_PROVIDER", "azure")
    azure = AsyncMock(
        return_value=classifier.ClassificationResult("general", 0.9, "fixture", "llm")
    )
    monkeypatch.setattr(classifier, "classify_intent_llm", azure)
    await classifier.classify_intent_with_fallback("Explain transformers", {})
    azure.assert_awaited_once()
    assert not provider[1]


def test_replay_cases_have_valid_capability_contracts() -> None:
    from pathlib import Path

    from tests.eval.replay_intent_routing import load_cases

    cases = load_cases(Path(__file__).parents[2] / "eval" / "intent_routing_cases.json")
    assert len(cases) == 20
    assert sum(case.unsupported for case in cases) == 1
    revise = next(case for case in cases if case.id == "revise-draft")
    assert revise.required_tools == ["revise_draft"]
    assert "create_draft" not in revise.required_tools


def test_retry_batch_does_not_reuse_a_later_result() -> None:
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"id": "reused", "name": "export_bibliography", "args": {}}],
        ),
        HumanMessage(content="another turn"),
        ToolMessage(content='{"status":"success"}', tool_call_id="reused"),
    ]
    state = classifier.build_routing_state(
        "try again", {}, prior_tool=_extract_prior_tool(messages)
    )
    assert state["prior_tools"] == [
        {"name": "export_bibliography", "status": "missing"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "html", "answer_keys", "oversized"])
async def test_wire_errors_fall_back_without_logging_payloads(
    provider: tuple[dict[str, Any], list[httpx.Request]],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert typesafe._client is not None
    if failure == "transport":
        post = AsyncMock(side_effect=httpx.ConnectError("private diagnostic"))
    else:
        body = "private diagnostic"
        if failure == "answer_keys":
            payload = provider[0]["payload"]
            payload["answers"]["unexpected"] = payload["answers"]["intent"]
            body = json.dumps(payload)
        elif failure == "oversized":
            body = "x" * 65537
        post = AsyncMock(
            return_value=httpx.Response(
                200,
                content=body,
                request=httpx.Request("POST", "https://api.typesafe.ai/v1/systemone"),
            )
        )
    monkeypatch.setattr(typesafe._client, "post", post)
    azure = AsyncMock(
        return_value=classifier.ClassificationResult("writing", 0.9, "fixture", "llm")
    )
    monkeypatch.setattr(classifier, "classify_intent_llm", azure)
    result = await classifier.classify_intent_with_fallback("Export references", {})
    assert result.source == "llm"
    post.assert_awaited_once()
    azure.assert_awaited_once()
    assert "private diagnostic" not in caplog.text


def test_state_budgets_are_explicit() -> None:
    state = classifier.build_routing_state(
        "a" * 9000 + "preserve ending",
        {},
        "b" * 9000,
        {"calls": [{"name": f"tool{i}", "status": "error"} for i in range(10)]},
    )
    assert state["query_truncated"] is True
    assert len(state["query"]) < 8100
    assert state["query"].endswith("preserve ending")
    assert len(state["previous_turn"]) <= classifier._PROMPT_FIELD_MAX_CHARS + 3
    assert len(state["prior_tools"]) == 8
    assert state["tool_batch_truncated"] is True
