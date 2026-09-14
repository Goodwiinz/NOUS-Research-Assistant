"""Unit tests for DOKnowledgeBaseClient.

Mocks httpx.AsyncClient.request to avoid network. Verifies retry on 429,
auth failure handling, and successful retrieve parsing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import Settings
from src.services.do_kb.client import DOKnowledgeBaseClient, DOKnowledgeBaseError


def _make_settings(**overrides: Any) -> Settings:
    base = dict(
        DO_KB_ENABLED=True,
        DO_API_TOKEN="test-token",
        DO_KB_REGION="tor1",
        DO_KB_PROJECT_ID="test-project",
        DO_KB_EMBEDDING_MODEL_UUID="model-uuid",
        DO_KB_REQUEST_TIMEOUT_SECONDS=1.0,
    )
    base.update(overrides)
    return Settings(**base)


def _mock_response(status: int, json_body: dict[str, Any] | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.json.return_value = json_body or {}
    response.text = "" if json_body is None else str(json_body)
    response.content = b"{}" if json_body is not None else b""
    return response


def _sent_method_url_body(request_mock: AsyncMock) -> tuple[str, str, dict[str, Any]]:
    call_args = request_mock.call_args
    method = call_args.args[0] if call_args.args else call_args[0][0]
    url = call_args.args[1] if len(call_args.args) > 1 else call_args[0][1]
    body = call_args.kwargs.get("json") or call_args[1].get("json") or {}
    return method, url, body


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pooled_client_reused_across_requests():
    """Audit A3: the AsyncClient is created once and reused, not rebuilt per call."""
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)

    payload = {"job": {"uuid": "j", "status": "PENDING"}}
    with patch("httpx.AsyncClient") as mock_async_client:
        mock_async_client.return_value.request = AsyncMock(
            return_value=_mock_response(200, payload)
        )
        await client.start_indexing(kb_uuid="kb")
        await client.start_indexing(kb_uuid="kb")

    # Two requests, one client constructed.
    assert mock_async_client.call_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_kb_happy_path():
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)

    payload = {
        "knowledge_base": {
            "uuid": "kb-123",
            "name": "nous-org-abc",
            "region": "tor1",
            "project_id": "test-project",
            "embedding_model_uuid": "model-uuid",
            "is_public": False,
        }
    }

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(return_value=_mock_response(200, payload))
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        kb = await client.create_kb(
            name="nous-org-abc",
            region="tor1",
            project_id="test-project",
            embedding_model_uuid="model-uuid",
        )

    assert kb.uuid == "kb-123"
    assert kb.name == "nous-org-abc"
    request_mock.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_spaces_data_source_uses_current_do_api_route():
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)
    payload = {
        "knowledge_base_data_source": {
            "uuid": "ds-123",
            "spaces_data_source": {
                "bucket_name": "bucket",
                "item_path": "documents/org/doc.txt",
                "region": "tor1",
            },
        }
    }

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(return_value=_mock_response(200, payload))
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        data_source = await client.add_spaces_data_source(
            kb_uuid="kb-123",
            bucket="bucket",
            key="documents/org/doc.txt",
        )

    assert data_source.uuid == "ds-123"
    method, url, body = _sent_method_url_body(request_mock)
    assert method == "POST"
    assert url.endswith("/v2/gen-ai/knowledge_bases/kb-123/data_sources")
    assert "data-sources" not in url
    assert body["knowledge_base_uuid"] == "kb-123"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_data_sources_uses_current_do_api_route():
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)
    payload = {"knowledge_base_data_sources": [{"uuid": "ds-123"}]}

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(return_value=_mock_response(200, payload))
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        data_sources = await client.list_data_sources(kb_uuid="kb-123")

    assert data_sources == [{"uuid": "ds-123"}]
    method, url, _ = _sent_method_url_body(request_mock)
    assert method == "GET"
    assert url.endswith("/v2/gen-ai/knowledge_bases/kb-123/data_sources")
    assert "data-sources" not in url


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retrieve_parses_chunks():
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)

    # Mirror DO live wire format: `results[]` with `text_content` field
    # plus `total_results` at the top.
    payload = {
        "results": [
            {
                "text_content": "first",
                "score": 0.9,
                "metadata": {"item_name": "doc-1.pdf"},
            },
            {
                "text_content": "second",
                "score": 0.7,
                "metadata": {"item_name": "doc-2.pdf"},
            },
        ],
        "total_results": 2,
    }

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(return_value=_mock_response(200, payload))
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        result = await client.retrieve(kb_uuid="kb-123", query="hello", top_k=2)

    assert result.total == 2
    assert result.chunks[0].text == "first"
    assert result.chunks[0].document_id == "doc-1.pdf"
    assert result.chunks[1].score == 0.7


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retrieve_synthesizes_score_when_absent():
    """DO retrieve Public Preview omits relevance scores. Verify the client
    falls back to a rank-position proxy (1.0 at rank 0, decay 0.05/step,
    floor 0.1) so downstream code can still rank/dedupe/threshold.
    """
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)

    payload = {
        "results": [
            {"text_content": "a", "metadata": {"item_name": "doc-a.pdf"}},
            {"text_content": "b", "metadata": {"item_name": "doc-b.pdf"}},
            {"text_content": "c", "metadata": {"item_name": "doc-c.pdf"}},
        ],
        "total_results": 3,
    }

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(return_value=_mock_response(200, payload))
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        result = await client.retrieve(kb_uuid="kb-123", query="x", top_k=3)

    scores = [c.score for c in result.chunks]
    assert scores[0] > scores[1] > scores[2]
    assert scores[0] == pytest.approx(1.0)
    assert scores[1] == pytest.approx(0.95)
    assert scores[2] == pytest.approx(0.90)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retrieve_uses_config_alpha():
    """When caller omits alpha, client sends DO_KB_RETRIEVE_ALPHA from config."""
    cfg = _make_settings(DO_KB_RETRIEVE_ALPHA=0.3)
    client = DOKnowledgeBaseClient(cfg=cfg)

    payload = {"results": [], "total_results": 0}

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(return_value=_mock_response(200, payload))
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        await client.retrieve(kb_uuid="kb-123", query="test")

    sent_body = request_mock.call_args.kwargs.get("json") or request_mock.call_args[
        1
    ].get("json")
    assert sent_body["alpha"] == 0.3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retrieve_explicit_alpha_overrides_config():
    """Caller-supplied alpha takes precedence over config default."""
    cfg = _make_settings(DO_KB_RETRIEVE_ALPHA=0.3)
    client = DOKnowledgeBaseClient(cfg=cfg)

    payload = {"results": [], "total_results": 0}

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(return_value=_mock_response(200, payload))
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        await client.retrieve(kb_uuid="kb-123", query="test", alpha=0.8)

    sent_body = request_mock.call_args.kwargs.get("json") or request_mock.call_args[
        1
    ].get("json")
    assert sent_body["alpha"] == 0.8


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retrieve_no_alpha_when_config_none():
    """When config alpha is None and caller omits it, no alpha in request body."""
    cfg = _make_settings(DO_KB_RETRIEVE_ALPHA=None)
    client = DOKnowledgeBaseClient(cfg=cfg)

    payload = {"results": [], "total_results": 0}

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(return_value=_mock_response(200, payload))
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        await client.retrieve(kb_uuid="kb-123", query="test")

    sent_body = request_mock.call_args.kwargs.get("json") or request_mock.call_args[
        1
    ].get("json")
    assert "alpha" not in sent_body


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_on_429_then_success(monkeypatch):
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    success_payload = {"job": {"uuid": "job-1", "status": "PENDING"}}
    responses = [
        _mock_response(429),
        _mock_response(200, success_payload),
    ]

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(side_effect=responses)
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        job = await client.start_indexing(kb_uuid="kb-123")

    assert job.uuid == "job-1"
    assert request_mock.await_count == 2
    assert sleeps  # backoff slept at least once


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_exhaustion_does_not_sleep_after_final_attempt(monkeypatch):
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    with patch("httpx.AsyncClient") as mock_async_client:
        responses = [_mock_response(503), _mock_response(503), _mock_response(503)]
        for response in responses:
            response.headers = {}
        request_mock = AsyncMock(side_effect=responses)
        mock_async_client.return_value.request = request_mock

        with pytest.raises(DOKnowledgeBaseError) as exc_info:
            await client._request("GET", "https://example.test/retry")

    assert exc_info.value.status_code == 503
    assert request_mock.await_count == 3
    assert sleeps == [1, 2]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_json_maps_to_do_kb_error():
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)
    response = _mock_response(200, {"ok": True})
    response.json.side_effect = ValueError("malformed")

    with patch("httpx.AsyncClient") as mock_async_client:
        mock_async_client.return_value.request = AsyncMock(return_value=response)
        with pytest.raises(DOKnowledgeBaseError) as exc_info:
            await client._request("GET", "https://example.test/invalid")

    assert exc_info.value.status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_indexing_uses_current_do_api_route():
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)
    payload = {"job": {"uuid": "job-1", "status": "PENDING"}}

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(return_value=_mock_response(200, payload))
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        job = await client.start_indexing(kb_uuid="kb-123")

    assert job.uuid == "job-1"
    method, url, body = _sent_method_url_body(request_mock)
    assert method == "POST"
    assert url.endswith("/v2/gen-ai/indexing_jobs")
    assert "indexing-jobs" not in url
    assert body["knowledge_base_uuid"] == "kb-123"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_indexing_job_uses_current_do_api_route():
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)
    payload = {"job": {"uuid": "job-1", "status": "PENDING"}}

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(return_value=_mock_response(200, payload))
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        job = await client.get_indexing_job(kb_uuid="kb-123", job_uuid="job-1")

    assert job.uuid == "job-1"
    method, url, _ = _sent_method_url_body(request_mock)
    assert method == "GET"
    assert url.endswith("/v2/gen-ai/indexing_jobs/job-1")
    assert "indexing-jobs" not in url


@pytest.mark.unit
@pytest.mark.asyncio
async def test_auth_failure_raises():
    cfg = _make_settings(DO_API_TOKEN=None, DO_KB_ENABLED=False)
    client = DOKnowledgeBaseClient(cfg=cfg)

    with pytest.raises(DOKnowledgeBaseError):
        await client.create_kb(
            name="x",
            region="tor1",
            project_id="p",
            embedding_model_uuid="m",
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retrieve_uses_config_default_top_k():
    cfg = _make_settings(DO_KB_DEFAULT_TOP_K=12)
    client = DOKnowledgeBaseClient(cfg=cfg)
    payload = {"results": [], "total_results": 0}
    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(return_value=_mock_response(200, payload))
        ctx = mock_async_client.return_value
        ctx.request = request_mock
        await client.retrieve(kb_uuid="kb-123", query="test")
    sent_body = request_mock.call_args.kwargs.get("json") or request_mock.call_args[
        1
    ].get("json")
    assert sent_body["num_results"] == 12


@pytest.mark.unit
@pytest.mark.asyncio
async def test_4xx_non_retryable_raises(monkeypatch):
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(return_value=_mock_response(404))
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        with pytest.raises(DOKnowledgeBaseError) as exc_info:
            await client.get_indexing_job(kb_uuid="kb", job_uuid="job")

    assert exc_info.value.status_code == 404
    assert request_mock.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retrieve_body_is_strict_query_num_results_alpha_only():
    """DO KBaaS now rejects any unrecognized retrieve-body field with 400.
    The body must carry only query, num_results, and (optionally) alpha —
    never the retired ``reranking`` / ``search_type`` fields."""
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)

    payload = {"results": [], "total_results": 0}

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(return_value=_mock_response(200, payload))
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        await client.retrieve(kb_uuid="kb-123", query="test")

    sent_body = request_mock.call_args.kwargs.get("json") or request_mock.call_args[
        1
    ].get("json")
    assert sent_body["query"] == "test"
    assert sent_body["num_results"] >= 1
    assert "reranking" not in sent_body
    assert "search_type" not in sent_body
    assert "filters" not in sent_body
    assert set(sent_body) <= {"query", "num_results", "alpha"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retrieve_adds_exact_named_document_filter_only_when_requested():
    cfg = _make_settings(DO_KB_RETRIEVE_ALPHA=None)
    client = DOKnowledgeBaseClient(cfg=cfg)
    filters = {
        "equals": {
            "key": "item_name",
            "value": "11111111-1111-4111-8111-111111111111.txt",
        }
    }

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(
            return_value=_mock_response(200, {"results": [], "total_results": 0})
        )
        mock_async_client.return_value.request = request_mock

        await client.retrieve(
            kb_uuid="kb-123", query="attention", top_k=8, filters=filters
        )

    _, _, sent_body = _sent_method_url_body(request_mock)
    assert sent_body == {"query": "attention", "num_results": 8, "filters": filters}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_429_raises_with_real_status(monkeypatch):
    """An all-retryable run must surface the actual HTTP status, not the old
    'exhausted retries: None' (audit A4)."""
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(return_value=_mock_response(429))
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        with pytest.raises(DOKnowledgeBaseError) as exc_info:
            await client.start_indexing(kb_uuid="kb")

    assert exc_info.value.status_code == 429
    assert "429" in str(exc_info.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_honors_retry_after_header(monkeypatch):
    """A 429 with Retry-After: 5 should sleep 5s, not the 2**attempt backoff (A5)."""
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    resp_429 = _mock_response(429)
    resp_429.headers = {"Retry-After": "5"}
    resp_ok = _mock_response(200, {"job": {"uuid": "job-1", "status": "PENDING"}})

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(side_effect=[resp_429, resp_ok])
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        job = await client.start_indexing(kb_uuid="kb")

    assert job.uuid == "job-1"
    assert 5 in sleeps


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_data_source_sends_delete_request():
    """delete_data_source sends DELETE to the correct data-source endpoint."""
    cfg = _make_settings()
    client = DOKnowledgeBaseClient(cfg=cfg)

    with patch("httpx.AsyncClient") as mock_async_client:
        request_mock = AsyncMock(return_value=_mock_response(204))
        ctx = mock_async_client.return_value
        ctx.request = request_mock

        await client.delete_data_source(kb_uuid="kb-123", ds_uuid="ds-456")

    request_mock.assert_awaited_once()
    method, url, _ = _sent_method_url_body(request_mock)
    assert method == "DELETE"
    assert "kb-123" in url
    assert url.endswith("/v2/gen-ai/knowledge_bases/kb-123/data_sources/ds-456")
    assert "data-sources" not in url
    assert "ds-456" in url
