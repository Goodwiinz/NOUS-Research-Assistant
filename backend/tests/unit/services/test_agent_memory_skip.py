"""Unit tests for the memory_retrieval_node conversational-skip fast-path.

A greeting ("hi", "thanks") must NOT trigger the Cohere query-embedding +
Postgres semantic search inside ``search_memories`` — that work sits on the
parallel-preprocessing critical path (~0.5-2s) and, because the save gate in
``memory_save_node`` never persists greeting turns, can never return a useful
hit. See trace 019e9ef7 (a "hi" turn embedded the query and recalled 5
low-score noise memories).
"""

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
class TestMemoryRetrievalFastPath:
    async def _run(self, monkeypatch, query: str):
        from unittest.mock import AsyncMock, MagicMock

        from langchain_core.messages import HumanMessage

        import src.services.agent.memory as memmod
        from src.services.agent._nodes_memory import memory_retrieval_node

        mock_search = AsyncMock(return_value=[{"key": "k", "score": 0.4}])
        monkeypatch.setattr(memmod, "search_memories", mock_search)
        monkeypatch.setattr(
            memmod, "get_memory_store", AsyncMock(return_value=MagicMock())
        )

        state = {"messages": [HumanMessage(content=query)]}
        config = {"configurable": {"user_id": "user-mem-1"}}

        result = await memory_retrieval_node(state, config)
        return mock_search, result

    async def test_skips_search_for_conversational_query(self, monkeypatch):
        for q in ("hi", "thanks!", "ok cool", "hello", "yes"):
            mock_search, result = await self._run(monkeypatch, q)
            assert mock_search.called is False, q
            assert result == {"user_memories": []}, q

    async def test_runs_search_for_real_query(self, monkeypatch):
        mock_search, result = await self._run(
            monkeypatch, "what did we decide about transformer attention"
        )
        assert mock_search.called is True
        assert result["user_memories"]

    async def test_runs_search_when_real_query_contains_small_talk_words(
        self, monkeypatch
    ):
        """R8-A1 (GOO-362): "no"/"good"/"what model" inside a question recall."""
        for q in (
            "Do my papers find no effect of LoRA on good calibration?",
            "what model does the paper propose",
        ):
            mock_search, _ = await self._run(monkeypatch, q)
            assert mock_search.called is True, q
