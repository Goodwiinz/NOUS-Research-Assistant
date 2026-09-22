"""Integration tests for agent API endpoints.

Tests the HTTP-level behavior of agent endpoints including
job ownership, persistence, and input validation.
Uses FastAPI TestClient with dependency overrides.
"""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.agent.execute import (
    AgentExecuteRequest,
    ConfirmationRequest,
    _get_job,
    _set_job,
    router,
)
from tests.utils.agent_thread_access import editable_thread_getter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TEST_ORG_ID = "11111111-1111-1111-1111-111111111111"
_TEST_USER_A_ID = "22222222-2222-2222-2222-222222222222"
_TEST_USER_B_ID = "33333333-3333-3333-3333-333333333333"


def _make_mock_user(
    user_id: str = _TEST_USER_A_ID,
    organization_id: str = _TEST_ORG_ID,
):
    user = Mock()
    user.id = user_id
    user.email = "test@example.com"
    user.first_name = "Test"
    user.last_name = "User"
    user.role = Mock(value="user")
    user.organization_id = organization_id
    user.is_active = True
    return user


def _make_mock_db():
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=Mock(return_value=None))
    )
    db.add = Mock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.begin = AsyncMock()
    db.get = AsyncMock(return_value=None)
    return db


@pytest.fixture
def mock_user_a():
    return _make_mock_user(_TEST_USER_A_ID)


@pytest.fixture
def mock_user_b():
    return _make_mock_user(_TEST_USER_B_ID)


@pytest.fixture
def app_with_overrides(mock_user_a):
    """Create a FastAPI app with the agent router and dependency overrides."""
    from src.core.database import get_db
    from src.core.dependencies import get_current_user

    app = FastAPI()
    app.include_router(router)

    async def override_get_current_user():
        return mock_user_a

    async def override_get_db():
        return _make_mock_db()

    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_db] = override_get_db

    return app


@pytest.fixture
def client(app_with_overrides):
    @asynccontextmanager
    async def _no_lifespan(_app):
        yield

    app_with_overrides.router.lifespan_context = _no_lifespan
    with TestClient(app_with_overrides) as c:
        yield c


# ---------------------------------------------------------------------------
# Job Ownership Tests
# ---------------------------------------------------------------------------


