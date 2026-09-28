"""The research connector must go through the shared arXiv rate gate.

arXiv asks for one request every three seconds from a single connection,
counted across every machine you control, and tightened enforcement in
Feb 2026 — 429s now arrive even for callers honouring that interval.

``ArXivIngestionService`` implements this with a Redis slot reservation shared
by all pods. This connector skipped it entirely: plain-HTTP GET straight to
``export.arxiv.org``, no reservation, no backoff, no ``Retry-After``, driven
from Celery. It is the one arXiv path that can get the whole platform
rate-limited on behalf of every other feature.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.unit

_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v5</id>
    <title>Attention Is All You Need</title>
    <summary>The dominant sequence transduction models…</summary>
    <author><name>Ashish Vaswani</name></author>
  </entry>
</feed>"""


class _Resp:
    def __init__(self, status_code: int = 200, text: str = _FEED) -> None:
        self.status_code = status_code
        self.text = text
        self.headers: dict[str, str] = {}
        self.extensions: dict[str, Any] = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://export.arxiv.org/api/query")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"unexpected status {self.status_code}",
                request=request,
                response=response,
            )


class _Client:
    """Captures the request so the URL scheme can be asserted."""

    calls: List[dict[str, Any]] = []
    responses: List[_Resp] = []

    def __init__(self, *_a: Any, **kw: Any) -> None:
        self.kw = kw

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    async def get(self, url: str, **kw: Any) -> _Resp:
        _Client.calls.append({"url": url, **kw})
        return _Client.responses.pop(0) if _Client.responses else _Resp()


@pytest.fixture(autouse=True)
def _reset() -> None:
    _Client.calls = []
    _Client.responses = []


async def test_search_reserves_a_rate_slot_before_calling_arxiv() -> None:
    from src.services.arxiv import arxiv_service
    from src.services.research_engine.connectors.arxiv_connector import ArxivConnector

    slot = AsyncMock(return_value=0.0)
    with (
        patch("httpx.AsyncClient", _Client),
        patch.object(arxiv_service, "_acquire_arxiv_rate_slot", slot),
    ):
        docs = await ArxivConnector().search("transformers", max_results=1)

    assert slot.await_count == 1, (
        "every arXiv call must reserve a slot on the shared cross-pod gate — "
        "this connector runs from Celery and can rate-limit the whole platform"
    )
    assert len(docs) == 1


async def test_uses_https_not_plain_http() -> None:
    from src.services.arxiv import arxiv_service
    from src.services.research_engine.connectors.arxiv_connector import ArxivConnector

    with (
        patch("httpx.AsyncClient", _Client),
        patch.object(
            arxiv_service,
            "_acquire_arxiv_rate_slot",
            AsyncMock(return_value=0.0),
        ),
    ):
        await ArxivConnector().search("transformers")

    assert _Client.calls[0]["url"].startswith("https://")


async def test_429_is_retried_after_reserving_another_slot() -> None:
    """A retry that skips the gate is the burst the gate exists to prevent."""
    from src.services.arxiv import arxiv_service
    from src.services.research_engine.connectors.arxiv_connector import ArxivConnector

    limited = _Resp(status_code=429, text="")
    limited.headers = {"Retry-After": "0"}
    _Client.responses = [limited, _Resp()]

    slot = AsyncMock(return_value=0.0)
    with (
        patch("httpx.AsyncClient", _Client),
        patch.object(arxiv_service, "_acquire_arxiv_rate_slot", slot),
    ):
        docs = await ArxivConnector().search("transformers")

    assert slot.await_count == 2, "the retry must reserve its own slot"
    assert len(_Client.calls) == 2
    assert len(docs) == 1


async def test_search_trace_records_request_response_and_rate_wait() -> None:
    from src.services.arxiv import arxiv_service
    from src.services.research_engine.connectors import arxiv_connector
    from src.services.research_engine.connectors.arxiv_connector import ArxivConnector
    from src.services.research_engine.connectors.base import SearchTrace

    checkpoints: list[dict[str, Any]] = []

    async def checkpoint(
        provider: str, execution_id: str, page: dict[str, Any]
    ) -> None:
        checkpoints.append(page)

    trace = SearchTrace(
        execution_id=str(uuid4()),
        provider="arxiv",
        requested_limit=2,
        on_page_update=checkpoint,
    )
    slot = AsyncMock(return_value=0.25)
    sleep = AsyncMock()
    response_text = _FEED.replace(
        "</feed>",
        '<opensearch:totalResults xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">2</opensearch:totalResults></feed>',
    )
    _Client.responses = [_Resp(text=response_text)]

    with (
        patch("httpx.AsyncClient", _Client),
        patch.object(arxiv_service, "_acquire_arxiv_rate_slot", slot),
        patch.object(arxiv_connector.asyncio, "sleep", sleep),
    ):
        docs = await ArxivConnector().search(
            "transformers", max_results=2, search_trace=trace
        )

    assert len(docs) == 1
    slot.assert_awaited_once()
    sleep.assert_awaited_once_with(0.25)
    assert checkpoints[0]["page_status"] == "requested"
    assert checkpoints[-1]["page_status"] == "completed"
    assert trace.pages[0]["response"]["has_more"] is True


async def test_http_failure_is_recorded_in_search_trace() -> None:
    from src.services.arxiv import arxiv_service
    from src.services.research_engine.connectors.arxiv_connector import ArxivConnector
    from src.services.research_engine.connectors.base import SearchTrace

    trace = SearchTrace(execution_id=str(uuid4()), provider="arxiv", requested_limit=1)
    _Client.responses = [_Resp(status_code=503, text="")]

    with (
        patch("httpx.AsyncClient", _Client),
        patch.object(
            arxiv_service,
            "_acquire_arxiv_rate_slot",
            AsyncMock(return_value=0.0),
        ),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await ArxivConnector().search("transformers", search_trace=trace)

    assert trace.pages[0]["page_status"] == "failed"
    assert trace.pages[0]["response"]["error_type"] == "HTTPStatusError"


async def test_http_failure_without_trace_still_raises() -> None:
    from src.services.arxiv import arxiv_service
    from src.services.research_engine.connectors.arxiv_connector import ArxivConnector

    _Client.responses = [_Resp(status_code=503, text="")]

    with (
        patch("httpx.AsyncClient", _Client),
        patch.object(
            arxiv_service,
            "_acquire_arxiv_rate_slot",
            AsyncMock(return_value=0.0),
        ),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await ArxivConnector().search("transformers")


def test_parser_is_defusedxml_not_stdlib() -> None:
    """Repo convention: never stdlib XML on network-fed input (XXE/billion laughs)."""
    # Resolve from this file, not the CWD: pytest runs from the repo root in
    # CI and from backend/ locally, and a relative path only works in one.
    src = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "services"
        / "research_engine"
        / "connectors"
        / "arxiv_connector.py"
    ).read_text()

    assert "from defusedxml import" in src
    assert "import xml.etree.ElementTree" not in src
