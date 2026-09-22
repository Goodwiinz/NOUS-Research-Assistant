"""Regression tests for the 8 agent workflow bugs ported from develop (PR #383 hotfix)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import pytest
from langgraph.errors import GraphInterrupt

pytestmark = pytest.mark.asyncio


def _make_user():
    user = Mock()
    user.id = uuid4()
    return user


@asynccontextmanager
async def _session_cm(db):
    yield db


class TestThreadOwnership:
    """Bug 1 — thread lookup must enforce workspace ownership."""

    async def test_foreign_thread_id_is_not_resolved(self):
        from src.api.agent.execute import AgentExecuteRequest
        from src.services.agent.agent_execution_service import (
            AgentThreadResolutionError,
            _resolve_thread,
        )

        user = _make_user()
        foreign_thread_id = str(uuid4())
        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=Mock(return_value=None))
        )
        db.add = Mock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        request = AgentExecuteRequest(
            messages=[{"role": "user", "content": "hello"}],
            thread_id=foreign_thread_id,
        )

        # The access funnel hides a foreign thread exactly like a missing one.
        # Explicit IDs are authoritative, so the resolver must deny instead of
        # silently degrading to an ephemeral checkpoint.
        with (
            patch(
                "src.services.threads.workspace_access.get_thread",
                new=AsyncMock(return_value=None),
            ),
            pytest.raises(AgentThreadResolutionError, match="Thread not found"),
        ):
            await _resolve_thread(db, user, request, create_if_missing=False)

        db.commit.assert_not_called()


class TestHitlCheckpointOwnership:
    """HITL resume must fail closed when checkpoint ownership is missing."""

    async def test_resume_rejects_legacy_checkpoint_when_thread_not_owned(self):
        from src.api.agent.execute import AgentExecuteRequest, _get_job, _set_job
        from src.services.agent.agent_execution_service import _resume_agent_graph

        job_id = str(uuid4())
        current_user = _make_user()
        foreign_thread_id = str(uuid4())

        _set_job(
            job_id,
            {
                "status": "awaiting_confirmation",
                "tool_executions": [],
                "user_id": str(current_user.id),
                "request": AgentExecuteRequest(
                    messages=[{"role": "user", "content": "ingest this paper"}],
                    thread_id=foreign_thread_id,
                ).model_dump(),
            },
        )

        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=Mock(return_value=None))
        )

        mock_graph = MagicMock()
        mock_graph.aget_state = AsyncMock(
            return_value=MagicMock(
                values={
                    "pending_confirmation": {
                        "tools": [{"name": "ingest_arxiv_papers", "args": {}}]
                    },
                    # Legacy checkpoint: no user_id in state.
                }
            )
        )
        mock_graph.ainvoke = AsyncMock()

        with (
            patch(
                "src.services.agent.checkpointer.get_checkpointer",
                new_callable=AsyncMock,
            ),
            patch(
                "src.services.agent.memory.get_memory_store",
                new_callable=AsyncMock,
            ),
            patch(
                "src.services.agent.graph.compile_agent_graph",
                return_value=mock_graph,
            ),
            patch(
                "src.services.agent.agent_execution_service.AsyncSessionLocal",
                return_value=_session_cm(db),
            ),
            patch(
                "src.services.agent.agent_run_service.record_job_status",
                new_callable=AsyncMock,
            ),
        ):
            await _resume_agent_graph(job_id, True, current_user)

        mock_graph.ainvoke.assert_not_called()
        job = _get_job(job_id)
        assert job is not None
        assert job["status"] == "failed"  # legacy "error" collapsed (audit C7)
        assert job["error"] == "Thread not found"


class TestStreamingGraphInterrupt:
    """Bug 2 — GraphInterrupt handler must not KeyError on empty config."""

    async def test_graph_interrupt_before_config_populated_emits_confirmation(self):
        from src.api.agent.execute import AgentExecuteRequest
        from src.api.agent.streaming import stream_event_generator

        request_body = AgentExecuteRequest(
            messages=[{"role": "user", "content": "delete everything"}],
        )
        http_request = AsyncMock()
        http_request.is_disconnected = AsyncMock(return_value=False)

        confirmation = {"tools": [{"name": "ingest_arxiv_papers", "args": {}}]}

        class _InterruptStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise GraphInterrupt(interrupts=[MagicMock(value=confirmation)])

        mock_graph = MagicMock()
        mock_graph.astream_events = MagicMock(return_value=_InterruptStream())
        mock_graph.aget_state = AsyncMock(
            return_value=MagicMock(
                values={"pending_confirmation": confirmation},
                tasks=[MagicMock(interrupts=[MagicMock()])],
            )
        )
        mock_db = AsyncMock()
        mock_db.close = AsyncMock()

        with (
            patch(
                "src.services.agent.checkpointer.get_checkpointer",
                new_callable=AsyncMock,
            ),
            patch(
                "src.services.agent.memory.get_memory_store",
                new_callable=AsyncMock,
            ),
            patch(
                "src.services.agent.graph.compile_agent_graph",
                return_value=mock_graph,
            ),
            patch(
                "src.api.agent.streaming.AsyncSessionLocal",
                return_value=mock_db,
            ),
            patch(
                "src.api.agent.streaming._resolve_thread",
                new_callable=AsyncMock,
                return_value=(None, ""),
            ),
            patch(
                "src.api.agent.streaming._clear_stale_pending_confirmation",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            events = [
                chunk
                async for chunk in stream_event_generator(
                    request_body, http_request, _make_user()
                )
            ]

        assert any("event: confirmation" in e for e in events)
        assert not any("event: error" in e for e in events)


class TestBackgroundTimeout:
    """Bug 5 — background graph.ainvoke must time out after 360s."""

    async def test_run_agent_graph_marks_failed_on_timeout(self):
        from src.api.agent.execute import AgentExecuteRequest, _get_job, _set_job
        from src.services.agent.agent_execution_service import _run_agent_graph

        job_id = str(uuid4())
        user = _make_user()
        db = AsyncMock()

        _set_job(
            job_id,
            {
                "status": "running",
                "tool_executions": [],
                "user_id": str(user.id),
                "request": {
                    "messages": [{"role": "user", "content": "hi"}],
                    "page_context": {"type": "unknown"},
                    "model": "gpt-4o",
                    "use_rag": True,
                    "max_context_docs": 5,
                },
            },
        )

        request = AgentExecuteRequest(
            messages=[{"role": "user", "content": "hi"}],
            page_context={"type": "unknown"},
        )

        async def hang_forever(*_args, **_kwargs):
            await asyncio.sleep(9999)

        mock_graph = MagicMock()
        mock_graph.ainvoke = hang_forever

        with (
            patch(
                "src.services.agent.checkpointer.get_checkpointer",
                new_callable=AsyncMock,
            ),
            patch(
                "src.services.agent.graph.compile_agent_graph",
                return_value=mock_graph,
            ),
            patch(
                "src.services.agent.agent_execution_service.AsyncSessionLocal",
                return_value=_session_cm(db),
            ),
            patch(
                "src.services.agent.agent_execution_service._resolve_thread",
                new_callable=AsyncMock,
                return_value=(None, ""),
            ),
            patch(
                "src.services.agent.agent_execution_service.asyncio.timeout"
            ) as mock_timeout,
        ):
            mock_timeout.return_value.__aenter__ = AsyncMock(
                side_effect=asyncio.TimeoutError()
            )
            mock_timeout.return_value.__aexit__ = AsyncMock(return_value=False)
            await _run_agent_graph(job_id, request, user)

        job = _get_job(job_id)
        assert job is not None
        assert job["status"] == "failed"
        assert "timed out" in job["error"].lower()


class TestCheckpointerFallbackLog:
    """Bug 8 — MemorySaver fallback must log at ERROR."""

    async def test_postgres_failure_logs_error(self, caplog):
        from src.services.agent import checkpointer

        checkpointer._checkpointer = None

        with (
            patch(
                "src.services.agent._pool_utils.get_shared_langgraph_pool",
                new_callable=AsyncMock,
                side_effect=RuntimeError("connection refused"),
            ),
            caplog.at_level(logging.ERROR),
        ):
            cp = await checkpointer.get_checkpointer()

        assert cp is not None
        assert any(
            "MemorySaver" in r.message and r.levelno >= logging.ERROR
            for r in caplog.records
        )

        checkpointer._checkpointer = None


class TestFilteredToolNodeErrorInfo:
    """Bug 6 — filtered_tool_node must propagate last_error_info."""

    async def test_filtered_tool_node_returns_last_error_info(self):
        from langchain_core.messages import AIMessage, ToolMessage

        from src.services.agent.graph import make_filtered_tool_node

        node = make_filtered_tool_node({"search_arxiv"})
        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {"id": "tc1", "name": "search_arxiv", "args": {"query": "x"}},
            ],
        )
        state = {
            "messages": [ai_msg],
            "tool_executions": [],
            "error_count": 0,
            "last_error": "",
            "last_error_info": {},
            "page_context": {},
            "tool_loop_count": 0,
        }
        config = {
            "configurable": {
                "user_id": str(uuid4()),
                "organization_id": str(uuid4()),
            }
        }

        error_info = {
            "category": "transient",
            "message": "timeout",
            "suggestion": "retry",
        }

        with patch(
            "src.services.agent._nodes_tools._execute_single_tool",
            new_callable=AsyncMock,
            return_value={
                "message": ToolMessage(
                    content='{"error": "timeout"}', tool_call_id="tc1"
                ),
                "execution": {
                    "id": "tc1",
                    "tool_name": "search_arxiv",
                    "tool_display_name": "Search Arxiv",
                    "args": {},
                    "status": "failed",
                    "result": {},
                    "duration_ms": 1,
                },
                "error_increment": 1,
                "error_text": "timeout",
                "error_info": error_info,
            },
        ):
            result = await node(state, config)

        assert result["last_error_info"] == error_info
