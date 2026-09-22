"""Regression test for the HITL JOB confirm-path persistence fix (S2).

The job/poll confirm path (``_resume_agent_graph``) used to call the
deprecated ``_persist_thread_messages`` shim, which re-inserted a bare user
row on every confirm (no client_message_id -> no dedup -> inflated
message_count) and wrote the assistant row with no idempotency key / plan /
token_usage. The user row was already persisted up-front by the initial
/execute run, so the confirm path must persist ONLY the assistant row —
mirroring the SSE confirm path (streaming.py ``_resume_assistant_cmid``).
"""

import uuid as _uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

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

    def __init__(self, pre_snapshot, post_snapshot, final_state):
        self._pre = pre_snapshot
        self._post = post_snapshot
        self._get_state_calls = 0
        self.ainvoke = AsyncMock(return_value=final_state)

    async def aget_state(self, config):
        self._get_state_calls += 1
        return self._pre if self._get_state_calls == 1 else self._post


@pytest.mark.asyncio
async def test_job_confirm_persists_assistant_only_with_checkpoint_cmid():
    from src.api.agent.execute import AgentExecuteRequest, _set_job
    from src.services.agent.agent_execution_service import _resume_agent_graph

    user = Mock()
    user.id = "user-confirm-1"
    user.organization_id = "org-1"

    thread_id = str(uuid4())
    conversation_id = str(uuid4())
    job_id = str(uuid4())

    _set_job(
        job_id,
        {
            "status": "awaiting_confirmation",
            "tool_executions": [],
            "user_id": str(user.id),
            "request": AgentExecuteRequest(
                messages=[{"role": "user", "content": "ingest this paper"}],
                thread_id=thread_id,
                model="model-router",
            ).model_dump(),
        },
    )

    pre_snapshot = SimpleNamespace(
        values={"user_id": str(user.id)},
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
            SimpleNamespace(
                type="ai",
                content="Done - the paper was ingested.",
                usage_metadata={"input_tokens": 11, "output_tokens": 7},
            )
        ],
        "tool_executions": [],
        "retrieved_contexts": [],
        "plan": [{"step": 1, "description": "Ingest", "tool": "ingest_arxiv"}],
    }
    graph = _FakeGraph(pre_snapshot, post_snapshot, final_state)

    thread_row = SimpleNamespace(id=thread_id, conversation_id=conversation_id)
    db = AsyncMock()
    db.get = AsyncMock(return_value=thread_row)

    @asynccontextmanager
    async def _session_cm():
        yield db

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
            return_value=_session_cm(),
        ),
        patch(
            "src.services.agent.agent_run_service.record_job_status",
            new_callable=AsyncMock,
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
    kwargs = persist_assistant.await_args.kwargs
    expected_cmid = str(
        _uuid.uuid5(_uuid.NAMESPACE_URL, f"nous-assistant-resume:{thread_id}:ckpt-1")
    )
    assert kwargs["client_message_id"] == expected_cmid
    assert kwargs["thread_id"] == thread_id
    assert kwargs["content"] == "Done - the paper was ingested."
    assert kwargs["model_name"] == "model-router"
    assert kwargs["plan"] == final_state["plan"]
    assert kwargs["token_usage"] == {"input_tokens": 11, "output_tokens": 7}
