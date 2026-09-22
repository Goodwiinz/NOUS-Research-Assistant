"""forget_memory deletes the closest semantic match from the user's store."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.agent.memory import delete_memory_by_query


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_memory_returns_deleted_count():
    store = MagicMock()
    matched_item = MagicMock(key="abc123", score=0.92)
    matched_item.value = {"query": "find papers on transformers"}
    store.asearch = AsyncMock(return_value=[matched_item])
    store.adelete = AsyncMock(return_value=None)

    out = await delete_memory_by_query(
        store, user_id="user-1", query="papers on transformers", limit=3
    )

    assert out["deleted"] == 1
    assert out["matches"][0]["key"] == "abc123"
    store.adelete.assert_awaited_once_with(("user", "user-1"), "abc123")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_memory_skips_low_score_match():
    """A match with score < 0.6 is reported but NOT deleted (avoid accidents)."""
    store = MagicMock()
    weak = MagicMock(key="xyz", score=0.4)
    weak.value = {"query": "something else"}
    store.asearch = AsyncMock(return_value=[weak])
    store.adelete = AsyncMock(return_value=None)

    out = await delete_memory_by_query(store, user_id="user-1", query="papers", limit=3)

    assert out["deleted"] == 0
    assert out["matches"][0]["score"] == 0.4
    store.adelete.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_memory_unindexed_uses_specific_substring_fallback():
    """Audit B3 — with no semantic index every item scores None (coerced 0.0),
    so the 0.6 threshold rejects everything and a confirmed forget would
    silently delete nothing. The exact-substring fallback must still delete a
    memory that genuinely mentions a sufficiently specific query."""
    store = MagicMock()
    hit = MagicMock(key="k-hit", score=None)
    hit.value = {"query": "remember my arXiv research recovery contact details"}
    miss = MagicMock(key="k-miss", score=None)
    miss.value = {"query": "buy milk tomorrow"}
    store.asearch = AsyncMock(return_value=[hit, miss])
    store.adelete = AsyncMock(return_value=None)

    out = await delete_memory_by_query(
        store, user_id="u1", query="ARXIV RESEARCH RECOVERY CONTACT", limit=5
    )

    assert out["deleted"] == 1
    store.adelete.assert_awaited_once_with(("user", "u1"), "k-hit")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "my arXiv research recovery contact email jordan.avery@example.com",
        "the recovery contact email for my arXiv research",
    ],
)
async def test_delete_memory_unindexed_matches_redacted_paraphrase(query):
    store = MagicMock()
    hit = MagicMock(key="k-hit", score=None)
    hit.value = {
        "query": (
            "Please remember this for my arXiv research going forward: "
            "my recovery contact email is <email>."
        )
    }
    miss = MagicMock(key="k-miss", score=None)
    miss.value = {"query": "Please remember my grocery delivery window."}
    store.asearch = AsyncMock(return_value=[hit, miss])
    store.adelete = AsyncMock(return_value=None)

    out = await delete_memory_by_query(store, user_id="u1", query=query, limit=5)

    assert out["deleted"] == 1
    store.adelete.assert_awaited_once_with(("user", "u1"), "k-hit")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ["jordan.avery@example.com", "forget this"], ids=["pii-only", "generic"]
)
async def test_delete_memory_unindexed_unsafe_query_deletes_nothing(query):
    store = MagicMock()
    first = MagicMock(key="k-first", score=None)
    first.value = {"query": "My arXiv recovery contact email is <email>."}
    second = MagicMock(key="k-second", score=None)
    second.value = {"query": "My billing contact email is <email>."}
    store.asearch = AsyncMock(return_value=[first, second])
    store.adelete = AsyncMock(return_value=None)

    out = await delete_memory_by_query(store, user_id="u1", query=query, limit=5)

    assert out["deleted"] == 0
    store.adelete.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "stored_query"),
    [
        ("ALICE@example.com", "remember my email is alice@example.com"),
        ("forget this", "please forget this stored item"),
        ("all memories delete", "delete all memories"),
        ("delete every single thing", "please delete every single thing"),
        ("thing single every delete", "delete every single thing"),
    ],
    ids=[
        "pii-only-exact",
        "generic-exact",
        "generic-reordered",
        "generic-quantifiers-exact",
        "generic-quantifiers-reordered",
    ],
)
async def test_delete_memory_unindexed_generic_request_deletes_nothing(
    query, stored_query
):
    store = MagicMock()
    recent = MagicMock(key="k-recent", score=None)
    recent.value = {"query": stored_query}
    store.asearch = AsyncMock(return_value=[recent])
    store.adelete = AsyncMock(return_value=None)

    out = await delete_memory_by_query(store, user_id="u1", query=query, limit=5)

    assert out["deleted"] == 0
    store.adelete.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_memory_unindexed_no_text_match_deletes_nothing():
    """Audit B3 — the unindexed fallback must NOT blindly delete the recency
    hits asearch returns; only memories whose text contains the query go."""
    store = MagicMock()
    recent = MagicMock(key="k1", score=None)
    recent.value = {"query": "an unrelated recent memory"}
    store.asearch = AsyncMock(return_value=[recent])
    store.adelete = AsyncMock(return_value=None)

    out = await delete_memory_by_query(
        store, user_id="u1", query="quantum gravity", limit=5
    )

    assert out["deleted"] == 0
    store.adelete.assert_not_awaited()


@pytest.mark.unit
def test_forget_memory_is_destructive_tool():
    from src.services.agent._nodes_tools import DESTRUCTIVE_TOOLS

    assert "forget_memory" in DESTRUCTIVE_TOOLS


@pytest.mark.unit
def test_forget_memory_tool_registered():
    from src.services.agent.tools import ALL_TOOLS

    assert any(
        t.name == "forget_memory" for t in ALL_TOOLS
    ), "forget_memory must be registered in ALL_TOOLS so subgraphs can bind it"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_failure_propagates_instead_of_reporting_zero_deletions():
    """B8-S1: a failed store search is not "nothing matched"."""
    store = MagicMock()
    store.asearch = AsyncMock(side_effect=RuntimeError("store down"))
    store.adelete = AsyncMock(return_value=None)

    with pytest.raises(RuntimeError):
        await delete_memory_by_query(store, user_id="u1", query="papers", limit=3)

    store.adelete.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_reports_a_search_failure_as_an_error_not_completed():
    from src.services.agent import tools_impl

    store = MagicMock()
    store.asearch = AsyncMock(side_effect=RuntimeError("store down"))

    with patch(
        "src.services.agent.memory.get_memory_store", AsyncMock(return_value=store)
    ):
        out = await tools_impl._tool_forget_memory(query="papers", user_id="u1")

    # A HITL-approved destructive action that never ran must not come back
    # as {"status": "completed", "deleted": 0}.
    assert "error" in out
    assert out.get("status") != "completed"