class TestJobOwnershipEndpoints:
    """Integration tests for job ownership enforcement on API endpoints."""

    def test_get_job_status_returns_404_for_wrong_user(self, client, mock_user_a):
        """GET /jobs/{id} should return 404 if job belongs to different user."""
        job_id = str(uuid4())
        _set_job(
            job_id,
            {
                "status": "completed",
                "result": {"message": {"role": "assistant", "content": "hi"}},
                "tool_executions": [],
                "user_id": "user-zzz",  # Different user
            },
        )

        response = client.get(f"/api/v1/agent/jobs/{job_id}")
        assert response.status_code == 404

    def test_get_job_status_returns_job_for_owner(self, client, mock_user_a):
        """GET /jobs/{id} should return the job if user owns it."""
        job_id = str(uuid4())
        _set_job(
            job_id,
            {
                "status": "running",
                "tool_executions": [],
                "user_id": str(mock_user_a.id),
            },
        )

        response = client.get(f"/api/v1/agent/jobs/{job_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"

    def test_get_job_status_returns_404_for_nonexistent(self, client):
        """GET /jobs/{id} should return 404 for nonexistent job."""
        response = client.get(f"/api/v1/agent/jobs/{uuid4()}")
        assert response.status_code == 404

    def test_confirm_action_returns_404_for_wrong_user(self, client, mock_user_a):
        """POST /confirm/{id} should return 404 if job belongs to different user."""
        job_id = str(uuid4())
        _set_job(
            job_id,
            {
                "status": "awaiting_confirmation",
                "confirmation": {"tools": [], "message": "Confirm?"},
                "tool_executions": [],
                "user_id": "user-zzz",
            },
        )

        response = client.post(
            f"/api/v1/agent/confirm/{job_id}",
            json={"confirmed": True},
        )
        assert response.status_code == 404

    def test_confirm_action_works_for_owner(self, client, mock_user_a):
        """POST /confirm/{id} should succeed for the job owner."""
        job_id = str(uuid4())
        _set_job(
            job_id,
            {
                "status": "awaiting_confirmation",
                "confirmation": {"tools": [], "message": "Confirm?"},
                "tool_executions": [],
                "user_id": str(mock_user_a.id),
            },
        )

        with patch(
            "src.api.agent.execute._resume_agent_graph",
            new_callable=AsyncMock,
        ):
            response = client.post(
                f"/api/v1/agent/confirm/{job_id}",
                json={"confirmed": True},
            )
        assert response.status_code == 200
        assert response.json()["status"] == "running"

    def test_confirm_action_rejects_non_awaiting_job(self, client, mock_user_a):
        """POST /confirm/{id} should return 409 if job is not awaiting confirmation."""
        job_id = str(uuid4())
        _set_job(
            job_id,
            {
                "status": "running",
                "tool_executions": [],
                "user_id": str(mock_user_a.id),
            },
        )

        response = client.post(
            f"/api/v1/agent/confirm/{job_id}",
            json={"confirmed": True},
        )
        assert response.status_code == 409


class TestJobOwnershipFailClosed:
    """Ownerless job records must NOT be accessible."""

    def test_job_without_user_id_returns_404(self, client):
        """Fail closed on a missing user_id.

        The old guard skipped the ownership check when user_id was absent,
        which let any authenticated user read another user's completed job
        (terminal status writes used to drop user_id from the record). Every
        write path now stamps the owner, so an ownerless record means
        corruption or a pre-fix legacy entry — treat it as not found.
        Records expire after 1h, so locking out legacy entries is a
        one-time, bounded cost.
        """
        job_id = str(uuid4())
        _set_job(
            job_id,
            {
                "status": "running",
                "tool_executions": [],
                # No user_id — ownerless record
            },
        )

        response = client.get(f"/api/v1/agent/jobs/{job_id}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Execute Endpoint Tests
# ---------------------------------------------------------------------------


class TestExecuteEndpoint:
    """Integration tests for POST /execute."""

    def test_execute_returns_job_id(self, client):
        """POST /execute should return a job_id immediately."""
        with patch(
            "src.api.agent.execute._run_agent_graph",
            new_callable=AsyncMock,
        ):
            response = client.post(
                "/api/v1/agent/execute",
                json={
                    "messages": [{"role": "user", "content": "hello"}],
                    "page_context": {"type": "unknown"},
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data
        assert len(data["job_id"]) > 0

    def test_execute_stores_user_id_in_job(self, client, mock_user_a):
        """POST /execute should store user_id in the created job."""
        with patch(
            "src.api.agent.execute._run_agent_graph",
            new_callable=AsyncMock,
        ):
            response = client.post(
                "/api/v1/agent/execute",
                json={
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        job_id = response.json()["job_id"]
        job = _get_job(job_id)
        assert job is not None
        assert job["user_id"] == str(mock_user_a.id)

    def test_execute_stores_request_in_job(self, client):
        """POST /execute should store the request for later resume persistence."""
        with patch(
            "src.api.agent.execute._run_agent_graph",
            new_callable=AsyncMock,
        ):
            response = client.post(
                "/api/v1/agent/execute",
                json={
                    "messages": [{"role": "user", "content": "find papers on ML"}],
                    "page_context": {"type": "project", "project_id": "proj-1"},
                },
            )

        job_id = response.json()["job_id"]
        job = _get_job(job_id)
        assert job.get("request") is not None
        assert job["request"]["messages"][0]["content"] == "find papers on ML"
        assert job["request"]["page_context"]["type"] == "project"


class TestResumePersistence:
    """Test that _resume_agent_graph persists the resumed assistant turn.

    The confirm path persists ONLY the assistant row (the user row was written
    up-front by the initial /execute run). It resolves the thread via
    ``db.get(Thread, ...)`` and returns that thread's ids in the response — it
    no longer round-trips through the removed ``_persist_thread_messages`` shim.
    """

    @pytest.fixture(autouse=True)
    def _allow_durable_thread_access(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "src.services.threads.workspace_access.get_thread",
            editable_thread_getter(),
        )

    async def test_resume_persists_assistant_only(self):
        """After graph.ainvoke, the assistant row is persisted and the user
        row is NOT re-persisted on the confirm path."""
        from langchain_core.messages import AIMessage

        from src.api.agent.execute import _resume_agent_graph

        job_id = str(uuid4())
        thread_id = str(uuid4())
        user = _make_mock_user("user-111")
        db = _make_mock_db()
        db.get = AsyncMock(
            return_value=SimpleNamespace(id=thread_id, conversation_id="conv-1")
        )

        _set_job(
            job_id,
            {
                "status": "awaiting_confirmation",
                "tool_executions": [],
                "user_id": str(user.id),
                "request": {
                    "messages": [{"role": "user", "content": "ingest paper"}],
                    "page_context": {"type": "unknown"},
                    "model": "model-router",
                    "use_rag": True,
                    "max_context_docs": 5,
                    "thread_id": thread_id,
                },
            },
        )

        mock_final_state = {
            "messages": [AIMessage(content="Done, paper ingested.")],
            "tool_executions": [],
        }

        @asynccontextmanager
        async def _mock_async_session():
            yield db

        with (
            patch(
                "src.services.agent.checkpointer.get_checkpointer",
                new_callable=AsyncMock,
            ),
            patch(
                "src.services.agent.graph.compile_agent_graph",
            ) as mock_compile,
            patch(
                "src.services.agent.agent_execution_service._persist_assistant_message_safe",
                new_callable=AsyncMock,
                return_value="assistant-row-1",
            ) as mock_persist_assistant,
            patch(
                "src.services.agent.agent_execution_service._persist_user_message",
                new_callable=AsyncMock,
            ) as mock_persist_user,
            patch(
                "src.services.agent.agent_execution_service.AsyncSessionLocal",
                return_value=_mock_async_session(),
            ),
            patch(
                "src.services.agent.agent_run_service.record_job_status",
                new_callable=AsyncMock,
            ),
        ):
            mock_graph = MagicMock()
            mock_graph.ainvoke = AsyncMock(return_value=mock_final_state)
            mock_graph.aget_state = AsyncMock(return_value=None)
            mock_compile.return_value = mock_graph

            await _resume_agent_graph(job_id, True, user)

        mock_persist_assistant.assert_awaited_once()
        assert (
            mock_persist_assistant.await_args.kwargs["content"]
            == "Done, paper ingested."
        )
        # The user row is never re-persisted on the confirm path.
        mock_persist_user.assert_not_awaited()

    async def test_resume_uses_original_thread_id_when_present(self):
        """Resume should use the stored request thread_id, not the transient job id."""
        from langchain_core.messages import AIMessage

        from src.api.agent.execute import _resume_agent_graph

        job_id = str(uuid4())
        thread_id = str(uuid4())
        user = _make_mock_user("user-222")
        db = _make_mock_db()
        db.get = AsyncMock(
            return_value=SimpleNamespace(id=thread_id, conversation_id="conv-1")
        )

        _set_job(
            job_id,
            {
                "status": "awaiting_confirmation",
                "tool_executions": [],
                "user_id": str(user.id),
                "request": {
                    "messages": [{"role": "user", "content": "confirm ingest"}],
                    "page_context": {"type": "project", "project_id": "proj-1"},
                    "model": "model-router",
                    "use_rag": True,
                    "max_context_docs": 5,
                    "thread_id": thread_id,
                },
            },
        )

        mock_final_state = {
            "messages": [AIMessage(content="Confirmed.")],
            "tool_executions": [],
        }

        @asynccontextmanager
        async def _mock_async_session():
            yield db

        with (
            patch(
                "src.services.agent.checkpointer.get_checkpointer",
                new_callable=AsyncMock,
            ),
            patch(
                "src.services.agent.graph.compile_agent_graph",
            ) as mock_compile,
            patch(
                "src.services.agent.agent_execution_service._persist_assistant_message_safe",
                new_callable=AsyncMock,
                return_value="assistant-row-1",
            ),
            patch(
                "src.services.agent.agent_execution_service.AsyncSessionLocal",
                return_value=_mock_async_session(),
            ),
            patch(
                "src.services.agent.agent_run_service.record_job_status",
                new_callable=AsyncMock,
            ),
        ):
            mock_graph = MagicMock()
            mock_graph.ainvoke = AsyncMock(return_value=mock_final_state)
            mock_graph.aget_state = AsyncMock(return_value=None)
            mock_compile.return_value = mock_graph

            await _resume_agent_graph(job_id, True, user)

        config = mock_graph.ainvoke.call_args.kwargs["config"]
        assert config["configurable"]["thread_id"] == thread_id

    async def test_resume_result_includes_persisted_thread_ids(self):
        """Resume responses return the thread and conversation IDs resolved
        from the thread row."""
        from langchain_core.messages import AIMessage

        from src.api.agent.execute import _resume_agent_graph

        job_id = str(uuid4())
        thread_id = str(uuid4())
        user = _make_mock_user("user-333")
        db = _make_mock_db()
        db.get = AsyncMock(
            return_value=SimpleNamespace(id=thread_id, conversation_id="conv-77")
        )

        _set_job(
            job_id,
            {
                "status": "awaiting_confirmation",
                "tool_executions": [],
                "user_id": str(user.id),
                "request": {
                    "messages": [{"role": "user", "content": "save this result"}],
                    "page_context": {"type": "unknown"},
                    "model": "model-router",
                    "use_rag": True,
                    "max_context_docs": 5,
                    "thread_id": thread_id,
                },
            },
        )

        mock_final_state = {
            "messages": [AIMessage(content="Saved.")],
            "tool_executions": [],
        }

        @asynccontextmanager
        async def _mock_async_session():
            yield db

        with (
            patch(
                "src.services.agent.checkpointer.get_checkpointer",
                new_callable=AsyncMock,
            ),
            patch(
                "src.services.agent.graph.compile_agent_graph",
            ) as mock_compile,
            patch(
                "src.services.agent.agent_execution_service._persist_assistant_message_safe",
                new_callable=AsyncMock,
                return_value="assistant-row-1",
            ),
            patch(
                "src.services.agent.agent_execution_service.AsyncSessionLocal",
                return_value=_mock_async_session(),
            ),
            patch(
                "src.services.agent.agent_run_service.record_job_status",
                new_callable=AsyncMock,
            ),
        ):
            mock_graph = MagicMock()
            mock_graph.ainvoke = AsyncMock(return_value=mock_final_state)
            mock_graph.aget_state = AsyncMock(return_value=None)
            mock_compile.return_value = mock_graph

            await _resume_agent_graph(job_id, True, user)

        job = _get_job(job_id)
        assert job["status"] == "completed"
        assert job["result"]["thread_id"] == thread_id
        assert job["result"]["conversation_id"] == "conv-77"


# ---------------------------------------------------------------------------
# SSE Stream Persistence Tests
# ---------------------------------------------------------------------------


class TestSSEStreamPersistence:
    """Test that the SSE /stream endpoint persists messages after completion."""

    async def test_stream_endpoint_persists_messages(self):
        """event_generator should persist user up-front and assistant post-stream.

        Task 4 of docs/plans/2026-05-13-agent-persist-perf.md split the old
        single ``_persist_thread_messages`` call into two phases: the user row
        is written before the LLM call, the assistant row after the stream
        finishes. This structural test pins the new contract.
        """
        # We'll test the event_generator logic by verifying the persistence
        # call is present in the source code (structural test), since
        # actually invoking the full SSE pipeline requires a real graph.
        import inspect

        from langchain_core.messages import AIMessage

        from src.api.agent.streaming import stream_event_generator

        source = inspect.getsource(stream_event_generator)
        assert "_resolve_thread" in source
        assert "_persist_user_message" in source
        assert "_persist_assistant_message" in source
        assert "aget_state" in source

    async def test_stream_has_timeout(self):
        """event_generator should wrap astream_events with asyncio.timeout."""
        import inspect

        from src.api.agent.streaming import stream_event_generator

        source = inspect.getsource(stream_event_generator)
        assert "asyncio.timeout" in source

    def test_stream_endpoint_passes_full_page_context(self, client):
        """SSE /stream forwards page context, with project fields fail-closed.

        Since the thread-project binding work (#664/#666),
        _resolve_and_bind_project ownership-verifies any client-sent
        project_id and DROPS it (None) when no owned project matches — the
        client's claim is never trusted verbatim. With no DB rows behind the
        mocks, the sent proj-123 is unverifiable, so the graph must receive
        project_id/project_name as None while every other field passes
        through untouched.
        """
        from langchain_core.messages import AIMessage

        payload = {
            "messages": [{"role": "user", "content": "summarize the project"}],
            "page_context": {
                "type": "project",
                "project_id": "proj-123",
                "project_name": "Atlas",
                "label": "Notes",
                "metadata": {"active_tab": "notes", "source": "sidebar"},
            },
            "model": "model-router",
            "use_rag": True,
            "max_context_docs": 5,
            "attachment_ids": ["44444444-4444-4444-8444-444444444444"],
        }

        async def _empty_events():
            if False:
                yield {}

        snapshot = SimpleNamespace(
            values={
                "messages": [AIMessage(content="Project summary")],
                "tool_executions": [],
            },
            tasks=(),
        )

        with (
            patch(
                "src.services.agent.checkpointer.get_checkpointer",
                new_callable=AsyncMock,
            ),
            patch(
                "src.services.agent.graph.compile_agent_graph",
            ) as mock_compile,
            patch(
                "src.api.agent.streaming._resolve_thread",
                new=AsyncMock(return_value=(None, None)),
            ),
        ):
            mock_graph = MagicMock()
            mock_graph.astream_events = Mock(
                side_effect=lambda *args, **kwargs: _empty_events()
            )
            mock_graph.aget_state = AsyncMock(return_value=snapshot)
            mock_compile.return_value = mock_graph

            with client.stream(
                "POST", "/api/v1/agent/stream", json=payload
            ) as response:
                body = "".join(response.iter_text())

        assert response.status_code == 200
        assert "event: done" in body

        initial_state = mock_graph.astream_events.call_args.args[0]
        config = mock_graph.astream_events.call_args.kwargs["config"]
        # Unverifiable client project claim is dropped (fail-closed); the
        # rest of the context passes through unchanged.
        expected_context = {
            **payload["page_context"],
            "project_id": None,
            "project_name": None,
        }
        assert initial_state["page_context"] == expected_context
        assert initial_state["attachment_ids"] == payload["attachment_ids"]
        assert initial_state["attachment_status"] == []
        assert config["configurable"]["page_context"] == expected_context

    def test_stream_confirm_persists_resumed_messages(self, client, mock_user_a):
        """SSE /stream/confirm should persist the resumed assistant turn.

        The confirm path persists ONLY the assistant row (the user row was
        already persisted up-front on the original /stream request). It must
        not re-insert the user row — that was the duplicate-user-message bug.
        """
        from langchain_core.messages import AIMessage, HumanMessage

        thread_id = str(uuid4())
        payload = {"thread_id": thread_id, "confirmed": True}

        async def _empty_events():
            if False:
                yield {}

        snapshot = SimpleNamespace(
            values={
                "user_id": str(mock_user_a.id),
                "messages": [
                    HumanMessage(content="ingest this paper"),
                    AIMessage(content="The paper was ingested."),
                ],
                "tool_executions": [],
                "page_context": {
                    "type": "project",
                    "project_id": "proj-9",
                    "project_name": "Atlas",
                    "label": "Documents",
                    "metadata": {"active_tab": "documents"},
                },
            },
            tasks=(),
        )

        with (
            patch(
                "src.services.agent.checkpointer.get_checkpointer",
                new_callable=AsyncMock,
            ),
            patch(
                "src.services.agent.graph.compile_agent_graph",
            ) as mock_compile,
            patch(
                "src.services.agent.agent_execution_service._persist_assistant_message_safe",
                new_callable=AsyncMock,
                return_value="assistant-row-id",
            ) as mock_persist_assistant,
            patch(
                "src.services.agent.agent_execution_service._persist_user_message",
                new_callable=AsyncMock,
            ) as mock_persist_user,
            patch(
                "src.api.agent.streaming.get_active_run_for_thread",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(
                    job_id="run-1",
                    user_message_id=None,
                    client_message_id=None,
                ),
            ) as mock_active_run,
            patch(
                "src.api.agent.streaming.claim_awaiting_run_for_confirmation",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_claim,
            patch(
                "src.api.agent.streaming._finalize_run_id",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "src.services.threads.workspace_access.get_thread",
                new=editable_thread_getter(),
            ),
        ):
            mock_graph = MagicMock()
            mock_graph.astream_events = Mock(
                side_effect=lambda *args, **kwargs: _empty_events()
            )
            mock_graph.aget_state = AsyncMock(return_value=snapshot)
            mock_compile.return_value = mock_graph

            with client.stream(
                "POST", "/api/v1/agent/stream/confirm", json=payload
            ) as response:
                body = "".join(response.iter_text())

        assert response.status_code == 200
        assert "event: done" in body
        # Assistant row must be persisted (assistant-only — no user row).
        mock_persist_assistant.assert_called_once()
        assert mock_persist_assistant.call_args.kwargs["thread_id"] == thread_id
        assert (
            mock_persist_assistant.call_args.kwargs["content"]
            == "The paper was ingested."
        )
        # The user row must NOT be re-persisted on the confirm path.
        mock_persist_user.assert_not_called()
        mock_active_run.assert_awaited_once()
        active_run_call = mock_active_run.await_args
        assert active_run_call.args[1] == thread_id
        assert active_run_call.kwargs == {
            "organization_id": mock_user_a.organization_id,
            "user_id": mock_user_a.id,
        }
        mock_claim.assert_awaited_once()
        assert mock_claim.await_args.args[1] == "run-1"
        assert mock_claim.await_args.kwargs == {
            "organization_id": mock_user_a.organization_id,
            "user_id": mock_user_a.id,
        }

    def test_stream_confirm_rejects_snapshot_without_user_id(self, client):
        """SSE /stream/confirm must not resume legacy ownerless checkpoints."""
        thread_id = str(uuid4())
        payload = {"thread_id": thread_id, "confirmed": True}

        snapshot = SimpleNamespace(
            values={
                "messages": [],
                "tool_executions": [],
                "page_context": {"type": "general"},
            },
            tasks=(),
        )

        with (
            patch(
                "src.services.agent.checkpointer.get_checkpointer",
                new_callable=AsyncMock,
            ),
            patch(
                "src.services.agent.graph.compile_agent_graph",
            ) as mock_compile,
            patch(
                "src.services.threads.workspace_access.get_thread",
                new=editable_thread_getter(),
            ),
        ):
            mock_graph = MagicMock()
            mock_graph.astream_events = Mock()
            mock_graph.aget_state = AsyncMock(return_value=snapshot)
            mock_compile.return_value = mock_graph

            with client.stream(
                "POST", "/api/v1/agent/stream/confirm", json=payload
            ) as response:
                body = "".join(response.iter_text())

        assert response.status_code == 200
        assert "Thread not found" in body
        mock_graph.astream_events.assert_not_called()
