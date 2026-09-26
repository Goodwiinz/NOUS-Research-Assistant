"""Regression test for the HITL JOB confirm-path persistence fix (S2).

The job/poll confirm path (``_resume_agent_graph``) used to call the
deprecated ``_persist_thread_messages`` shim, which re-inserted a bare user
row on every confirm (no client_message_id -> no dedup -> inflated
message_count) and wrote the assistant row with no idempotency key / plan /
token_usage. The user row was already persisted up-front by the initial
/execute run, so the confirm path must persist ONLY the assistant row —
mirroring the SSE confirm path (streaming.py ``_resume_assistant_cmid``).
"""

import json
import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, AsyncContextManager, AsyncIterator, cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from tests.utils.agent_thread_access import editable_thread_getter


@pytest.fixture(autouse=True)
def _allow_durable_thread_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.services.threads.workspace_access.get_thread",
        editable_thread_getter(),
    )


def _interrupt_task() -> SimpleNamespace:
    return SimpleNamespace(interrupts=[SimpleNamespace(value={"tools": []})])


class _FakeGraph:
    """``aget_state`` returns a pre-resume snapshot (live interrupt + checkpoint
    id) on the first call and a resolved snapshot (no interrupt) afterwards, so
    the resume advances past the interrupt check into persistence."""

    def __init__(self, pre_snapshot: Any, post_snapshot: Any, final_state: Any) -> None:
        self._pre = pre_snapshot
        self._post = post_snapshot
        self._get_state_calls = 0
        self.ainvoke = AsyncMock(return_value=final_state)

    async def aget_state(self, config: Any) -> Any:
        self._get_state_calls += 1
        return self._pre if self._get_state_calls == 1 else self._post


