"""SQLite-backed presentation replay proofs; no providers or live sockets.

The real append-only ledger and scoped run reads are retained. Only document,
message and membership access are replaced so each revocation is controllable.
SQLite does not establish PostgreSQL contention or cross-process guarantees.

Mutation proof (2026-10-13): changed ``by_creation=True`` to ``False`` in
``src/services/compat/alphaxiv_live.py:325``. The focused command
``python -m pytest -q backend/tests/unit/services/test_alphaxiv_live_replay.py::test_releasing_old_run_lease_does_not_switch_replay_backwards --no-cov -o log_cli=false``
failed because releasing the old lease replaced the newer snapshot with the
old completed answer/run ID. The source was restored byte-for-byte; the
focused regression passed again as part of the complete replay suite.

Recorder cadence (2026-10-20): forcing LiveRecorder._reauthorization_due in
src/services/compat/alphaxiv_live.py to always True failed
``-k recorder_authorizes_once`` (per-frame authorize/paper checks returned);
forcing it to False after the first write failed
``-k recorder_reauthorizes_on_its_cadence`` (revocation never observed).
Both were restored byte-for-byte and the ``-k recorder`` tests passed again.

LiveFollower (2026-10-20), each restored byte-for-byte and rerun green:
- ``after_seq=current.last_seq`` -> ``after_seq=0`` in LiveFollower.poll failed
  ``-k follower_poll_reads_only_new_rows`` (ledger re-read from 0).
- ``if current.terminal:`` -> ``if False:`` failed
  ``-k follower_goes_idle_after_terminal`` (ledger read after the terminal).
- ``if _header(rows) is not None:`` -> ``if False:`` failed
  ``-k follower_run_switch_yields_exactly_one`` (new run never replaced).
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import Request
from pydantic import ValidationError
from sqlalchemy import event, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.compat import alphaxiv_live as live_route
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.chat_message import ChatMessage, MessageRole
from src.models.document import DocumentType, ProcessingStatus
from src.models.user import User
from src.schemas.alphaxiv import AlphaXivLiveChatRequest
from src.services.agent.agent_run_service import release_lease
from src.services.agent.run_event_store import (
    RunAlreadyTerminalError,
    append_event,
    read_events,
)
from src.services.agent.run_event_types import RunEventType, validate_payload
from src.services.agent.schemas import AgentExecuteRequest, AgentMessage
from src.services.compat import alphaxiv_live as replay
from src.services.compat.alphaxiv import CompatibilityError

pytestmark = pytest.mark.unit

ACTOR = UUID("11111111-1111-4111-8111-111111111111")
ORG = UUID("22222222-2222-4222-8222-222222222222")
CHAT = UUID("33333333-3333-4333-8333-333333333333")
PAPER = UUID("44444444-4444-4444-8444-444444444444")
MESSAGE = UUID("55555555-5555-4555-8555-555555555555")
PROMPT = "Explain the exact result from the selected pages."


@dataclass
class Harness:
    factory: async_sessionmaker[AsyncSession]
    run_id: UUID
    authorize: AsyncMock
    document: AsyncMock
    message: AsyncMock

    async def start(
        self, thinking: Literal["low", "medium", "high"] | None = None
    ) -> None:
        await replay.start_live_run(
            actor_id=ACTOR,
            chat_id=CHAT,
            run_id=self.run_id,
            body=AlphaXivLiveChatRequest.model_validate(
                {
                    "message": PROMPT,
                    "paperVersionId": str(PAPER),
                    "llmChatId": str(CHAT),
                    "model": "gpt-5-mini" if thinking else "gpt-4o",
                    "thinking": thinking,
                    "webSearch": "off",
                    "selectionPageRange": [1, 3],
                }
            ),
        )

    async def record(self, kind: str, payload: dict[str, Any]) -> None:
        await replay.record_live_frame(
            actor_id=ACTOR,
            chat_id=CHAT,
            run_id=self.run_id,
            kind=kind,
            payload=payload,
        )

    async def append(
        self, kind: RunEventType, payload: dict[str, Any] | None = None
    ) -> None:
        async with self.factory() as db:
            await append_event(
                db,
                run_id=str(self.run_id),
                event_type=kind,
                payload=payload or {},
                organization_id=ORG,
            )
            await db.commit()

    async def snapshot(self) -> dict[str, Any]:
        value = await replay.read_live_snapshot(actor_id=ACTOR, chat_id=CHAT)
        assert value is not None
        return value

    async def rows(self) -> list[AgentRunEvent]:
        async with self.factory() as db:
            return await read_events(
                db,
                str(self.run_id),
                organization_id=ORG,
                user_id=ACTOR,
                limit=2000,
            )


@pytest.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    # Preserve outer-transaction ownership across the ledger's SAVEPOINTs.
    @event.listens_for(engine.sync_engine, "connect")
    def disable_driver_transactions(connection: Any, _record: Any) -> None:
        connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def emit_begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as connection:
        await connection.run_sync(AgentRun.__table__.create)
        await connection.run_sync(AgentRunEvent.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    authorize = AsyncMock(return_value=SimpleNamespace(id=ACTOR, organization_id=ORG))
    document = AsyncMock(
        return_value=SimpleNamespace(
            id=PAPER,
            document_type=DocumentType.PDF,
            processing_status=ProcessingStatus.COMPLETED,
        )
    )
    message = AsyncMock(
        return_value=ChatMessage(
            id=MESSAGE,
            thread_id=CHAT,
            role=MessageRole.USER,
            content=PROMPT,
            is_deleted=False,
        )
    )
    monkeypatch.setattr(replay, "AsyncSessionLocal", factory)
    monkeypatch.setattr(replay, "authorize_pdf_chat", authorize)
    monkeypatch.setattr(replay, "get_accessible_document_or_none", document)
    monkeypatch.setattr(replay, "get_message", message)
    run_id = uuid4()
    async with factory() as db:
        db.add(
            AgentRun(
                job_id=str(run_id),
                status="running",
                user_id=ACTOR,
                organization_id=ORG,
                thread_id=CHAT,
                user_message_id=MESSAGE,
                execution_provider="nous",
            )
        )
        await db.commit()
    try:
        yield Harness(factory, run_id, authorize, document, message)
    finally:
        await engine.dispose()


def _text(snapshot: dict[str, Any]) -> str:
    return "".join(
        item["delta"]
        for item in snapshot["events"]
        if item["type"] == "delta_output_text"
    )


def _native_frame(kind: str, payload: dict[str, Any]) -> str:
    return f"event: {kind}\ndata: {json.dumps(payload)}\n\n"


@pytest.mark.parametrize(
    "terminal,terminal_payload,frame_kind,expected_kind",
    [
        (RunEventType.RUN_COMPLETED, {}, "done", "completed"),
        (
            RunEventType.RUN_FAILED,
            {"code": "provider_failed", "message": "Safe failure"},
            "error",
            "error",
        ),
        (RunEventType.RUN_CANCELLED, {"reason": "user_requested"}, "done", "aborted"),
    ],
)
async def test_detached_driver_records_real_replay_and_native_terminal(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    terminal: RunEventType,
    terminal_payload: dict[str, Any],
    frame_kind: str,
    expected_kind: str,
) -> None:
    body = AlphaXivLiveChatRequest.model_validate(
        {
            "message": PROMPT,
            "paperVersionId": str(PAPER),
            "llmChatId": str(CHAT),
            "model": "gpt-4o",
            "webSearch": "off",
            "selectionPageRange": [1, 3],
        }
    )
    native = AgentExecuteRequest(
        messages=[AgentMessage(role="user", content=PROMPT)],
        attachment_ids=[PAPER],
    )
    request = live_route._DetachedRequest(
        Request({"type": "http", "headers": [], "state": {}})
    )
    user = cast(User, SimpleNamespace(id=ACTOR, organization_id=ORG))
    ready: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
    source_paused, release_model, closed = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    pulls: list[asyncio.Task[Any] | None] = []
    long_output = "Long stable answer. " * 300

    async def source(*_args: Any, **_kwargs: Any) -> AsyncIterator[str]:
        try:
            pulls.append(asyncio.current_task())
            yield _native_frame(
                "status",
                {
                    "phase": "accepted",
                    "thread_id": str(CHAT),
                    "run_id": str(harness.run_id),
                },
            )
            source_paused.set()
            await release_model.wait()
            frames: list[tuple[str, dict[str, Any]]] = [
                (
                    "rag_context",
                    {
                        "contexts": [
                            {
                                "source_position": 1,
                                "document_id": str(PAPER),
                                "page_number": 3,
                                "content": "RAW_PRIVATE_SNIPPET",
                            }
                        ]
                    },
                ),
                ("token", {"content": long_output + "[D"}),
                ("token", {"content": "oc 1] trailing [D"}),
                ("heartbeat", {"tick": 1}),
            ]
            for kind, payload in frames:
                pulls.append(asyncio.current_task())
                yield _native_frame(kind, payload)
            await harness.append(terminal, terminal_payload)
            pulls.append(asyncio.current_task())
            yield _native_frame(frame_kind, {"error": "RAW_TERMINAL_SECRET"})
        finally:
            closed.set()

    monkeypatch.setattr(live_route.streaming, "stream_event_generator", source)
    producer = asyncio.create_task(
        live_route._drive_live_turn(
            body, native, request, user, ready, time.monotonic()
        )
    )
    try:
        assert await asyncio.wait_for(asyncio.shield(ready), 2) == {
            "llmChatId": str(CHAT),
            "runId": str(harness.run_id),
        }
        await asyncio.wait_for(source_paused.wait(), 2)
        accepted = await harness.snapshot()
        assert accepted["events"][0]["type"] == "run_start"
        assert accepted["events"][0]["userContent"] == PROMPT
        assert not producer.done()
        release_model.set()
        await asyncio.wait_for(asyncio.shield(producer), 2)
    finally:
        release_model.set()
        if not producer.done():
            producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)

    assert closed.is_set()
    assert len(pulls) == 6 and all(pull is producer for pull in pulls)
    snapshot = await harness.snapshot()
    assert _text(snapshot).startswith(long_output)
    assert f'paper="{PAPER}" page="3"' in _text(snapshot)
    assert _text(snapshot).endswith(" trailing [D")
    assert snapshot["events"][-1] == {"type": "run_end", "kind": expected_kind}
    rows = await harness.rows()
    assert rows[-1].event_type == terminal.value
    serialized = json.dumps([row.payload for row in rows])
    assert "RAW_PRIVATE_SNIPPET" not in serialized
    assert "RAW_TERMINAL_SECRET" not in serialized


async def test_snapshot_starts_from_canonical_message_and_dense_wire_sequence(
    harness: Harness,
) -> None:
    await harness.append(RunEventType.RUN_CREATED)
    await harness.start()
    await harness.append(RunEventType.RUN_STARTED)
    await harness.record("token", {"content": "Alpha "})
    await harness.append(RunEventType.USAGE_UPDATED, {"total_tokens": 9})
    await harness.record("token", {"content": "omega."})

    snapshot = await harness.snapshot()
    assert snapshot["runId"] == str(harness.run_id)
    assert snapshot["events"][0] == {
        "type": "run_start",
        "userContent": PROMPT,
        "userFiles": [],
        "model": "gpt-4o",
        "thinking": None,
        "webSearch": "off",
        "parentMessageId": None,
        "paperVersionId": str(PAPER),
        "selectionPageRange": [1, 3],
    }
    assert _text(snapshot) == "Alpha omega."
    update = await replay.read_live_update(
        actor_id=ACTOR, chat_id=CHAT, known_run_id=harness.run_id, after_seq=1
    )
    assert update["type"] == "delta"
    assert update["payloads"] == [
        {"runId": str(harness.run_id), "seq": seq, "event": item}
        for seq, item in enumerate(snapshot["events"][1:], start=2)
    ]
    assert len(snapshot["events"]) < len(await harness.rows())
    assert snapshot == await harness.snapshot()


async def test_snapshot_sequence_is_a_resumable_projected_event_cursor(
    harness: Harness,
) -> None:
    await harness.start()
    await harness.append(RunEventType.RUN_STARTED)
    await harness.record("token", {"content": "First "})
    await harness.append(RunEventType.USAGE_UPDATED, {"total_tokens": 9})
    initial = await harness.snapshot()
    assert initial["seq"] == len(initial["events"]) == 2
    assert initial["seq"] < len(await harness.rows())

    await harness.record("token", {"content": "second."})
    await harness.append(RunEventType.RUN_COMPLETED)
    update = await replay.read_live_update(
        actor_id=ACTOR,
        chat_id=CHAT,
        known_run_id=harness.run_id,
        after_seq=initial["seq"],
    )
    assert [item["seq"] for item in update["payloads"]] == [3, 4]
    assert update["payloads"][-1]["event"] == {
        "type": "run_end",
        "kind": "completed",
    }
    replacement = await replay.read_live_update(
        actor_id=ACTOR, chat_id=CHAT, known_run_id=uuid4(), after_seq=0
    )
    assert replacement["type"] == "replace"
    final = replacement["payload"]
    assert final["seq"] == len(final["events"]) == 4
    assert final["events"][: initial["seq"]] == initial["events"]
    assert final == await harness.snapshot()


@pytest.mark.parametrize("thinking", ["low", "medium", "high"])
async def test_requested_thinking_survives_durable_replay(
    harness: Harness, thinking: Literal["low", "medium", "high"]
) -> None:
    await harness.start(thinking=thinking)
    await harness.record("token", {"content": "Answer."})
    await harness.append(RunEventType.RUN_COMPLETED)
    header = next(
        row
        for row in await harness.rows()
        if row.event_type == RunEventType.CHAT_PRESENTATION_STARTED.value
    )
    assert header.payload["thinking"] == thinking
    snapshot = await harness.snapshot()
    assert snapshot["events"][0]["thinking"] == thinking
    assert snapshot == await harness.snapshot()


async def test_legacy_presentation_without_thinking_replays_null(
    harness: Harness,
) -> None:
    await harness.start()
    header = (await harness.rows())[0]
    legacy = {key: value for key, value in header.payload.items() if key != "thinking"}
    # Seed the stored shape from before thinking became a presentation fact.
    async with harness.factory() as db:
        await db.execute(
            update(AgentRunEvent)
            .where(AgentRunEvent.id == header.id)
            .values(payload=legacy)
        )
        await db.commit()
    assert "thinking" not in (await harness.rows())[0].payload
    snapshot = await harness.snapshot()
    assert snapshot["events"][0]["thinking"] is None


async def test_replay_reads_beyond_first_ledger_page(harness: Harness) -> None:
    await harness.start()
    for _ in range(505):
        await harness.append(RunEventType.RUN_STARTED)
    await harness.record("token", {"content": "Visible after the first page."})
    await harness.append(RunEventType.RUN_COMPLETED)

    snapshot = await harness.snapshot()
    assert len(await harness.rows()) > 500
    assert _text(snapshot) == "Visible after the first page."
    assert snapshot["events"][-1] == {"type": "run_end", "kind": "completed"}


async def test_releasing_old_run_lease_does_not_switch_replay_backwards(
    harness: Harness,
) -> None:
    await harness.start()
    await harness.record("token", {"content": "Old completed answer"})
    await harness.append(RunEventType.RUN_COMPLETED)
    now = datetime.now(timezone.utc)
    async with harness.factory() as db:
        await db.execute(
            update(AgentRun)
            .where(AgentRun.job_id == str(harness.run_id))
            .values(
                status="completed",
                lease_owner="test-old-sweeper",
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(hours=2),
            )
        )
        new_id = uuid4()
        db.add(
            AgentRun(
                job_id=str(new_id),
                status="running",
                user_id=ACTOR,
                organization_id=ORG,
                thread_id=CHAT,
                user_message_id=MESSAGE,
                execution_provider="nous",
                created_at=now - timedelta(days=1),
            )
        )
        await db.commit()
    newer = Harness(
        harness.factory, new_id, harness.authorize, harness.document, harness.message
    )
    await newer.start()
    await newer.record("token", {"content": "Current answer"})
    expected = await newer.snapshot()
    assert expected["runId"] == str(new_id)
    async with harness.factory() as db:
        await db.execute(
            update(AgentRun)
            .where(AgentRun.job_id == str(new_id))
            .values(updated_at=now - timedelta(hours=1))
        )
        await db.commit()
        assert await release_lease(
            db, str(harness.run_id), lease_owner="test-old-sweeper"
        )

    assert await newer.snapshot() == expected
    assert await replay.read_live_update(
        actor_id=ACTOR,
        chat_id=CHAT,
        known_run_id=new_id,
        after_seq=len(expected["events"]),
    ) == {"type": "delta", "payloads": []}
    assert await replay.read_live_update(
        actor_id=ACTOR,
        chat_id=CHAT,
        known_run_id=harness.run_id,
        after_seq=1,
    ) == {"type": "replace", "payload": expected}


async def test_split_citation_and_unclosed_tail_survive_durable_replay(
    harness: Harness,
) -> None:
    await harness.start()
    await harness.record(
        "rag_context",
        {
            "contexts": [
                {
                    "source_position": 1,
                    "document_id": str(PAPER),
                    "page_number": 3,
                    "title": "Results",
                }
            ]
        },
    )
    for fragment in ("Result [D", "oc ", "1]. unfinished [D"):
        await harness.record("token", {"content": fragment})
    active = await harness.snapshot()
    assert _text(active).endswith("unfinished ")
    await harness.append(RunEventType.RUN_COMPLETED)
    terminal = await harness.snapshot()
    assert f'paper="{PAPER}" page="3" title="Results"' in _text(terminal)
    assert _text(terminal).endswith("unfinished [D")
    assert terminal["events"][: len(active["events"])] == active["events"]
    assert terminal["events"][-1] == {"type": "run_end", "kind": "completed"}


@pytest.mark.parametrize(
    "terminal,payload,expected",
    [
        (RunEventType.RUN_COMPLETED, {}, "completed"),
        (
            RunEventType.RUN_FAILED,
            {"code": "provider_failed", "message": "Safe failure"},
            "error",
        ),
        (RunEventType.RUN_CANCELLED, {"reason": "user_requested"}, "aborted"),
    ],
)
async def test_native_terminal_is_authoritative_and_late_frames_do_not_append(
    harness: Harness,
    terminal: RunEventType,
    payload: dict[str, Any],
    expected: str,
) -> None:
    await harness.start()
    await harness.record("token", {"content": "Partial [D"})
    # Presentation cannot claim completion before the native writer commits.
    before = len(await harness.rows())
    await harness.record("done", {"content": "Forged completion"})
    await harness.record("error", {"error": "Raw internal failure"})
    assert len(await harness.rows()) == before
    assert all(
        item["type"] != "run_end" for item in (await harness.snapshot())["events"]
    )

    await harness.append(terminal, payload)
    before = len(await harness.rows())
    with pytest.raises(RunAlreadyTerminalError):
        await harness.record("token", {"content": "MUST NOT APPEAR"})
    snapshot = await harness.snapshot()
    assert len(await harness.rows()) == before
    assert _text(snapshot) == "Partial [D"
    assert snapshot["events"][-1] == {"type": "run_end", "kind": expected}
    assert sum(item["type"] == "run_end" for item in snapshot["events"]) == 1


async def test_large_unicode_delta_preserves_every_character(harness: Harness) -> None:
    await harness.start()
    text = "Aβ🧪" * 2300 + " THE-END"
    await harness.record("token", {"content": text})
    await harness.append(RunEventType.RUN_COMPLETED)
    assert _text(await harness.snapshot()) == text
    rows = await harness.rows()
    frames = [
        row.payload
        for row in rows
        if row.event_type == RunEventType.CHAT_PRESENTATION_FRAME
    ]
    assert len(frames) > 4
    assert all(len(item["frame"]["text"]) <= 1000 for item in frames)


@pytest.mark.parametrize("revoked", ["membership", "document", "message"])
async def test_replay_rechecks_access_on_every_read(
    harness: Harness, revoked: str
) -> None:
    await harness.start()
    await harness.record("token", {"content": "Only for the current actor."})
    await harness.snapshot()
    if revoked == "membership":
        harness.authorize.side_effect = CompatibilityError(404, "Chat not found")
    elif revoked == "document":
        harness.document.return_value = None
    elif revoked == "message":
        harness.message.return_value = None
    with pytest.raises(CompatibilityError):
        await replay.read_live_snapshot(actor_id=ACTOR, chat_id=CHAT)
    with pytest.raises(CompatibilityError):
        await replay.read_live_update(
            actor_id=ACTOR, chat_id=CHAT, known_run_id=harness.run_id, after_seq=1
        )


async def test_changed_actor_organization_cannot_replay_prior_tenant(
    harness: Harness,
) -> None:
    await harness.start()
    await harness.record("token", {"content": "Prior tenant private answer"})
    await harness.snapshot()
    harness.authorize.return_value = SimpleNamespace(id=ACTOR, organization_id=uuid4())
    assert await replay.read_live_snapshot(actor_id=ACTOR, chat_id=CHAT) is None
    assert await replay.read_live_update(
        actor_id=ACTOR, chat_id=CHAT, known_run_id=harness.run_id, after_seq=1
    ) == {"type": "replace", "payload": None}


async def test_changed_canonical_message_cannot_relabel_a_run(harness: Harness) -> None:
    await harness.start()
    await harness.record("token", {"content": "Answer to the original question."})
    harness.message.return_value.content = "An edited question with a different meaning"
    with pytest.raises(CompatibilityError, match="accepted message changed") as caught:
        await harness.snapshot()
    assert caught.value.status_code == 409


@pytest.mark.parametrize("cursor", ["unknown_run", "ahead"])
async def test_stale_cursor_receives_full_replacement(
    harness: Harness, cursor: str
) -> None:
    await harness.start()
    await harness.record("token", {"content": "A complete answer"})
    snapshot = await harness.snapshot()
    update = await replay.read_live_update(
        actor_id=ACTOR,
        chat_id=CHAT,
        known_run_id=uuid4() if cursor == "unknown_run" else harness.run_id,
        after_seq=1 if cursor == "unknown_run" else len(snapshot["events"]) + 5,
    )
    assert update == {"type": "replace", "payload": snapshot}
    assert await replay.read_live_update(
        actor_id=ACTOR,
        chat_id=CHAT,
        known_run_id=harness.run_id,
        after_seq=len(snapshot["events"]),
    ) == {"type": "delta", "payloads": []}


async def test_negative_cursor_is_rejected(harness: Harness) -> None:
    with pytest.raises(CompatibilityError) as caught:
        await replay.read_live_update(
            actor_id=ACTOR, chat_id=CHAT, known_run_id=harness.run_id, after_seq=-1
        )
    assert caught.value.status_code == 422


@pytest.mark.parametrize("bound", ["_MAX_SNAPSHOT_BYTES", "_MAX_LEDGER_ROWS"])
async def test_replay_limits_fail_explicitly_without_truncated_snapshot(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    await harness.start()
    await harness.record("token", {"content": "A complete answer"})
    expected = await harness.snapshot()
    with monkeypatch.context() as patch:
        patch.setattr(replay, bound, 1)
        with pytest.raises(CompatibilityError) as caught:
            await harness.snapshot()
        assert caught.value.status_code == 503
    assert await harness.snapshot() == expected


@pytest.mark.parametrize("replacement_pages", [[], [8]])
async def test_context_replacement_does_not_reuse_old_citation_pages(
    harness: Harness, replacement_pages: list[int]
) -> None:
    await harness.start()
    for pages in ([3, 4], replacement_pages):
        await harness.record(
            "rag_context",
            {
                "contexts": [
                    {
                        "source_position": index,
                        "document_id": str(PAPER),
                        "page_number": page,
                    }
                    for index, page in enumerate(pages, start=1)
                ]
            },
        )
    await harness.record("token", {"content": "[Doc 1] [Doc 2]"})
    await harness.append(RunEventType.RUN_COMPLETED)
    text = _text(await harness.snapshot())
    assert 'page="3"' not in text
    assert 'page="4"' not in text
    assert "[Doc 2]" in text
    assert ('page="8"' in text) is bool(replacement_pages)


async def test_presentation_payloads_drop_raw_context_and_do_not_copy_prompt(
    harness: Harness,
) -> None:
    await harness.start()
    await harness.record(
        "rag_context",
        {
            "contexts": [
                {
                    "source_position": 1,
                    "document_id": str(PAPER),
                    "page_number": 3,
                    "title": "Results",
                    "content": "RAW_PRIVATE_SNIPPET",
                    "metadata": {"api_key": "CUSTOM_SECRET_MARKER"},
                }
            ],
            "customFilter": {"secret": "CUSTOM_SECRET_MARKER"},
        },
    )
    token = "sk-proj-" + "a" * 40
    await harness.record(
        "token", {"content": f"Email person@example.invalid token {token}"}
    )
    serialized = json.dumps([row.payload for row in await harness.rows()])
    for forbidden in (
        PROMPT,
        "RAW_PRIVATE_SNIPPET",
        "CUSTOM_SECRET_MARKER",
        token,
        "person@example.invalid",
    ):
        assert forbidden not in serialized
    assert "Results" in serialized
    assert "<token>" in _text(await harness.snapshot())
    assert "<email>" in _text(await harness.snapshot())


@pytest.mark.parametrize(
    "frame",
    [
        {"kind": "token", "text": "answer", "api_key": "secret"},
        {"kind": "context_complete", "contexts": [{"content": "snippet"}]},
        {"kind": "custom_filter", "secret": "secret"},
    ],
)
def test_ledger_validation_rejects_unknown_presentation_fields(
    frame: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        validate_payload(RunEventType.CHAT_PRESENTATION_FRAME, {"frame": frame})


async def test_replay_reauthorizes_durable_file_refs(harness: Harness) -> None:
    from src.models.document import Document
    from src.models.message_attachment import MessageAttachment
    from src.services.compat.alphaxiv_files import file_url

    file_id = uuid4()
    async with harness.factory() as db:
        connection = await db.connection()
        await connection.run_sync(Document.__table__.create)
        await connection.run_sync(MessageAttachment.__table__.create)
        await connection.run_sync(User.__table__.create)
        await db.execute(
            text(
                "INSERT INTO users (id,organization_id,email,password_hash,first_name,last_name,role,is_active,login_count,created_at,updated_at,is_deleted) "
                "VALUES (:id,:org,'file@example.test','unused','File','User','USER',1,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,0)"
            ),
            {"id": str(ACTOR), "org": str(ORG)},
        )
        db.add(
            Document(
                id=file_id,
                organization_id=ORG,
                title="Notes",
                filename="notes.txt",
                file_path="unused",
                file_size_bytes=10,
                mime_type="text/plain",
                document_type=DocumentType.TEXT,
                processing_status=ProcessingStatus.COMPLETED,
                content_text="Actual file content",
            )
        )
        db.add_all(
            [
                MessageAttachment(message_id=MESSAGE, document_id=identifier)
                for identifier in (PAPER, file_id)
            ]
        )
        await db.commit()
    body = AlphaXivLiveChatRequest.model_validate(
        {
            "message": PROMPT,
            "paperVersionId": str(PAPER),
            "files": [{"contentType": "text/plain", "url": file_url(file_id)}],
        }
    )
    await replay.start_live_run(
        actor_id=ACTOR, chat_id=CHAT, run_id=harness.run_id, body=body
    )
    snapshot = await harness.snapshot()
    assert snapshot["events"][0]["userFiles"] == [
        {"contentType": "text/plain", "url": file_url(file_id)}
    ]
    # Reopening a fresh DB session must reconstruct identical references and sequence positions.
    assert await harness.snapshot() == snapshot
    async with harness.factory() as db:
        await db.execute(
            update(Document).where(Document.id == file_id).values(is_deleted=True)
        )
        await db.commit()
    # A later-deleted attachment is omitted from reads; the replay survives.
    after = await harness.snapshot()
    assert after["events"][0]["userFiles"] == []
    assert after["events"][1:] == snapshot["events"][1:]
    # A foreign-organization reference is never exposed either.
    async with harness.factory() as db:
        await db.execute(
            update(Document)
            .where(Document.id == file_id)
            .values(is_deleted=False, organization_id=uuid4())
        )
        await db.commit()
    assert (await harness.snapshot())["events"][0]["userFiles"] == []


async def test_live_start_rejects_file_missing_from_canonical_accepted_message(
    harness: Harness,
) -> None:
    from src.models.document import Document
    from src.models.message_attachment import MessageAttachment
    from src.services.compat.alphaxiv_files import file_url

    identifier = uuid4()
    async with harness.factory() as db:
        connection = await db.connection()
        await connection.run_sync(Document.__table__.create)
        await connection.run_sync(MessageAttachment.__table__.create)
        await connection.run_sync(User.__table__.create)
        await db.execute(
            text(
                "INSERT INTO users (id,organization_id,email,password_hash,first_name,last_name,role,is_active,login_count,created_at,updated_at,is_deleted) "
                "VALUES (:id,:org,'file@example.test','unused','File','User','USER',1,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,0)"
            ),
            {"id": str(ACTOR), "org": str(ORG)},
        )
        db.add(
            Document(
                id=identifier,
                organization_id=ORG,
                title="Unattached",
                filename="note.txt",
                file_path="unused",
                file_size_bytes=10,
                mime_type="text/plain",
                document_type=DocumentType.TEXT,
                processing_status=ProcessingStatus.COMPLETED,
                content_text="Not accepted as input",
            )
        )
        await db.commit()
    body = AlphaXivLiveChatRequest.model_validate(
        {
            "message": PROMPT,
            "paperVersionId": str(PAPER),
            "files": [{"contentType": "text/plain", "url": file_url(identifier)}],
        }
    )
    with pytest.raises(CompatibilityError, match="accepted files changed"):
        await replay.start_live_run(
            actor_id=ACTOR, chat_id=CHAT, run_id=harness.run_id, body=body
        )
    assert await harness.rows() == []


async def test_update_without_any_run_is_a_no_op_until_one_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat with no presentation run (new, or last used natively) must not
    re-send an empty replacement on every poll."""
    snapshot = AsyncMock(return_value=None)
    monkeypatch.setattr(replay, "read_live_snapshot", snapshot)

    assert await replay.read_live_update(
        actor_id=ACTOR, chat_id=CHAT, known_run_id=None, after_seq=0
    ) == {"type": "delta", "payloads": []}

    # A run the client knew about that disappears is replaced exactly once;
    # the follower's cursor then becomes (None, 0) and polls stay silent.
    assert await replay.read_live_update(
        actor_id=ACTOR, chat_id=CHAT, known_run_id=uuid4(), after_seq=2
    ) == {"type": "replace", "payload": None}

    started = {"runId": str(uuid4()), "events": [{"type": "run_start"}]}
    snapshot.return_value = started
    assert await replay.read_live_update(
        actor_id=ACTOR, chat_id=CHAT, known_run_id=None, after_seq=0
    ) == {"type": "replace", "payload": started}


