import asyncio
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.core.config import settings
from src.services.search.cohere_rerank_service import CohereRerankService


async def test_last_failure_is_isolated_between_concurrent_invocations() -> None:
    service = CohereRerankService()
    release = asyncio.Event()
    both_set = asyncio.Barrier(2)

    async def observe(reason: str):
        service.last_failure = {"reason": reason}
        await both_set.wait()
        if reason == "first":
            release.set()
        await release.wait()
        return service.last_failure

    first, second = await asyncio.gather(observe("first"), observe("second"))

    assert first == {"reason": "first"}
    assert second == {"reason": "second"}


@patch("src.services.search.cohere_rerank_service.httpx.Client")
def test_rerank_sync_sets_404_failure_reason_and_falls_back(
    mock_http_client: MagicMock,
) -> None:
    service = CohereRerankService()
    service._enabled = True
    service.endpoint = "https://example.invalid/rerank"
    service.api_key = "test-key"
    service.model = "rerank-v3.5"

    request = httpx.Request("POST", service.endpoint)
    response = httpx.Response(404, request=request, text='{"error":"not found"}')
    http_error = httpx.HTTPStatusError("Not Found", request=request, response=response)
    response_mock = MagicMock()
    response_mock.raise_for_status.side_effect = http_error
    response_mock.status_code = 404
    response_mock.text = '{"error":"not found"}'

    mock_http_client.return_value.post.return_value = response_mock

    results = service.rerank_sync(
        query="test query",
        documents=[{"id": "doc-1", "content": "content", "relevance_score": 0.8}],
        top_n=1,
    )

    assert len(results) == 1
    assert service.last_failure["reason"] == "endpoint_not_found"
    assert service.last_failure["status_code"] == 404


@patch("src.services.search.cohere_rerank_service.time.sleep")
@patch("src.services.search.cohere_rerank_service.httpx.Client")
def test_rerank_sync_reuses_client_and_honors_429_retry_after(
    mock_http_client: MagicMock, mock_sleep: MagicMock
) -> None:
    service = CohereRerankService()
    service._enabled = True
    service.endpoint = "https://example.invalid/rerank"
    service.api_key = "test-key"

    request = httpx.Request("POST", service.endpoint)
    limited = httpx.Response(429, request=request, headers={"Retry-After": "2"})
    success = httpx.Response(
        200,
        request=request,
        json={"results": [{"index": 0, "relevance_score": 0.9}]},
    )
    mock_http_client.return_value.post.side_effect = [limited, success, success]

    documents = [{"id": "doc-1", "content": "content", "score": 0.5}]
    assert service.rerank_sync("query", documents)[0].relevance_score == 0.9
    assert service.rerank_sync("query", documents)[0].relevance_score == 0.9

    mock_sleep.assert_called_once_with(2.0)
    mock_http_client.assert_called_once_with(timeout=30.0)


ROUTING_CASES = [
    (
        "https://tenant.services.ai.azure.com/openai/v1/",
        "https://tenant.services.ai.azure.com/providers/cohere/v2/rerank",
    ),
    (
        "https://tenant.cognitiveservices.azure.com/openai/v1",
        "https://tenant.cognitiveservices.azure.com/providers/cohere/v2/rerank",
    ),
    (
        "https://tenant.openai.azure.com/openai/v1/?api-version=example",
        "https://tenant.openai.azure.com/providers/cohere/v2/rerank?api-version=example",
    ),
    (
        "https://tenant.services.ai.azure.com/providers/cohere/v2/rerank",
        "https://tenant.services.ai.azure.com/providers/cohere/v2/rerank",
    ),
    ("https://api.cohere.com/v2/rerank", "https://api.cohere.com/v2/rerank"),
    (
        "https://custom.example.test/openai/v1/",
        "https://custom.example.test/openai/v1/",
    ),
    (
        "https://tenant.services.ai.azure.com.evil.example/openai/v1/",
        "https://tenant.services.ai.azure.com.evil.example/openai/v1/",
    ),
]


def _routing_service(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> CohereRerankService:
    monkeypatch.setattr(settings, "COHERE_RERANK_ENDPOINT", endpoint)
    monkeypatch.setattr(settings, "COHERE_RERANK_API_KEY", "routing-test")
    monkeypatch.setattr(
        "src.services.search.cohere_rerank_service.get_circuit_breaker",
        lambda _name: None,
    )
    return CohereRerankService()


def _routing_response(request: httpx.Request, expected: str) -> httpx.Response:
    assert request.headers["Authorization"] == "Bearer routing-test"
    if str(request.url) != expected:
        return httpx.Response(404, json={"error": {"code": "404"}})
    return httpx.Response(
        200, json={"results": [{"index": 1, "relevance_score": 0.99}]}
    )


@pytest.mark.parametrize(("endpoint", "expected"), ROUTING_CASES)
async def test_async_rerank_routes_azure_base_and_preserves_full_urls(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, expected: str
) -> None:
    service = _routing_service(monkeypatch, endpoint)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: _routing_response(request, expected)
        )
    ) as client:
        service._async_client = client
        outcome = await service.rerank_with_outcome(
            "query",
            [
                {"id": "fallback", "content": "unrelated", "score": 0.9},
                {"id": "provider", "content": "relevant", "score": 0.1},
            ],
            top_n=1,
        )
    assert outcome.succeeded, outcome.failure
    assert [row.document_id for row in outcome.results] == ["provider"]


@pytest.mark.parametrize(("endpoint", "expected"), ROUTING_CASES)
def test_sync_rerank_routes_azure_base_and_preserves_full_urls(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, expected: str
) -> None:
    service = _routing_service(monkeypatch, endpoint)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: _routing_response(request, expected)
        )
    ) as client:
        service._sync_client = client
        rows = service.rerank_sync(
            "query",
            [
                {"id": "fallback", "content": "unrelated", "score": 0.9},
                {"id": "provider", "content": "relevant", "score": 0.1},
            ],
            top_n=1,
        )
    assert service.last_failure is None
    assert [row.document_id for row in rows] == ["provider"]
