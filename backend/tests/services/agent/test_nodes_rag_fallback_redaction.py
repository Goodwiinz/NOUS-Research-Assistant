"""The hybrid fallback is the default-config retrieval path (DO_KB_ENABLED
false) and must redact credential-shaped content before contexts reach the
system prompt / SSE frame / citations (audit 2026-08-07, gap 1)."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MARKER = "gho_000000000000000000000000000000000000"


def _fake_result(title: str, full_text: str) -> SimpleNamespace:
    return SimpleNamespace(
        document_id="11111111-1111-1111-1111-111111111111",
        title=title,
        metadata={"full_text": full_text},
        content_preview=None,
        relevance_score=0.9,
    )


async def test_hybrid_fallback_redacts_title_and_content() -> None:
    from src.services.agent._nodes_rag import _legacy_hybrid_search_fallback

    response = SimpleNamespace(
        results=[_fake_result(f"Ops notes {MARKER}", f"token is {MARKER} ok")]
    )
    with patch(
        "src.services.search.hybrid_search_service.hybrid_search_service.search",
        return_value=response,
    ):
        contexts = await _legacy_hybrid_search_fallback(
            query="ops token", user_id="u-1", organization_id="o-1"
        )

    assert len(contexts) == 1
    assert MARKER not in contexts[0]["title"]
    assert MARKER not in contexts[0]["content"]
    assert "<token>" in contexts[0]["content"]


async def test_hybrid_fallback_leaves_clean_content_intact() -> None:
    from src.services.agent._nodes_rag import _legacy_hybrid_search_fallback

    response = SimpleNamespace(results=[_fake_result("Plain title", "plain body text")])
    with patch(
        "src.services.search.hybrid_search_service.hybrid_search_service.search",
        return_value=response,
    ):
        contexts = await _legacy_hybrid_search_fallback(
            query="plain", user_id="u-1", organization_id="o-1"
        )

    assert contexts[0]["title"] == "Plain title"
    assert contexts[0]["content"] == "plain body text"


async def test_hybrid_fallback_keeps_active_project_scope() -> None:
    from src.services.agent._nodes_rag import _legacy_hybrid_search_fallback

    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    document_id = uuid.uuid4()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [document_id]
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    @asynccontextmanager
    async def scoped_session():
        yield session

    captured = {}

    def search(**kwargs):
        captured["request"] = kwargs["search_request"]
        return SimpleNamespace(results=[])

    with (
        patch(
            "src.services.agent.tool_session.tool_session",
            side_effect=scoped_session,
        ),
        patch(
            "src.services.search.hybrid_search_service.hybrid_search_service.search",
            side_effect=search,
        ),
    ):
        contexts = await _legacy_hybrid_search_fallback(
            query="project query",
            user_id=user_id,
            organization_id=str(uuid.uuid4()),
            project_id=project_id,
        )

    assert contexts == []
    assert captured["request"].filters.document_ids == [str(document_id)]
    sql = str(session.execute.await_args.args[0])
    assert "collections.is_deleted = false" in sql
    assert "collection_documents.is_deleted = false" in sql
    assert "workspaces.owner_id" in sql


async def test_hybrid_fallback_fails_closed_for_empty_project() -> None:
    from src.services.agent._nodes_rag import _legacy_hybrid_search_fallback

    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    @asynccontextmanager
    async def scoped_session():
        yield session

    search = AsyncMock()
    with (
        patch(
            "src.services.agent.tool_session.tool_session",
            side_effect=scoped_session,
        ),
        patch(
            "src.services.search.hybrid_search_service.hybrid_search_service.search",
            search,
        ),
    ):
        contexts = await _legacy_hybrid_search_fallback(
            query="project query",
            user_id=str(uuid.uuid4()),
            organization_id=str(uuid.uuid4()),
            project_id=str(uuid.uuid4()),
        )

    assert contexts == []
    search.assert_not_called()


# --- Evidence content: the matched passage, not the document's opening -----
#
# Fulltext never sets metadata["full_text"/"text"], so the fallback used to fall
# through to content_preview, which is content_text[:300]: the LLM was given
# the first 300 characters of each document instead of the passage that
# matched the query (audit 2026-11, retrieval #3).


def _snippet(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text)


async def test_hybrid_fallback_requests_snippets() -> None:
    from src.services.agent._nodes_rag import _legacy_hybrid_search_fallback

    captured = {}

    def search(**kwargs):
        captured["request"] = kwargs["search_request"]
        return SimpleNamespace(results=[])

    with patch(
        "src.services.search.hybrid_search_service.hybrid_search_service.search",
        side_effect=search,
    ):
        await _legacy_hybrid_search_fallback(
            query="attention heads", user_id="u-1", organization_id="o-1"
        )

    assert captured["request"].include_snippets is True


async def test_hybrid_fallback_uses_matched_snippets_over_preview() -> None:
    from src.services.agent._nodes_rag import _legacy_hybrid_search_fallback

    result = SimpleNamespace(
        document_id="11111111-1111-1111-1111-111111111111",
        title="Transformers",
        metadata={},
        content_preview="Abstract. This opening paragraph is unrelated...",
        snippets=[
            _snippet("Each <mark>attention</mark> <mark>head</mark> attends."),
            _snippet("Multi-<mark>head</mark> layers are stacked six times."),
        ],
        relevance_score=0.9,
    )
    with patch(
        "src.services.search.hybrid_search_service.hybrid_search_service.search",
        return_value=SimpleNamespace(results=[result]),
    ):
        contexts = await _legacy_hybrid_search_fallback(
            query="attention heads", user_id="u-1", organization_id="o-1"
        )

    content = contexts[0]["content"]
    assert "Each attention head attends." in content
    assert "Multi-head layers are stacked six times." in content
    assert "<mark>" not in content and "</mark>" not in content
    assert "opening paragraph is unrelated" not in content


async def test_hybrid_fallback_uses_preview_when_no_snippets() -> None:
    from src.services.agent._nodes_rag import _legacy_hybrid_search_fallback

    result = SimpleNamespace(
        document_id="11111111-1111-1111-1111-111111111111",
        title="Doc",
        metadata={},
        content_preview="only the preview",
        snippets=[],
        relevance_score=0.5,
    )
    with patch(
        "src.services.search.hybrid_search_service.hybrid_search_service.search",
        return_value=SimpleNamespace(results=[result]),
    ):
        contexts = await _legacy_hybrid_search_fallback(
            query="q", user_id="u-1", organization_id="o-1"
        )

    assert contexts[0]["content"] == "only the preview"
