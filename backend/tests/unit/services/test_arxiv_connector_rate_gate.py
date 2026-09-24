"""The research connector must go through the shared arXiv rate gate.

arXiv asks for one request every three seconds from a single connection,
counted across every machine you control, and tightened enforcement in
Feb 2026 — 429s now arrive even for callers honouring that interval.

The shared request implementation now owns an exclusive Redis lease, pacing,
and response policy for every attempt. This connector previously skipped that
boundary and contacted ``export.arxiv.org`` directly from Celery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock, patch

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

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected raise_for_status {self.status_code}")


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
    from src.services.research_engine.connectors.arxiv_connector import ArxivConnector

    slot = AsyncMock(return_value=0.0)
    with (
        patch("httpx.AsyncClient", _Client),
        patch("src.services.arxiv.arxiv_service._acquire_arxiv_rate_slot", slot),
    ):
        docs = await ArxivConnector().search("transformers", max_results=1)

    assert slot.await_count == 1, (
        "every arXiv call must reserve a slot on the shared cross-pod gate — "
        "this connector runs from Celery and can rate-limit the whole platform"
    )
    assert len(docs) == 1


async def test_uses_https_not_plain_http() -> None:
    from src.services.research_engine.connectors.arxiv_connector import ArxivConnector

    with (
        patch("httpx.AsyncClient", _Client),
        patch(
            "src.services.arxiv.arxiv_service._acquire_arxiv_rate_slot",
            AsyncMock(return_value=0.0),
        ),
    ):
        await ArxivConnector().search("transformers")

    assert _Client.calls[0]["url"].startswith("https://")


async def test_429_is_retried_after_reserving_another_slot() -> None:
    """A retry that skips the gate is the burst the gate exists to prevent."""
    from src.services.research_engine.connectors.arxiv_connector import ArxivConnector

    limited = _Resp(status_code=429, text="")
    limited.headers = {"Retry-After": "0"}
    _Client.responses = [limited, _Resp()]

    slot = AsyncMock(return_value=0.0)
    with (
        patch("httpx.AsyncClient", _Client),
        patch("src.services.arxiv.arxiv_service._acquire_arxiv_rate_slot", slot),
        patch(
            "src.services.arxiv.arxiv_service._arm_arxiv_cooldown",
            AsyncMock(),
        ),
    ):
        docs = await ArxivConnector().search("transformers")

    assert slot.await_count == 2, "the retry must reserve its own slot"
    assert len(_Client.calls) == 2
    assert len(docs) == 1


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


def test_connector_uses_the_shared_request_implementation() -> None:
    """The Celery connector must not grow an independent HTTP retry loop."""
    src = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "services"
        / "research_engine"
        / "connectors"
        / "arxiv_connector.py"
    ).read_text()

    assert "request_arxiv_api" in src
    assert "_MAX_ATTEMPTS" not in src
    assert "await client.get(" not in src
