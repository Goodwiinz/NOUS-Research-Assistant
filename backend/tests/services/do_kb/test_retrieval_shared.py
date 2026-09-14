"""Audit B2 (DO KB retrieval) — shared retrieve core + delegation parity.

The RAG node's primary read (``_nodes_rag._try_primary_do_kb_read``) and the
agent ``do_kb_retrieve`` tool (``tools_impl._tool_do_kb_retrieve``) shared the
same org→kb lookup and the same 404-vs-transient error classification. That
core now lives in ``src.services.do_kb.retrieval``; each caller maps the
returned :class:`DOKBRetrieveOutcome` onto its own timeout / telemetry / return
shape.

These tests cover:
  - ``resolve_org_kb_uuid`` (org → kb_uuid);
  - ``retrieve_kb_chunks`` outcome classification (success / 404 / transient /
    timeout / propagated generic error), incl. the 404-ERROR-vs-503-WARNING
    logging split;
  - both call sites *delegate* to ``retrieve_kb_chunks`` (the dedup is real,
    not two copies) while preserving their divergent post-processing.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.do_kb.client import DOKnowledgeBaseError
from src.services.do_kb.models import Chunk, RetrieveResult
from src.services.do_kb.retrieval import (
    DOKBRetrieveOutcome,
    DOKBRetrieveStatus,
    resolve_org_kb_uuid,
    retrieve_kb_chunks,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# resolve_org_kb_uuid
# ---------------------------------------------------------------------------


async def test_resolve_org_kb_uuid_returns_uuid():
    session = MagicMock()
    session.get = AsyncMock(return_value=SimpleNamespace(do_kb_uuid="kb-9"))
    assert await resolve_org_kb_uuid(session, "org-1") == "kb-9"
    session.get.assert_awaited_once()


async def test_resolve_org_kb_uuid_none_when_org_missing():
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    assert await resolve_org_kb_uuid(session, "org-1") is None


async def test_resolve_org_kb_uuid_short_circuits_without_org_id():
    session = MagicMock()
    session.get = AsyncMock()
    assert await resolve_org_kb_uuid(session, None) is None
    session.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# retrieve_kb_chunks — outcome classification
# ---------------------------------------------------------------------------


def _client_raising(exc):
    client = MagicMock()
    client.retrieve = AsyncMock(side_effect=exc)
    return client


async def test_success_returns_result():
    result = RetrieveResult(
        chunks=[Chunk(text="hi", score=0.9, document_id="d", metadata={})], total=1
    )
    client = MagicMock()
    client.retrieve = AsyncMock(return_value=result)
    with patch("src.services.do_kb.get_do_kb_client", return_value=client):
        outcome = await retrieve_kb_chunks(kb_uuid="kb", query="q", top_k=5)
    assert outcome.status is DOKBRetrieveStatus.SUCCESS
    assert outcome.result is result
    client.retrieve.assert_awaited_once_with(kb_uuid="kb", query="q", top_k=5)


async def test_404_returns_error_404_and_logs_error(caplog):
    exc = DOKnowledgeBaseError("gone", status_code=404)
    with (
        patch("src.services.do_kb.get_do_kb_client", return_value=_client_raising(exc)),
        caplog.at_level(logging.ERROR),
    ):
        outcome = await retrieve_kb_chunks(kb_uuid="kb", query="q", org_id="org-1")
    assert outcome.status is DOKBRetrieveStatus.ERROR_404
    assert outcome.error is exc
    assert any(
        r.levelno >= logging.ERROR and "404" in r.getMessage() for r in caplog.records
    )


async def test_transient_error_returns_error_other_stays_warning(caplog):
    exc = DOKnowledgeBaseError("upstream down", status_code=503)
    with (
        patch("src.services.do_kb.get_do_kb_client", return_value=_client_raising(exc)),
        caplog.at_level(logging.WARNING),
    ):
        outcome = await retrieve_kb_chunks(kb_uuid="kb", query="q", org_id="org-1")
    assert outcome.status is DOKBRetrieveStatus.ERROR_OTHER
    assert outcome.error is exc
    # A transient failure never escalates to ERROR (keeps 404s distinguishable).
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_timeout_returns_timeout_status():
    async def _slow(**_kwargs):
        await asyncio.sleep(10)
        return RetrieveResult(chunks=[], total=0)

    client = MagicMock()
    client.retrieve = _slow
    with patch("src.services.do_kb.get_do_kb_client", return_value=client):
        outcome = await retrieve_kb_chunks(kb_uuid="kb", query="q", timeout=0.05)
    assert outcome.status is DOKBRetrieveStatus.TIMEOUT
    assert outcome.result is None


async def test_no_timeout_does_not_wrap_and_passes_top_k():
    result = RetrieveResult(chunks=[], total=0)
    client = MagicMock()
    client.retrieve = AsyncMock(return_value=result)
    with patch("src.services.do_kb.get_do_kb_client", return_value=client):
        outcome = await retrieve_kb_chunks(kb_uuid="kb", query="q", top_k=None)
    assert outcome.status is DOKBRetrieveStatus.SUCCESS
    client.retrieve.assert_awaited_once_with(kb_uuid="kb", query="q", top_k=None)


async def test_named_document_filters_are_forwarded_once():
    filters = {
        "equals": {
            "key": "item_name",
            "value": "11111111-1111-4111-8111-111111111111.txt",
        }
    }
    client = MagicMock()
    client.retrieve = AsyncMock(return_value=RetrieveResult(chunks=[], total=0))

    with patch("src.services.do_kb.get_do_kb_client", return_value=client):
        outcome = await retrieve_kb_chunks(
            kb_uuid="kb", query="attention", top_k=8, filters=filters
        )

    assert outcome.status is DOKBRetrieveStatus.SUCCESS
    client.retrieve.assert_awaited_once_with(
        kb_uuid="kb", query="attention", top_k=8, filters=filters
    )


async def test_filtered_400_preserves_status_without_broad_retry():
    filters = {
        "equals": {
            "key": "item_name",
            "value": "11111111-1111-4111-8111-111111111111.txt",
        }
    }
    exc = DOKnowledgeBaseError("invalid request body", status_code=400)
    client = _client_raising(exc)

    with patch("src.services.do_kb.get_do_kb_client", return_value=client):
        outcome = await retrieve_kb_chunks(
            kb_uuid="kb", query="attention", filters=filters
        )

    assert outcome.status is DOKBRetrieveStatus.ERROR_OTHER
    assert outcome.error is exc
    client.retrieve.assert_awaited_once_with(
        kb_uuid="kb", query="attention", top_k=None, filters=filters
    )


async def test_generic_exception_propagates():
    with patch(
        "src.services.do_kb.get_do_kb_client",
        return_value=_client_raising(RuntimeError("boom")),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await retrieve_kb_chunks(kb_uuid="kb", query="q")


# ---------------------------------------------------------------------------
# Delegation parity — both call sites route through retrieve_kb_chunks
# ---------------------------------------------------------------------------


async def test_tool_delegates_to_shared_retrieve():
    """``_tool_do_kb_retrieve`` calls the shared helper (not an inline copy)."""
    from src.services.agent import tools_impl

    user = SimpleNamespace(organization_id="org-1", id="u-1")
    db = MagicMock()
    empty_rows = MagicMock()
    empty_rows.__iter__ = lambda self: iter([])
    db.execute = AsyncMock(return_value=empty_rows)

    result = RetrieveResult(
        chunks=[Chunk(text="hi", score=0.9, document_id="d.pdf", metadata={})],
        total=1,
    )
    fake_retrieve = AsyncMock(
        return_value=DOKBRetrieveOutcome(
            status=DOKBRetrieveStatus.SUCCESS, result=result
        )
    )

    # Concrete flags (not a MagicMock) so the post-retrieve rerank/evidence
    # branches stay disabled instead of tripping on truthy auto-attributes.
    fake_settings = SimpleNamespace(
        DO_KB_ENABLED=True,
        DO_KB_DEFAULT_TOP_K=10,
        AGENT_DOKB_COHERE_RERANK=False,
        AGENT_ITERATIVE_RETRIEVAL=False,
    )

    with (
        patch("src.core.config.settings", fake_settings),
        patch(
            "src.services.do_kb.retrieval.resolve_org_kb_uuid",
            AsyncMock(return_value="kb-1"),
        ),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", fake_retrieve),
    ):
        out = await tools_impl._tool_do_kb_retrieve(
            {"query": "hi", "top_k": 7}, db, user
        )

    fake_retrieve.assert_awaited_once()
    kwargs = fake_retrieve.await_args.kwargs
    assert kwargs["kb_uuid"] == "kb-1"
    assert kwargs["query"] == "hi"
    assert kwargs["top_k"] == 7
    assert kwargs["org_id"] == "org-1"
    # No timeout for the tool path.
    assert kwargs.get("timeout") is None
    assert out["source"] == "do_kb"
    assert out["chunks"][0]["text"] == "hi"


@pytest.mark.parametrize(
    ("score_sources", "expected_semantics"),
    [
        (("rank_proxy",), "rank_only"),
        (("cohere",), "relevance"),
        (("upstream",), "upstream"),
        (("upstream", "rank_proxy"), "mixed"),
    ],
)
async def test_tool_reports_score_semantics_from_emitted_chunks(
    score_sources: tuple[str, ...], expected_semantics: str
) -> None:
    """The tool reports the provenance of every emitted score."""
    from src.services.agent import tools_impl

    user = SimpleNamespace(organization_id="org-1", id="u-1")
    db = MagicMock()
    chunks = [
        Chunk(
            text=f"retrieved text {index}",
            score=0.1,
            document_id=f"d-{index}.pdf",
            metadata={"score_source": score_source},
        )
        for index, score_source in enumerate(score_sources)
    ]
    fake_retrieve = AsyncMock(
        return_value=DOKBRetrieveOutcome(
            status=DOKBRetrieveStatus.SUCCESS,
            result=RetrieveResult(chunks=chunks, total=len(chunks)),
        )
    )
    cfg = SimpleNamespace(
        DO_KB_ENABLED=True,
        DO_KB_DEFAULT_TOP_K=10,
        AGENT_DOKB_COHERE_RERANK=False,
        AGENT_ITERATIVE_RETRIEVAL=False,
    )

    with (
        patch("src.core.config.settings", cfg),
        patch(
            "src.services.do_kb.retrieval.resolve_org_kb_uuid",
            AsyncMock(return_value="kb-1"),
        ),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", fake_retrieve),
        patch(
            "src.services.do_kb.resolve.resolve_and_filter_chunks",
            AsyncMock(return_value=({}, chunks)),
        ),
    ):
        out = await tools_impl._tool_do_kb_retrieve(
            {"query": "q", "top_k": 1}, db, user
        )

    assert out["score_semantics"] == expected_semantics
    if expected_semantics == "rank_only":
        assert "retrieval rank" in out["note"]
    else:
        assert "note" not in out


async def test_rag_node_delegates_to_shared_retrieve():
    """``_try_primary_do_kb_read`` calls the shared helper with the timeout."""
    from contextlib import asynccontextmanager

    from src.services.agent import _nodes_rag

    # Ids-only signature (audit B8): scalar ids; org must parse as a UUID.
    user_id = str(uuid.uuid4())
    org_uuid = uuid.uuid4()

    cfg = MagicMock()
    cfg.DO_KB_PRIMARY_READ = True
    cfg.DO_KB_RETRIEVE_TIMEOUT_SECONDS = 4.0
    cfg.AGENT_DOKB_COHERE_RERANK = False

    session = MagicMock()

    @asynccontextmanager
    async def _session_ctx():
        yield session

    result = RetrieveResult(
        chunks=[Chunk(text="hi", score=0.9, document_id="d", metadata={})], total=1
    )
    fake_retrieve = AsyncMock(
        return_value=DOKBRetrieveOutcome(
            status=DOKBRetrieveStatus.SUCCESS, result=result
        )
    )

    async def fake_resolve(*, chunks, org_id, session, project_id):
        return ({}, chunks)

    with (
        patch("src.core.config.settings", cfg),
        patch("src.core.database.AsyncSessionLocal", _session_ctx),
        patch(
            "src.services.do_kb.retrieval.resolve_org_kb_uuid",
            AsyncMock(return_value="kb-1"),
        ),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", fake_retrieve),
        patch("src.services.do_kb.resolve.resolve_and_filter_chunks", fake_resolve),
    ):
        out = await _nodes_rag._try_primary_do_kb_read(
            "q", user_id, str(org_uuid), project_id=None
        )

    fake_retrieve.assert_awaited_once()
    kwargs = fake_retrieve.await_args.kwargs
    assert kwargs["kb_uuid"] == "kb-1"
    assert kwargs["timeout"] == 4.0
    assert kwargs["org_id"] == org_uuid
    assert out is not None and len(out) == 1


@pytest.mark.parametrize("rerank_enabled", [False, True])
async def test_rag_node_sanitizes_and_deduplicates_before_optional_rerank(
    rerank_enabled: bool,
):
    """The primary path never exposes unsanitized or duplicate chunks."""
    from contextlib import asynccontextmanager

    from src.services.agent import _nodes_rag

    user_id = str(uuid.uuid4())
    org_uuid = uuid.uuid4()
    cfg = SimpleNamespace(
        DO_KB_PRIMARY_READ=True,
        DO_KB_RETRIEVE_TIMEOUT_SECONDS=4.0,
        AGENT_DOKB_COHERE_RERANK=rerank_enabled,
    )
    session = MagicMock()

    @asynccontextmanager
    async def _session_ctx():
        yield session

    chunks = [
        Chunk(
            text="Contact synthetic.alpha@example.test",
            score=0.9,
            document_id="a",
            metadata={"score_source": "rank_proxy"},
        ),
        Chunk(
            text="Contact synthetic.beta@example.test",
            score=0.8,
            document_id="b",
            metadata={"score_source": "rank_proxy"},
        ),
        Chunk(
            text="Distinct result",
            score=0.7,
            document_id="c",
            metadata={"score_source": "rank_proxy"},
        ),
    ]
    outcome = DOKBRetrieveOutcome(
        status=DOKBRetrieveStatus.SUCCESS,
        result=RetrieveResult(chunks=chunks, total=3),
    )

    async def _resolve(**kwargs):
        return ({}, kwargs["chunks"])

    async def _rerank(_query, sanitized_chunks):
        assert [chunk.text for chunk in sanitized_chunks] == [
            "Contact <email>",
            "Distinct result",
        ]
        return list(reversed(sanitized_chunks))

    rerank = AsyncMock(side_effect=_rerank)
    with (
        patch("src.core.config.settings", cfg),
        patch("src.core.database.AsyncSessionLocal", _session_ctx),
        patch(
            "src.services.do_kb.retrieval.resolve_org_kb_uuid",
            AsyncMock(return_value="kb-1"),
        ),
        patch(
            "src.services.do_kb.retrieval.retrieve_kb_chunks",
            AsyncMock(return_value=outcome),
        ),
        patch("src.services.do_kb.resolve.resolve_and_filter_chunks", _resolve),
        patch("src.services.do_kb.rerank.cohere_rescore_chunks", rerank),
    ):
        contexts = await _nodes_rag._try_primary_do_kb_read("q", user_id, str(org_uuid))

    assert contexts is not None
    expected = (
        ["Distinct result", "Contact <email>"]
        if rerank_enabled
        else ["Contact <email>", "Distinct result"]
    )
    assert [context["content"] for context in contexts] == expected
    assert all("synthetic." not in context["content"] for context in contexts)
    if rerank_enabled:
        rerank.assert_awaited_once()
    else:
        rerank.assert_not_awaited()


async def test_rag_node_rerank_failure_keeps_sanitized_deduplicated_order():
    from contextlib import asynccontextmanager

    from src.services.agent import _nodes_rag

    user_id = str(uuid.uuid4())
    org_uuid = uuid.uuid4()
    cfg = SimpleNamespace(
        DO_KB_PRIMARY_READ=True,
        DO_KB_RETRIEVE_TIMEOUT_SECONDS=4.0,
        AGENT_DOKB_COHERE_RERANK=True,
    )
    session = MagicMock()

    @asynccontextmanager
    async def _session_ctx():
        yield session

    chunks = [
        Chunk(text="Call 415-555-0101", document_id="a"),
        Chunk(text="Call 415-555-0102", document_id="b"),
    ]
    outcome = DOKBRetrieveOutcome(
        status=DOKBRetrieveStatus.SUCCESS,
        result=RetrieveResult(chunks=chunks, total=2),
    )

    async def _resolve(**kwargs):
        return ({}, kwargs["chunks"])

    async def _failure_passthrough(_query, sanitized_chunks):
        assert [chunk.text for chunk in sanitized_chunks] == ["Call <phone>"]
        return sanitized_chunks

    with (
        patch("src.core.config.settings", cfg),
        patch("src.core.database.AsyncSessionLocal", _session_ctx),
        patch(
            "src.services.do_kb.retrieval.resolve_org_kb_uuid",
            AsyncMock(return_value="kb-1"),
        ),
        patch(
            "src.services.do_kb.retrieval.retrieve_kb_chunks",
            AsyncMock(return_value=outcome),
        ),
        patch("src.services.do_kb.resolve.resolve_and_filter_chunks", _resolve),
        patch(
            "src.services.do_kb.rerank.cohere_rescore_chunks",
            AsyncMock(side_effect=_failure_passthrough),
        ),
    ):
        contexts = await _nodes_rag._try_primary_do_kb_read("q", user_id, str(org_uuid))

    assert contexts is not None
    assert [context["content"] for context in contexts] == ["Call <phone>"]


async def test_rag_node_empty_after_sanitization_triggers_fallback():
    from contextlib import asynccontextmanager

    from src.services.agent import _nodes_rag

    user_id = str(uuid.uuid4())
    org_uuid = uuid.uuid4()
    cfg = SimpleNamespace(
        DO_KB_PRIMARY_READ=True,
        DO_KB_RETRIEVE_TIMEOUT_SECONDS=4.0,
        AGENT_DOKB_COHERE_RERANK=True,
    )
    session = MagicMock()

    @asynccontextmanager
    async def _session_ctx():
        yield session

    outcome = DOKBRetrieveOutcome(
        status=DOKBRetrieveStatus.SUCCESS,
        result=RetrieveResult(chunks=[Chunk(text=" \n ", document_id="a")], total=1),
    )

    async def _resolve(**kwargs):
        return ({}, kwargs["chunks"])

    rerank = AsyncMock()
    with (
        patch("src.core.config.settings", cfg),
        patch("src.core.database.AsyncSessionLocal", _session_ctx),
        patch(
            "src.services.do_kb.retrieval.resolve_org_kb_uuid",
            AsyncMock(return_value="kb-1"),
        ),
        patch(
            "src.services.do_kb.retrieval.retrieve_kb_chunks",
            AsyncMock(return_value=outcome),
        ),
        patch("src.services.do_kb.resolve.resolve_and_filter_chunks", _resolve),
        patch("src.services.do_kb.rerank.cohere_rescore_chunks", rerank),
    ):
        contexts = await _nodes_rag._try_primary_do_kb_read("q", user_id, str(org_uuid))

    assert contexts is None
    rerank.assert_not_awaited()
