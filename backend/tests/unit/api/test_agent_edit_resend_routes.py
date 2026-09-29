"""Every route that persists a user turn must also resync the checkpoint.

There are THREE of them and the original change wired only one, so an edit sent
through the Luna fast path or through ``POST /agent/execute`` tombstoned the DB
rows and left the LangGraph checkpoint holding the replaced turn — the model
kept answering the question the user had already edited away.

These tests assert the wiring, not the resync itself (that is
``test_agent_edit_resend_checkpoint.py``): whichever writer owns this turn's
user row reports tombstones, the route calls ``resync_thread_checkpoint`` for
the resolved thread, and a non-edit turn calls it for NO thread at all.

Two writers exist on ``/stream`` since P0-C (#1330). The primary one is the
atomic accept transaction, which persists the user row (and, on an edit, its
tombstones) BEFORE the generator emits ``accepted``; the route then reads the
outcome off ``AcceptedSubmission.tombstoned``. The degraded path — no
ownership-verified thread the accept transaction can key on — still runs
``_persist_user_message_guarded`` and reads the outcome off its
``TombstoneReport``. Both are covered here, because a resync wired to only one
of them is a resync that silently stops happening for most real traffic.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable, Optional
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessageChunk

from tests.utils.agent_job_status import stub_durable_status_projection

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _stub_durable_status_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_durable_status_projection(monkeypatch)


def _user() -> Any:  # SimpleNamespace stand-in for models.user.User
    return SimpleNamespace(id=uuid4(), organization_id=uuid4())


def _persist_reporting(tombstoned: bool, count: int = 3) -> Any:
    """Stand-in for ``_persist_user_message_guarded`` that fills the report."""
    calls: list = []

    async def _persist(
        db: Any,
        current_user: Any,
        request: Any,
        *,
        tombstoned_out: Any = None,
        **kw: Any,
    ) -> bool:
        calls.append(request)
        if tombstoned_out is not None and tombstoned:
            tombstoned_out.record(count)
        return True

    _persist.calls = calls  # type: ignore[attr-defined]
    return _persist


def _acceptance(thread_id: Any, *, tombstoned: bool, count: int = 3) -> Any:
    """What the committed accept transaction hands the generator."""
    from src.services.agent.agent_submission_service import AcceptedSubmission

    return AcceptedSubmission(
        run_id=str(uuid4()),
        thread_id=str(thread_id),
        user_message_id=str(uuid4()),
        outbox_id=str(uuid4()),
        idempotency_key="agent-stream:test",
        tombstoned=tombstoned,
        tombstoned_count=count if tombstoned else 0,
    )


class _FakeLuna:
    async def astream(
        self, _messages: Any, *, config: Any = None
    ) -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(content="hello")


class _LunaGraph:
    """Fast-path graph seam: records the post-answer checkpoint append."""

    def __init__(self) -> None:
        self.aupdate_state = AsyncMock(return_value=None)

    async def astream_events(self, *_a: Any, **_k: Any) -> AsyncIterator[Any]:
        raise AssertionError("fast-path turn entered LangGraph execution")
        yield  # pragma: no cover


class _ReducerLunaGraph(_LunaGraph):
    """Same seam, but ``aupdate_state`` goes through langgraph's real reducer.

    The mock-only graph above cannot see the defect this exists for: whether the
    post-answer append REPAIRS a checkpoint the resync seeded with a stopped
    partial answer. That is a property of ``add_messages``, so run the real one.
    """

    def __init__(self, head: Optional[list] = None) -> None:
        super().__init__()
        from langgraph.graph.message import add_messages

        self.head = list(head or [])
        self._add_messages = add_messages

        async def _apply(config: Any, values: Any, as_node: Any = None) -> None:
            self.head = list(self._add_messages(self.head, values["messages"]))

        self.aupdate_state = AsyncMock(side_effect=_apply)


async def _run_luna(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tombstoned: bool,
    assistant_row_id: Optional[str] = "assistant-row-id",
    graph: Any = None,
    resync: Any = None,
    user_cmid: Any = None,
    accepted: bool = True,
) -> SimpleNamespace:
    from src.api.agent import streaming as streaming_mod
    from src.api.agent.execute import AgentExecuteRequest, AgentMessage
    from src.core.config import get_settings
    from src.services.agent import agent_execution_service as jobs_mod
    from src.services.agent import llm_factory

    user = _user()
    thread = SimpleNamespace(id=uuid4(), conversation_id=uuid4())
    user_cmid = user_cmid or uuid4()
    body = AgentExecuteRequest(
        messages=[
            AgentMessage(
                role="user",
                content="Explain why the sky appears blue",
                client_message_id=user_cmid,
            )
        ],
        page_context={"type": "chat"},
        use_rag=False,
        thread_id=str(thread.id),
        supersedes_client_message_id=uuid4() if tombstoned else None,
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_MAX_INPUT_CHARS", 8_000)
    monkeypatch.setattr(llm_factory, "build_fast_path_llm", lambda: _FakeLuna())

    graph = graph if graph is not None else _LunaGraph()
    resync = resync if resync is not None else AsyncMock(return_value=True)
    persist = _persist_reporting(tombstoned)
    accept = AsyncMock(
        return_value=_acceptance(thread.id, tombstoned=tombstoned),
    )

    with (
        patch.object(
            streaming_mod,
            "AsyncSessionLocal",
            return_value=SimpleNamespace(close=AsyncMock()),
        ),
        patch.object(
            streaming_mod,
            "_resolve_thread",
            new=AsyncMock(return_value=(thread, str(thread.conversation_id))),
        ),
        # `accepted=False` models the degraded path: no thread the accept
        # transaction can key on, so the guarded persist owns the user row.
        patch.object(streaming_mod, "_accept_eligible", new=lambda _t: accepted),
        patch.object(streaming_mod, "accept_submission", new=accept),
        patch.object(
            streaming_mod,
            "mark_submission_dispatched",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod, "finalize_submission", new=AsyncMock(return_value=None)
        ),
        patch.object(streaming_mod, "_persist_user_message_guarded", new=persist),
        patch.object(streaming_mod, "resync_thread_checkpoint", new=resync),
        patch.object(
            jobs_mod,
            "_persist_assistant_message_safe",
            new=AsyncMock(return_value=assistant_row_id),
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch("src.services.agent.graph.compile_agent_graph", new=lambda **_k: graph),
    ):
        events = [
            e async for e in streaming_mod.stream_event_generator(body, request, user)
        ]

    return SimpleNamespace(
        events=events,
        graph=graph,
        resync=resync,
        user=user,
        thread=thread,
        user_cmid=user_cmid,
        persist=persist,
        accept=accept,
    )


async def test_luna_route_resyncs_when_the_accept_reports_tombstones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = await _run_luna(monkeypatch, tombstoned=True)

    run.resync.assert_awaited_once()
    kwargs = run.resync.await_args.kwargs
    assert kwargs["thread_id"] == str(run.thread.id)
    assert kwargs["user"] is run.user
    # The answer still reached the client.
    assert any('"content": "hello"' in e for e in run.events)
    assert any("event: done" in e for e in run.events)


async def test_luna_route_append_repairs_a_partial_row_seeded_by_the_resync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-answer append must run even when the resync already seeded.

    Failure this pins: a cancelled attempt persisted a STOPPED partial answer
    under the deterministic assistant cmid. The retry's assistant insert dedups
    onto that row (content is NOT updated), so the resync seeds the checkpoint
    with the PARTIAL text while the client received the full answer. Skipping
    the append as "redundant" would leave the model's context permanently
    holding the truncated answer.

    Asserted through langgraph's real ``add_messages``, because the repair is
    exactly its same-id replace semantics.
    """
    import uuid as _uuid

    from langchain_core.messages import AIMessage, HumanMessage

    user_cmid = uuid4()
    # Same derivation the route uses (streaming._assistant_client_message_id):
    # deterministic in the user cmid, which is precisely why a cancelled
    # attempt's partial row is the one the retry dedups onto.
    assistant_id = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"nous-assistant:{user_cmid}"))

    async def _seeding_resync(graph: Any, *, thread_id: Any, user: Any) -> bool:
        # Stand in for resync_thread_checkpoint: whole-head reseed from a DB
        # whose assistant row still holds the cancelled attempt's partial text.
        graph.head = [
            HumanMessage(content="Explain why the sky appears blue", id="u-seed"),
            AIMessage(content="hel", id=assistant_id),
        ]
        return True

    graph = _ReducerLunaGraph()
    resync = AsyncMock(side_effect=_seeding_resync)

    run = await _run_luna(
        monkeypatch,
        tombstoned=True,
        graph=graph,
        resync=resync,
        user_cmid=user_cmid,
    )
    assert run.user_cmid == user_cmid
    expected_assistant_id = assistant_id

    resync.assert_awaited_once()

    ai = [m for m in graph.head if isinstance(m, AIMessage)]
    # Fails with the partial "hel" if the post-resync append is ever skipped.
    assert [m.content for m in ai] == ["hello"], graph.head
    assert ai[0].id == expected_assistant_id
    assert graph.aupdate_state.await_count == 1
    # ...and the seeded partial row was REPLACED, not appended beside.
    assert len(graph.head) == len({m.id for m in graph.head})