class _PublicationRedis:
    """Small WATCH/MULTI store matching redis.asyncio's pipeline API."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.pipeline_calls: list["_PublicationPipeline"] = []

    def pipeline(self, *, transaction: bool) -> "_PublicationPipeline":
        assert transaction is True
        pipeline = _PublicationPipeline(self)
        self.pipeline_calls.append(pipeline)
        return pipeline


class _PublicationPipeline:
    def __init__(self, redis_client: "_PublicationRedis") -> None:
        self.redis_client = redis_client
        self.queued: tuple[str, int, str] | None = None
        self.watched_key: str | None = None
        self.multi_called = False
        self.executed = False

    async def __aenter__(self) -> "_PublicationPipeline":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        return None

    async def watch(self, key: str) -> None:
        self.watched_key = key

    async def get(self, key: str) -> str | None:
        return self.redis_client.values.get(key)

    def multi(self) -> None:
        self.multi_called = True

    def setex(self, key: str, ttl: int, value: str) -> "_PublicationPipeline":
        assert self.watched_key == key
        assert self.multi_called
        self.queued = (key, ttl, value)
        return self

    async def execute(self) -> list[bool]:
        assert self.queued is not None
        key, ttl, value = self.queued
        assert ttl > 0
        self.redis_client.values[key] = value
        self.executed = True
        return [True]

    async def reset(self) -> None:
        self.queued = None


@pytest.mark.asyncio
async def test_job_confirm_persists_assistant_only_with_checkpoint_cmid() -> None:
    from src.api.agent.execute import AgentExecuteRequest, AgentMessage, _set_job
    from src.services.agent import agent_execution_service
    from src.services.agent.agent_execution_service import _resume_agent_graph
    from src.services.agent.agent_run_service import RunStatusDecision
    from src.services.agent.tools import TOOL_REGISTRY
    from src.shared.enums import JobStatus

    user = Mock()
    user.id = str(uuid4())
    user.organization_id = "org-1"

    thread_id = str(uuid4())
    conversation_id = str(uuid4())
    job_id = str(uuid4())

    with patch.object(agent_execution_service, "_write_to_redis_only", new=AsyncMock()):
        _set_job(
            job_id,
            {
                "status": "awaiting_confirmation",
                "tool_executions": [],
                "user_id": str(user.id),
                "request": AgentExecuteRequest(
                    messages=[AgentMessage(role="user", content="ingest this paper")],
                    thread_id=thread_id,
                    model="model-router",
                ).model_dump(),
            },
            project=False,
        )

    runtime_snapshot_id = uuid4()
    registry_metadata = TOOL_REGISTRY.metadata_snapshot()
    runtime_snapshot_row = SimpleNamespace(
        id=runtime_snapshot_id,
        user_id=_uuid.UUID(str(user.id)),
        project_id=None,
        thread_id=_uuid.UUID(thread_id),
        job_id=job_id,
        tool_registry_hash=registry_metadata["hash"],
        tool_registry_version=registry_metadata["version"],
        tool_metadata={"descriptors": TOOL_REGISTRY.frozen_descriptor_metadata()},
        skill_catalog=[],
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    pre_snapshot = SimpleNamespace(
        values={
            "user_id": str(user.id),
            "runtime_snapshot_id": str(runtime_snapshot_id),
            "thread_id": thread_id,
            "current_project_id": "",
        },
        tasks=(_interrupt_task(),),
        config={"configurable": {"checkpoint_id": "ckpt-1"}},
    )
    post_snapshot = SimpleNamespace(
        values={"user_id": str(user.id)},
        tasks=(),
        config={"configurable": {}},
    )
    final_state = {
        "messages": [
            AIMessage(
                content="Done - the paper was ingested.",
                usage_metadata={
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "total_tokens": 18,
                },
            )
        ],
        "tool_executions": [],
        "retrieved_contexts": [],
        "plan": [{"step": 1, "description": "Ingest", "tool": "ingest_arxiv"}],
    }
    graph = _FakeGraph(pre_snapshot, post_snapshot, final_state)

    thread_row = SimpleNamespace(id=thread_id, conversation_id=conversation_id)
    durable_run = SimpleNamespace(
        job_id=job_id,
        thread_id=thread_id,
        client_message_id="original-user-turn-cmid",
        user_message_id="original-user-message",
    )
    sessions: list[AsyncMock] = []

    @asynccontextmanager
    async def _session_cm(db: AsyncMock) -> AsyncIterator[AsyncMock]:
        try:
            yield db
        finally:
            await db.close()

    def _new_session() -> AsyncContextManager[AsyncMock]:
        db = AsyncMock()
        db.get = AsyncMock(return_value=thread_row)
        db.get = AsyncMock(
            side_effect=lambda model, _id: (
                runtime_snapshot_row
                if model.__name__ == "AgentRuntimeSnapshot"
                else thread_row
            )
        )
        db.execute = AsyncMock(
            return_value=SimpleNamespace(
                scalar_one_or_none=lambda: durable_run,
                one_or_none=lambda: None,
            )
        )
        sessions.append(db)
        # The async-generator context manager wraps this deliberately loose
        # AsyncMock session double at the test boundary.
        return cast(AsyncContextManager[AsyncMock], _session_cm(db))

    decision = RunStatusDecision(
        job_id=job_id,
        requested_status=JobStatus.COMPLETED,
        effective_status=JobStatus.COMPLETED,
        user_id=str(user.id),
        organization_id=str(user.organization_id),
        thread_id=thread_id,
        error=None,
        cancel_requested_at=None,
        updated_at="2026-09-26T00:00:00+00:00",
    )
    record_job_status = AsyncMock(return_value=decision)
    redis_client = _PublicationRedis()

    persist_assistant = AsyncMock(return_value="assistant-row-1")
    persist_user = AsyncMock()

    with (
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.graph.compile_agent_graph",
            return_value=graph,
        ),
        patch(
            "src.services.agent.agent_execution_service.AsyncSessionLocal",
            side_effect=_new_session,
        ),
        patch(
            "src.services.agent.agent_run_service.record_job_status",
            new=record_job_status,
        ),
        patch(
            "src.services.agent.job_store._get_redis",
            new=AsyncMock(return_value=redis_client),
        ),
        patch(
            "src.services.agent.agent_execution_service._persist_assistant_message_safe",
            new=persist_assistant,
        ),
        patch(
            "src.services.agent.agent_execution_service._persist_user_message",
            new=persist_user,
        ),
        patch(
            "src.services.agent.observability.record_token_usage",
            new=Mock(),
        ),
    ):
        await _resume_agent_graph(job_id, True, user)

    # (a) The user row is NEVER re-persisted on the confirm path — it was
    #     already written up-front by the initial /execute run.
    persist_user.assert_not_awaited()

    # (b) Exactly one assistant row, carrying the checkpoint-anchored cmid
    #     (byte-identical to streaming.py's key), the resumed plan, and this
    #     turn's token usage.
    persist_assistant.assert_awaited_once()
    assistant_call = persist_assistant.await_args
    assert assistant_call is not None
    kwargs = assistant_call.kwargs
    expected_cmid = str(
        _uuid.uuid5(_uuid.NAMESPACE_URL, f"nous-assistant-resume:{thread_id}:ckpt-1")
    )
    assert kwargs["client_message_id"] == expected_cmid
    assert kwargs["thread_id"] == thread_id
    assert kwargs["content"] == "Done - the paper was ingested."
    assert kwargs["model_name"] == "model-router"
    assert kwargs["plan"] == final_state["plan"]
    assert kwargs["token_usage"] == {"input_tokens": 11, "output_tokens": 7}
    record_job_status.assert_awaited_once()
    projection_call = record_job_status.await_args
    assert projection_call is not None
    assert projection_call.args[0] == job_id
    assert projection_call.kwargs["raise_on_error"] is True
    assert len(redis_client.pipeline_calls) == 1
    publication_pipeline = redis_client.pipeline_calls[0]
    assert publication_pipeline.watched_key == f"agent:job:{job_id}"
    assert publication_pipeline.multi_called is True
    assert publication_pipeline.executed is True
    resume_call = graph.ainvoke.await_args
    assert resume_call is not None
    resume_input = resume_call.args[0]
    assert resume_input.resume == {"confirmed": True}
    assert resume_input.update["runtime_tool_names"] == list(
        TOOL_REGISTRY.available_descriptor_names()
    )
    assert resume_input.update["tool_registry_hash"] == registry_metadata["hash"]
    assert resume_input.update["tool_registry_version"] == registry_metadata["version"]
    assert resume_input.update["runtime_projection_unavailable"] is False
    published = json.loads(redis_client.values[f"agent:job:{job_id}"])
    assert published["status"] == JobStatus.COMPLETED.value
    assert published["user_id"] == str(user.id)
    assert published["organization_id"] == str(user.organization_id)
    assert published["thread_id"] == thread_id
    assert published["result"]["message"]["content"] == (
        "Done - the paper was ingested."
    )
    assert published["result"]["model"] == "model-router"
    assert published["result"]["usage"] == {"input_tokens": 11, "output_tokens": 7}
    assert published["result"]["finish_reason"] == "stop"
    assert len(sessions) >= 2
    assert sessions[0] is not sessions[1]
    for session in sessions:
        session.close.assert_awaited_once()
