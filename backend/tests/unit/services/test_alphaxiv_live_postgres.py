"""Real PostgreSQL ordering proofs for durable PDF presentation facts.

Set ORCHESTRATION_TEST_DATABASE_URL to a disposable PostgreSQL database.
Each test creates and drops its own generated schema. Run/event tables,
locks, ledger reads/appends and native finalization remain real; authority
boundaries and explicit scheduling pauses are the only injected behavior.

The duplicate-start test protects services/compat/alphaxiv_live.py:51,
_owned_run's early get_run(for_update=True). Mutation verified on PostgreSQL
14.23: a test-process wrapper setting get_run's for_update=False failed with
duplicate header sequences [1, 2], despite append_event's later lock. The
unmodified source passed again; no production-file mutation was necessary.
Focused command: PYTHONPATH=backend python -m pytest -q
backend/tests/unit/services/test_alphaxiv_live_postgres.py
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Column, MetaData, Table, event, insert, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.chat_message import ChatMessage, MessageRole
from src.models.document import DocumentType, ProcessingStatus
from src.models.user import User
from src.schemas.alphaxiv import AlphaXivLiveChatRequest
from src.services.agent.agent_submission_service import finalize_submission
from src.services.agent.run_event_store import RunAlreadyTerminalError, read_events
from src.services.agent.run_event_types import RunEventType
from src.services.compat import alphaxiv_live as replay
from src.shared.enums import JobStatus

pytestmark = pytest.mark.integration

_PROMPT = "Explain the selected PDF pages."
_WAIT_SECONDS = 10


@dataclass
class PostgresHarness:
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    actor_id: UUID
    org_id: UUID
    chat_id: UUID
    paper_id: UUID
    run_id: UUID

    async def start(self) -> None:
        await replay.start_live_run(
            actor_id=self.actor_id,
            chat_id=self.chat_id,
            run_id=self.run_id,
            body=AlphaXivLiveChatRequest.model_validate(
                {
                    "message": _PROMPT,
                    "paperVersionId": str(self.paper_id),
                    "llmChatId": str(self.chat_id),
                    "model": "gpt-4o",
                    "webSearch": "off",
                }
            ),
        )

    async def record(self) -> None:
        await replay.record_live_frame(
            actor_id=self.actor_id,
            chat_id=self.chat_id,
            run_id=self.run_id,
            kind="token",
            payload={"content": "A durable answer."},
        )

    async def finalize(self, db: AsyncSession, *, commit: bool = True) -> bool:
        return await finalize_submission(
            db,
            run_id=str(self.run_id),
            organization_id=self.org_id,
            status=JobStatus.COMPLETED,
            event_type=RunEventType.RUN_COMPLETED,
            commit=commit,
        )

    async def rows(self) -> list[AgentRunEvent]:
        async with self.factory() as db:
            return await read_events(
                db,
                str(self.run_id),
                organization_id=self.org_id,
                user_id=self.actor_id,
            )


@pytest.fixture
async def postgres_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[PostgresHarness]:
    url = os.environ.get("ORCHESTRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    assert url is not None
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError("ORCHESTRATION_TEST_DATABASE_URL must be PostgreSQL")
    schema = "alphaxiv_live_test_" + uuid4().hex
    admin = create_async_engine(url)
    engine = create_async_engine(
        url,
        isolation_level="READ COMMITTED",
        connect_args={"server_settings": {"search_path": schema}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    actor_id, org_id, chat_id, paper_id, run_id, message_id = (
        uuid4() for _ in range(6)
    )
    models: list[Any] = [AgentRun, AgentRunEvent]
    actual_names = {model.__tablename__ for model in models}
    parents = MetaData()
    for model in models:
        for foreign_key in model.__table__.foreign_keys:
            target = foreign_key.column
            name = target.table.name
            if name not in actual_names and name not in parents.tables:
                Table(name, parents, Column(target.name, target.type, primary_key=True))

    try:
        async with admin.begin() as connection:
            # Exclusively generated identifiers; no caller-controlled SQL.
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            await connection.run_sync(parents.create_all)
            for model in models:
                await connection.run_sync(model.__table__.create)
            for name, value in (("threads", chat_id), ("chat_messages", message_id)):
                await connection.execute(insert(parents.tables[name]).values(id=value))
        async with factory() as db:
            db.add(
                AgentRun(
                    job_id=str(run_id),
                    status="running",
                    user_id=actor_id,
                    organization_id=org_id,
                    thread_id=chat_id,
                    user_message_id=message_id,
                    execution_provider="nous",
                )
            )
            await db.commit()
        monkeypatch.setattr(replay, "AsyncSessionLocal", factory)
        monkeypatch.setattr(
            replay,
            "authorize_pdf_chat",
            AsyncMock(
                return_value=SimpleNamespace(id=actor_id, organization_id=org_id)
            ),
        )
        monkeypatch.setattr(
            replay,
            "get_accessible_document_or_none",
            AsyncMock(
                return_value=SimpleNamespace(
                    id=paper_id,
                    document_type=DocumentType.PDF,
                    processing_status=ProcessingStatus.COMPLETED,
                )
            ),
        )
        monkeypatch.setattr(
            replay,
            "get_message",
            AsyncMock(return_value=ChatMessage(role=MessageRole.USER, content=_PROMPT)),
        )
        yield PostgresHarness(
            engine, factory, actor_id, org_id, chat_id, paper_id, run_id
        )
    finally:
        await engine.dispose()
        try:
            async with admin.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                )
        finally:
            await admin.dispose()


async def _finish_tasks(*tasks: asyncio.Task[Any] | None) -> None:
    pending = [task for task in tasks if task is not None]
    for task in pending:
        if not task.done():
            task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


async def test_duplicate_start_has_exactly_one_header(
    postgres_harness: PostgresHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_read, second_arrived = asyncio.Event(), asyncio.Event()
    release_first, release_second = asyncio.Event(), asyncio.Event()
    original_read = replay._read_ledger
    first: asyncio.Task[None] | None = None
    second: asyncio.Task[None] | None = None

    async def pause_after_real_read(
        db: AsyncSession, run: AgentRun, user: User
    ) -> list[AgentRunEvent]:
        rows = await original_read(db, run, user)
        if asyncio.current_task() is first:
            first_read.set()
            await asyncio.wait_for(release_first.wait(), _WAIT_SECONDS)
        elif asyncio.current_task() is second:
            # Without the early lock this reads the same empty ledger. Keep
            # that genuine stale observation until the first start commits.
            second_arrived.set()
            await asyncio.wait_for(release_second.wait(), _WAIT_SECONDS)
        return rows

    def observe_second_lock(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if (
            asyncio.current_task() is second
            and "FOR UPDATE" in statement
            and "agent_runs" in statement
        ):
            second_arrived.set()

    monkeypatch.setattr(replay, "_read_ledger", pause_after_real_read)
    event.listen(
        postgres_harness.engine.sync_engine,
        "before_cursor_execute",
        observe_second_lock,
    )
    try:
        first = asyncio.create_task(postgres_harness.start())
        await asyncio.wait_for(first_read.wait(), _WAIT_SECONDS)
        second = asyncio.create_task(postgres_harness.start())
        await asyncio.wait_for(second_arrived.wait(), _WAIT_SECONDS)
        release_first.set()
        await asyncio.wait_for(first, _WAIT_SECONDS)
        release_second.set()
        await asyncio.wait_for(second, _WAIT_SECONDS)
        rows = await postgres_harness.rows()
        headers = [row for row in rows if row.event_type == "chat.presentation_started"]
        assert len(headers) == 1, (
            "Concurrent starts wrote duplicate presentation headers despite "
            f"append_event's later lock: {[row.seq for row in headers]}"
        )
    finally:
        release_first.set()
        release_second.set()
        await _finish_tasks(first, second)
        event.remove(
            postgres_harness.engine.sync_engine,
            "before_cursor_execute",
            observe_second_lock,
        )


async def test_native_terminal_winner_rejects_presentation_append(
    postgres_harness: PostgresHarness,
) -> None:
    await postgres_harness.start()
    attempted = asyncio.Event()
    writer: asyncio.Task[None] | None = None

    def observe_writer_lock(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if (
            asyncio.current_task() is writer
            and "FOR UPDATE" in statement
            and "agent_runs" in statement
        ):
            attempted.set()

    event.listen(
        postgres_harness.engine.sync_engine,
        "before_cursor_execute",
        observe_writer_lock,
    )
    try:
        async with postgres_harness.factory() as finalizer:
            assert await postgres_harness.finalize(finalizer, commit=False)
            writer = asyncio.create_task(postgres_harness.record())
            await asyncio.wait_for(attempted.wait(), _WAIT_SECONDS)
            await finalizer.commit()
            with pytest.raises(RunAlreadyTerminalError):
                await asyncio.wait_for(writer, _WAIT_SECONDS)
        rows = await postgres_harness.rows()
        assert [row.event_type for row in rows] == [
            "chat.presentation_started",
            "run.completed",
        ], "A presentation frame reopened the native terminal ledger"
    finally:
        await _finish_tasks(writer)
        event.remove(
            postgres_harness.engine.sync_engine,
            "before_cursor_execute",
            observe_writer_lock,
        )


async def test_presentation_winner_commits_before_native_terminal(
    postgres_harness: PostgresHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await postgres_harness.start()
    appended, finalizer_attempted, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    original_append = replay.append_event
    writer: asyncio.Task[None] | None = None
    finalizer: asyncio.Task[None] | None = None

    async def pause_after_real_append(db: AsyncSession, **kwargs: Any) -> AgentRunEvent:
        row = await original_append(db, **kwargs)
        appended.set()
        await asyncio.wait_for(release.wait(), _WAIT_SECONDS)
        return row

    async def finish_run() -> None:
        async with postgres_harness.factory() as db:
            assert await postgres_harness.finalize(db)

    def observe_finalizer_update(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if asyncio.current_task() is finalizer and statement.startswith(
            "UPDATE agent_runs"
        ):
            finalizer_attempted.set()

    monkeypatch.setattr(replay, "append_event", pause_after_real_append)
    event.listen(
        postgres_harness.engine.sync_engine,
        "before_cursor_execute",
        observe_finalizer_update,
    )
    try:
        writer = asyncio.create_task(postgres_harness.record())
        await asyncio.wait_for(appended.wait(), _WAIT_SECONDS)
        finalizer = asyncio.create_task(finish_run())
        await asyncio.wait_for(finalizer_attempted.wait(), _WAIT_SECONDS)
        release.set()
        await asyncio.wait_for(writer, _WAIT_SECONDS)
        await asyncio.wait_for(finalizer, _WAIT_SECONDS)
        rows = await postgres_harness.rows()
        assert [row.event_type for row in rows] == [
            "chat.presentation_started",
            "chat.presentation_frame",
            "run.completed",
        ], "Native finalization overtook an accepted presentation write"
        snapshot = await replay.read_live_snapshot(
            actor_id=postgres_harness.actor_id, chat_id=postgres_harness.chat_id
        )
        assert snapshot is not None
        assert snapshot["events"][-2:] == [
            {"type": "delta_output_text", "index": 0, "delta": "A durable answer."},
            {"type": "run_end", "kind": "completed"},
        ]
    finally:
        release.set()
        await _finish_tasks(writer, finalizer)
        event.remove(
            postgres_harness.engine.sync_engine,
            "before_cursor_execute",
            observe_finalizer_update,
        )