async def test_luna_route_still_appends_when_the_assistant_row_was_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed assistant persist means the seed lacks this answer.

    Skipping the append there would drop the answer out of the checkpoint
    entirely — worse than a redundant write.
    """
    run = await _run_luna(monkeypatch, tombstoned=True, assistant_row_id=None)

    run.resync.assert_awaited_once()
    assert run.graph.aupdate_state.await_count == 1
    assert any("event: error" in event for event in run.events)
    assert not any("event: done" in event for event in run.events)


async def test_luna_route_does_not_resync_a_plain_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = await _run_luna(monkeypatch, tombstoned=False)

    run.resync.assert_not_awaited()
    # ...and the normal post-answer append still happens.
    assert run.graph.aupdate_state.await_count == 1


async def test_luna_route_under_accept_does_not_rewrite_the_user_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accept transaction already committed it (P0-C).

    A second write here would insert a SECOND user row for a legacy client that
    sends no client_message_id — there is nothing for ON CONFLICT to infer.
    """
    run = await _run_luna(monkeypatch, tombstoned=True)

    assert run.persist.calls == []


async def test_luna_route_degraded_path_still_resyncs_from_the_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No accept-eligible thread: the guarded persist owns the tombstones."""
    run = await _run_luna(monkeypatch, tombstoned=True, accepted=False)

    assert len(run.persist.calls) == 1
    run.resync.assert_awaited_once()
    assert run.resync.await_args.kwargs["thread_id"] == str(run.thread.id)


async def test_luna_route_degraded_path_plain_turn_does_not_resync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = await _run_luna(monkeypatch, tombstoned=False, accepted=False)

    assert len(run.persist.calls) == 1
    run.resync.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Graph (SSE) route
# --------------------------------------------------------------------------- #


class _GraphRoute:
    def __init__(self) -> None:
        self.astream_called = False
        self._snapshot = SimpleNamespace(values={"messages": []}, tasks=())
        self.aupdate_state = AsyncMock(return_value=None)

    async def astream_events(self, *_a: Any, **_k: Any) -> AsyncIterator[Any]:
        self.astream_called = True
        if False:  # pragma: no cover
            yield

    async def aget_state(self, _config: Any) -> Any:
        return self._snapshot


async def _run_graph_route(
    monkeypatch: pytest.MonkeyPatch, *, tombstoned: bool, accepted: bool = True
) -> SimpleNamespace:
    from src.api.agent import streaming as streaming_mod
    from src.api.agent.execute import AgentExecuteRequest, AgentMessage
    from src.core.config import get_settings
    from src.services.agent import agent_execution_service as jobs_mod

    user = _user()
    thread = SimpleNamespace(id=uuid4(), conversation_id=uuid4(), source_project=None)
    body = AgentExecuteRequest(
        messages=[AgentMessage(role="user", content="Search my documents")],
        page_context={"type": "documents"},
        use_rag=True,
        thread_id=str(thread.id),
        supersedes_client_message_id=uuid4() if tombstoned else None,
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    monkeypatch.setattr(get_settings(), "AGENT_FAST_PATH_ENABLED", True)

    graph = _GraphRoute()
    resync = AsyncMock(return_value=True)
    persist = _persist_reporting(tombstoned)
    accept = AsyncMock(return_value=_acceptance(thread.id, tombstoned=tombstoned))

    # Ordering probe: model input is assembled here, so this records whether the
    # resync had already run. "Resynced eventually" is not the contract — a
    # resync AFTER assembly still hands the model the edited-away turn.
    real_build = jobs_mod.build_user_history_messages
    resync_count_at_assembly: list[int] = []

    def _recording_build(*a: Any, **k: Any) -> Any:
        resync_count_at_assembly.append(resync.await_count)
        return real_build(*a, **k)

    with (
        patch.object(
            streaming_mod,
            "AsyncSessionLocal",
            return_value=SimpleNamespace(close=AsyncMock()),
        ),
        patch.object(
            streaming_mod,
            "_resolve_thread",
            new=AsyncMock(return_value=(thread, str(thread.conversation_id))),
        ),
        patch.object(streaming_mod, "_accept_eligible", new=lambda _t: accepted),
        patch.object(streaming_mod, "accept_submission", new=accept),
        patch.object(
            streaming_mod,
            "mark_submission_dispatched",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod, "finalize_submission", new=AsyncMock(return_value=None)
        ),
        patch.object(streaming_mod, "_persist_user_message_guarded", new=persist),
        patch.object(streaming_mod, "resync_thread_checkpoint", new=resync),
        patch.object(jobs_mod, "build_user_history_messages", new=_recording_build),
        patch.object(
            jobs_mod,
            "_persist_assistant_message_safe",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch("src.services.agent.graph.compile_agent_graph", new=lambda **_k: graph),
    ):
        [e async for e in streaming_mod.stream_event_generator(body, request, user)]

    return SimpleNamespace(
        graph=graph,
        resync=resync,
        user=user,
        thread=thread,
        persist=persist,
        resync_count_at_assembly=resync_count_at_assembly,
    )


async def test_graph_route_resyncs_before_entering_the_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = await _run_graph_route(monkeypatch, tombstoned=True)

    run.resync.assert_awaited_once()
    assert run.resync.await_args.kwargs["thread_id"] == str(run.thread.id)
    assert run.resync.await_args.kwargs["user"] is run.user
    # The accept transaction owns the user row under P0-C.
    assert run.persist.calls == []
    # ...and it precedes model-input assembly, or the model is handed the
    # edited-away turn anyway.
    assert run.resync_count_at_assembly == [1]


async def test_graph_route_does_not_resync_a_plain_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = await _run_graph_route(monkeypatch, tombstoned=False)

    run.resync.assert_not_awaited()


async def test_graph_route_degraded_path_resyncs_from_the_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No accept-eligible thread: the guarded persist owns the tombstones."""
    run = await _run_graph_route(monkeypatch, tombstoned=True, accepted=False)

    assert len(run.persist.calls) == 1
    run.resync.assert_awaited_once()
    assert run.resync.await_args.kwargs["thread_id"] == str(run.thread.id)


