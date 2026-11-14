"""Audit finding D (live trace thread e3c56cef, 2026-08-11): DO KB chunks were
injected into the prompt regardless of relevance score — a query about
attention mechanisms pulled 5 chunks at 0.036-0.25 cohere relevance from
unrelated cybersecurity surveys. ``_drop_low_relevance_chunks`` floors only
cohere-scored chunks (the one calibrated 0-1 scale in the code); other score
sources (rank_proxy, upstream) pass through untouched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import HumanMessage

from src.services.agent._nodes_rag import _drop_low_relevance_chunks, rag_node
from src.services.do_kb.models import Chunk
from src.services.do_kb.postprocess import drop_low_relevance_chunks


def _ctx(score: float, score_source: str | None) -> dict:
    return {"content": "x", "score": score, "score_source": score_source}


def test_drops_junk_cohere_scores_below_floor() -> None:
    contexts = [_ctx(0.036, "cohere"), _ctx(0.25, "cohere")]
    kept = _drop_low_relevance_chunks(contexts)
    assert kept == [_ctx(0.25, "cohere")]


def test_keeps_non_cohere_scores_regardless_of_value() -> None:
    """rank_proxy / upstream scales aren't verified — never floored here."""
    contexts = [_ctx(0.01, "rank_proxy"), _ctx(0.02, "upstream"), _ctx(0.0, None)]
    assert _drop_low_relevance_chunks(contexts) == contexts


def test_no_chunks_dropped_is_a_noop() -> None:
    contexts = [_ctx(0.9, "cohere")]
    assert _drop_low_relevance_chunks(contexts) == contexts


def test_shared_floor_handles_chunk_score_provenance() -> None:
    chunks = [
        Chunk(text="junk", score=0.09, metadata={"score_source": "cohere"}),
        Chunk(text="rank", score=0.01, metadata={"score_source": "rank_proxy"}),
        Chunk(text="relevant", score=0.1, metadata={"score_source": "cohere"}),
    ]

    assert drop_low_relevance_chunks(chunks) == chunks[1:]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_filtered_primary_read_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit review, PR #1395 (Codex P2): when every DO KB chunk is filtered by
    the relevance floor, the primary read returns `[]` — a *successful* read
    with nothing relevant, not an unavailable one. rag_node must proceed with
    empty contexts, NOT fall through to the unfiltered legacy hybrid search
    (that would smuggle the exact junk the floor was added to drop back in
    through the side door)."""
    from src.services.agent import _nodes_rag

    monkeypatch.setattr(
        _nodes_rag, "_try_primary_do_kb_read", AsyncMock(return_value=[])
    )
    fallback = AsyncMock(side_effect=AssertionError("must not fall back"))
    monkeypatch.setattr(_nodes_rag, "_hybrid_search_fallback_with_status", fallback)

    project_id = "5ed25258-5ad2-4b06-9678-4a4abe5ecac1"
    state = {
        "messages": [HumanMessage(content="Find papers on attention mechanisms")],
        "page_context": {"type": "project", "project_id": project_id},
        "current_project_id": project_id,
    }

    result = await rag_node(
        state,
        config={"configurable": {"user_id": "user-1", "organization_id": "org-1"}},
    )

    fallback.assert_not_awaited()
    assert result["retrieved_contexts"] == []