async def test_invalid_page_filter_is_dropped_without_ending_the_run(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    await harness.start()
    with caplog.at_level("WARNING", logger=replay.logger.name):
        await harness.record(
            "rag_context",
            {
                "contexts": [
                    {"source_position": 1, "document_id": str(PAPER), "page_number": 2}
                ],
                "page_filter": {"document_id": "not-a-uuid", "accepted_pages": [0]},
            },
        )
    await harness.record("token", {"content": "Still answering [Doc 1]."})
    frames = [
        row.payload["frame"]
        for row in await harness.rows()
        if row.event_type == RunEventType.CHAT_PRESENTATION_FRAME.value
    ]
    complete = next(item for item in frames if item["kind"] == "context_complete")
    assert complete.get("page_filter") is None
    assert frames[-1] == {"kind": "token", "text": "Still answering [Doc 1]."}
    assert 'page="2"' in _text(await harness.snapshot())
    warned = [r for r in caplog.records if "page_filter" in r.getMessage()]
    assert len(warned) == 1 and warned[0].exc_info is not None


def _recorder(harness: Harness, **kwargs: Any) -> replay.LiveRecorder:
    return replay.LiveRecorder(
        actor_id=ACTOR, chat_id=CHAT, run_id=harness.run_id, paper_id=PAPER, **kwargs
    )


async def test_recorder_authorizes_once_and_never_rereads_the_header_per_frame(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await harness.start()
    ledger_reads = AsyncMock(wraps=replay.read_events)
    monkeypatch.setattr(replay, "read_events", ledger_reads)
    authorizations = harness.authorize.await_count
    paper_checks = harness.document.await_count
    recorder = _recorder(harness)
    for index in range(5):
        await recorder.record("token", {"content": f"t{index} "})
    assert harness.authorize.await_count == authorizations + 1
    assert harness.document.await_count == paper_checks + 1
    ledger_reads.assert_not_awaited()
    assert _text(await harness.snapshot()) == "t0 t1 t2 t3 t4 "


async def test_recorder_late_frame_after_native_terminal_does_not_append(
    harness: Harness,
) -> None:
    await harness.start()
    recorder = _recorder(harness)
    await recorder.record("token", {"content": "Partial"})
    await harness.append(RunEventType.RUN_COMPLETED)
    before = len(await harness.rows())
    with pytest.raises(RunAlreadyTerminalError):
        await recorder.record("token", {"content": "MUST NOT APPEAR"})
    assert len(await harness.rows()) == before
    snapshot = await harness.snapshot()
    assert _text(snapshot) == "Partial"
    assert sum(item["type"] == "run_end" for item in snapshot["events"]) == 1


async def test_recorder_reauthorizes_on_its_cadence_and_fails_closed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await harness.start()
    clock = [100.0]
    monkeypatch.setattr(replay, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    recorder = _recorder(harness, reauthorize_seconds=30.0)
    await recorder.record("token", {"content": "one "})
    authorizations = harness.authorize.await_count
    harness.authorize.side_effect = CompatibilityError(404, "Chat not found")
    clock[0] += 29.0
    # Inside the window the revocation is not yet observed (bounded staleness).
    await recorder.record("token", {"content": "two "})
    assert harness.authorize.await_count == authorizations
    clock[0] += 1.0
    before = len(await harness.rows())
    with pytest.raises(CompatibilityError) as caught:
        await recorder.record("token", {"content": "MUST NOT APPEAR"})
    assert caught.value.status_code == 404
    assert len(await harness.rows()) == before
    harness.authorize.side_effect = None
    assert _text(await harness.snapshot()) == "one two "


def _follower() -> replay.LiveFollower:
    return replay.LiveFollower(actor_id=ACTOR, chat_id=CHAT)


def _apply(
    view: dict[str, Any] | None, update: dict[str, Any]
) -> dict[str, Any] | None:
    """Client-side cursor application with the route's no-gap/no-dup rule."""
    if update["type"] == "replace":
        return copy.deepcopy(update["payload"])
    for payload in update["payloads"]:
        assert view is not None and payload["runId"] == view["runId"]
        assert payload["seq"] == view["seq"] + 1, "gap or duplicate"
        view["events"].append(payload["event"])
        view["seq"] = payload["seq"]
    return view


async def test_follower_poll_reads_only_new_rows_and_matches_fresh_snapshots(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await harness.start()
    await harness.record("token", {"content": "Alpha "})
    follower = _follower()
    view = copy.deepcopy(await follower.snapshot())
    assert view == await harness.snapshot()
    ledger_reads = AsyncMock(wraps=replay.read_events)
    monkeypatch.setattr(replay, "read_events", ledger_reads)
    steps: list[tuple[str, dict[str, Any]]] = [
        (
            "rag_context",
            {
                "contexts": [
                    {"source_position": 1, "document_id": str(PAPER), "page_number": 4}
                ]
            },
        ),
        ("token", {"content": "beta [D"}),
        ("token", {"content": "oc 1] gamma."}),
    ]
    for kind, payload in steps:
        consumed = (await harness.rows())[-1].seq
        await harness.record(kind, payload)
        ledger_reads.reset_mock()
        update = await follower.poll(reauthorize=False)
        # Incremental: the ledger is read only after the consumed sequence.
        assert [call.kwargs["after_seq"] for call in ledger_reads.await_args_list] == [
            consumed
        ]
        view = _apply(view, update)
        assert view == await harness.snapshot()
    await harness.append(RunEventType.RUN_COMPLETED)
    view = _apply(view, await follower.poll(reauthorize=False))
    assert view == await harness.snapshot()
    assert view is not None and view["events"][-1] == {
        "type": "run_end",
        "kind": "completed",
    }
    assert await follower.poll(reauthorize=False) == {"type": "delta", "payloads": []}


async def test_follower_rechecks_access_only_when_asked_and_fails_closed(
    harness: Harness,
) -> None:
    await harness.start()
    follower = _follower()
    await follower.snapshot()
    await harness.record("token", {"content": "Visible."})
    counts = (
        harness.authorize.await_count,
        harness.document.await_count,
        harness.message.await_count,
    )
    assert (await follower.poll(reauthorize=False))["type"] == "delta"
    assert (
        harness.authorize.await_count,
        harness.document.await_count,
        harness.message.await_count,
    ) == counts
    await follower.poll(reauthorize=True)
    assert harness.authorize.await_count == counts[0] + 1
    assert harness.document.await_count == counts[1] + 1
    assert harness.message.await_count == counts[2] + 1
    for revoke in ("membership", "document", "message"):
        if revoke == "membership":
            harness.authorize.side_effect = CompatibilityError(404, "Chat not found")
        elif revoke == "document":
            harness.authorize.side_effect = None
            harness.document.return_value = None
        else:
            harness.document.return_value = SimpleNamespace(
                id=PAPER,
                document_type=DocumentType.PDF,
                processing_status=ProcessingStatus.COMPLETED,
            )
            harness.message.return_value = None
        with pytest.raises(CompatibilityError):
            await follower.poll(reauthorize=True)


async def test_follower_org_change_on_recheck_replaces_with_empty_view(
    harness: Harness,
) -> None:
    await harness.start()
    follower = _follower()
    assert await follower.snapshot() is not None
    harness.authorize.return_value = SimpleNamespace(id=ACTOR, organization_id=uuid4())
    assert await follower.poll(reauthorize=True) == {
        "type": "replace",
        "payload": None,
    }
    assert await follower.poll(reauthorize=True) == {"type": "delta", "payloads": []}


async def test_follower_goes_idle_after_terminal_and_only_probes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await harness.start()
    follower = _follower()
    await follower.snapshot()
    assert not follower.idle
    await harness.append(RunEventType.RUN_CANCELLED, {"reason": "user_requested"})
    update = await follower.poll(reauthorize=False)
    assert update["payloads"][-1]["event"] == {"type": "run_end", "kind": "aborted"}
    assert follower.idle
    ledger_reads = AsyncMock(wraps=replay.read_events)
    monkeypatch.setattr(replay, "read_events", ledger_reads)
    for _ in range(3):
        assert await follower.poll(reauthorize=False) == {
            "type": "delta",
            "payloads": [],
        }
    ledger_reads.assert_not_awaited()


async def _new_run(harness: Harness) -> Harness:
    new_id = uuid4()
    async with harness.factory() as db:
        await db.execute(
            update(AgentRun)
            .where(AgentRun.job_id == str(harness.run_id))
            .values(status="completed")
        )
        db.add(
            AgentRun(
                job_id=str(new_id),
                status="running",
                user_id=ACTOR,
                organization_id=ORG,
                thread_id=CHAT,
                user_message_id=MESSAGE,
                execution_provider="nous",
                created_at=datetime.now(timezone.utc) + timedelta(seconds=5),
            )
        )
        await db.commit()
    return Harness(
        harness.factory, new_id, harness.authorize, harness.document, harness.message
    )


async def test_follower_run_switch_yields_exactly_one_replace(
    harness: Harness,
) -> None:
    await harness.start()
    await harness.record("token", {"content": "Old answer"})
    await harness.append(RunEventType.RUN_COMPLETED)
    follower = _follower()
    view = await follower.snapshot()
    assert follower.idle
    newer = await _new_run(harness)
    # The new run exists but has no presentation yet: the old view is replaced
    # by the empty one exactly once (the stateless contract does the same).
    first = await follower.poll(reauthorize=False)
    assert first == await replay.read_live_update(
        actor_id=ACTOR,
        chat_id=CHAT,
        known_run_id=harness.run_id,
        after_seq=len(view["events"]),  # type: ignore[index]
    )
    assert first == {"type": "replace", "payload": None}
    await newer.append(RunEventType.RUN_STARTED)
    assert await follower.poll(reauthorize=False) == {"type": "delta", "payloads": []}
    await newer.start()
    await newer.record("token", {"content": "New answer"})
    replaced = await follower.poll(reauthorize=False)
    assert replaced == {"type": "replace", "payload": await newer.snapshot()}
    assert not follower.idle
    await newer.record("token", {"content": " continues"})
    delta = await follower.poll(reauthorize=False)
    assert delta["type"] == "delta" and len(delta["payloads"]) == 1
    assert delta["payloads"][0]["runId"] == str(newer.run_id)
    assert _text(await newer.snapshot()) == "New answer continues"


def test_incremental_snapshot_size_matches_whole_encoding() -> None:
    events: list[dict[str, Any]] = [
        {"type": "run_start", "userContent": "Aβ🧪"},
        {"type": "delta_output_text", "index": 0, "delta": 'quote " and \\n'},
        {"type": "run_end", "kind": "completed"},
    ]
    for count in range(len(events) + 1):
        shown = events[:count]
        whole = replay._encoded_size({"runId": "r", "seq": count, "events": shown})
        assert whole == replay._snapshot_size(
            "r", count, sum(replay._encoded_size(item) for item in shown)
        )


async def test_follower_enforces_snapshot_bound_incrementally(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await harness.start()
    follower = _follower()
    snapshot = await follower.snapshot()
    monkeypatch.setattr(
        replay, "_MAX_SNAPSHOT_BYTES", replay._encoded_size(snapshot) + 10
    )
    await harness.record("token", {"content": "x" * 200})
    with pytest.raises(CompatibilityError) as caught:
        await follower.poll(reauthorize=False)
    assert caught.value.status_code == 503