async def test_graph_route_degraded_path_plain_turn_does_not_resync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = await _run_graph_route(monkeypatch, tombstoned=False, accepted=False)

    assert len(run.persist.calls) == 1
    run.resync.assert_not_awaited()


# --------------------------------------------------------------------------- #
# /agent/execute job route
# --------------------------------------------------------------------------- #


async def _run_execute_route(
    monkeypatch: pytest.MonkeyPatch, *, tombstoned: bool
) -> SimpleNamespace:
    from src.services.agent import agent_execution_service as aes

    user = _user()
    thread = SimpleNamespace(id=uuid4(), conversation_id=uuid4())
    request = SimpleNamespace(
        thread_id=str(thread.id),
        messages=[SimpleNamespace(role="user", content="hi", client_message_id=None)],
        supersedes_client_message_id=uuid4() if tombstoned else None,
        page_context={"type": "chat"},
        model="",
        use_rag=False,
        max_context_docs=5,
    )

    class _Graph:
        def __init__(self) -> None:
            self.entered = False

        async def ainvoke(self, *_a: Any, **_k: Any) -> Any:
            self.entered = True
            raise RuntimeError("stop here — wiring is what this test asserts")

        async def aget_state(self, _c: Any) -> Any:
            return SimpleNamespace(values={"messages": []}, tasks=())

    graph = _Graph()
    resync = AsyncMock(return_value=True)
    persist = _persist_reporting(tombstoned)

    class _Session:
        async def __aenter__(self) -> Any:
            return AsyncMock()

        async def __aexit__(self, *a: Any) -> bool:
            return False

    with (
        patch.object(aes, "AsyncSessionLocal", lambda: _Session()),
        patch.object(
            aes, "_resolve_thread", new=AsyncMock(return_value=(thread, "conv"))
        ),
        patch.object(aes, "_persist_user_message_guarded", new=persist),
        patch.object(aes, "resync_thread_checkpoint", new=resync),
        patch.object(
            aes,
            "_resolve_and_bind_project",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch("src.services.agent.graph.compile_agent_graph", new=lambda **_k: graph),
        patch.object(aes, "_set_job", new=lambda *a, **k: None),
    ):
        await aes._run_agent_graph("job-1", request, user)

    return SimpleNamespace(resync=resync, thread=thread, user=user)


async def test_execute_route_resyncs_when_tombstones_were_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = await _run_execute_route(monkeypatch, tombstoned=True)

    run.resync.assert_awaited_once()
    assert run.resync.await_args.kwargs["thread_id"] == str(run.thread.id)
    assert run.resync.await_args.kwargs["user"] is run.user


async def test_execute_route_does_not_resync_a_plain_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = await _run_execute_route(monkeypatch, tombstoned=False)

    run.resync.assert_not_awaited()
