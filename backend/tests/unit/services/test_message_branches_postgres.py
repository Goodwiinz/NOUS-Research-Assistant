"""Canonical branch writes and migration roundtrip on isolated PostgreSQL.

Requires ORCHESTRATION_TEST_DATABASE_URL. Every test owns one generated
schema and removes it. Authorization rows, documents, accepted messages/runs,
assistant writes and selection transactions are real; only background
summarization is replaced. No model/provider is invoked.

Mutation checks (PostgreSQL 14.23, test-process patches; source untouched):
- message_branches.authorize_branch_thread: forcing lock=False lets selection
  pass while a newly accepted run owns the thread; the concurrency test fails.
- message_branches._stamp_path: replacing its 1 ms increment with 1 us repeats
  a client millisecond timestamp; the selected-history test fails.
- pb01_pdf_message_branches.upgrade: skipping the unique reply index accepts a
  duplicate assistant; the migration test fails its IntegrityError assertion
  (and, first, its CONCURRENTLY statement count).
- pb01_pdf_message_branches.upgrade: skipping _drop_index_if_invalid keeps an
  INVALID unique index left by a failed concurrent build; dropping NOT VALID
  or VALIDATE CONSTRAINT changes the emitted DDL. Each fails the migration test.
- agent_submission_service.reserve_branch_reply_capacity: bypassing the check
  accepts a question without answer space; the capacity test fails.
Focused command: PYTHONPATH=backend python -m pytest -q
backend/tests/unit/services/test_message_branches_postgres.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Column,
    MetaData,
    Table,
    event,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.base import Base
from src.models.chat_message import ChatMessage, MessageRole
from src.models.conversation import Conversation
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.message_attachment import MessageAttachment
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.services.agent import agent_execution_service as execution
from src.services.agent import agent_submission_service as submission
from src.services.agent.run_event_types import RunEventType
from src.services.threads import message_branches as branches
from src.services.threads.chat_service import ChatService
from src.shared.enums import JobStatus

pytestmark = pytest.mark.integration


@pytest.fixture
async def postgres_schema() -> AsyncIterator[AsyncEngine]:
    url = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    assert url is not None
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError("PostgreSQL is required")
    schema = "pdf_branches_" + uuid4().hex
    admin = create_async_engine(url)
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        yield engine
    finally:
        await engine.dispose()
        try:
            async with admin.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                )
        finally:
            await admin.dispose()


@dataclass
class BranchHarness:
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    actor: UUID
    org: UUID
    workspace: UUID
    chat: UUID
    paper: UUID

    def request(self, parent: UUID | None = None, *, key: UUID | None = None) -> Any:
        return SimpleNamespace(
            messages=[
                SimpleNamespace(
                    role="user",
                    content="Explain this page",
                    client_message_id=key or uuid4(),
                )
            ],
            model="",
            use_rag=True,
            execution_provider="nous",
            thread_id=str(self.chat),
            attachment_ids=[],
            supersedes_client_message_id=None,
            branch_document_id=self.paper,
            parent_message_id=parent,
        )

    async def accept(
        self, parent: UUID | None = None, *, request: Any = None
    ) -> submission.AcceptedSubmission:
        async with self.factory() as db:
            thread = await db.get(Thread, self.chat)
            return await submission.accept_submission(
                db,
                current_user=SimpleNamespace(id=self.actor, organization_id=self.org),
                request=request or self.request(parent),
                thread=thread,
            )

    async def answer(
        self, accepted: submission.AcceptedSubmission, *, stopped: bool = False
    ) -> UUID:
        async with self.factory() as db:
            value = await execution._persist_assistant_message(
                db,
                thread_id=str(self.chat),
                content="Answer",
                model_name="gpt-4o",
                tool_executions_out=None,
                parent_message_id=accepted.user_message_id,
                client_message_id=str(UUID(accepted.run_id)),
                stopped=stopped,
            )
            assert value is not None
            return UUID(value)

    async def finish(self, accepted: submission.AcceptedSubmission) -> None:
        async with self.factory() as db:
            assert await submission.finalize_submission(
                db,
                run_id=accepted.run_id,
                organization_id=self.org,
                status=JobStatus.COMPLETED,
                event_type=RunEventType.RUN_COMPLETED,
            )


@pytest.fixture
async def branch_harness(
    postgres_schema: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[BranchHarness]:
    models: list[Any] = [
        Organization,
        User,
        Document,
        Workspace,
        WorkspaceMember,
        Conversation,
        Thread,
        ChatMessage,
        MessageAttachment,
        AgentRun,
        AgentRunEvent,
        AgentOutbox,
    ]
    names = {model.__tablename__ for model in models}
    parents = MetaData()
    for model in models:
        for foreign_key in model.__table__.foreign_keys:
            target = foreign_key.column
            if (
                target.table.name not in names
                and target.table.name not in parents.tables
            ):
                Table(
                    target.table.name,
                    parents,
                    Column(target.name, target.type, primary_key=True),
                )
    async with postgres_schema.begin() as connection:
        await connection.run_sync(parents.create_all)
        await connection.run_sync(
            lambda db: Base.metadata.create_all(
                db, tables=[model.__table__ for model in models]
            )
        )
    factory = async_sessionmaker(postgres_schema, expire_on_commit=False)
    actor, org, workspace, conversation, chat, paper = (uuid4() for _ in range(6))
    async with factory() as db:
        await db.execute(
            insert(Organization).values(
                id=org, name=str(org), storage_limit_bytes=1000000
            )
        )
        await db.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) "
                "VALUES (:actor, :org, :email, 'unused', 'PDF', 'User', 'USER', TRUE, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, FALSE)"
            ),
            {"actor": actor, "org": org, "email": f"{actor}@example.test"},
        )
        await db.execute(
            insert(Workspace).values(
                id=workspace, name="PDF", owner_id=actor, organization_id=org
            )
        )
        await db.execute(
            insert(Conversation).values(
                id=conversation,
                workspace_id=workspace,
                title="PDF",
                created_by_id=actor,
            )
        )
        await db.execute(
            insert(Thread).values(id=chat, conversation_id=conversation, title="PDF")
        )
        await db.execute(
            insert(Document).values(
                id=paper,
                organization_id=org,
                uploaded_by_user_id=actor,
                title="Paper",
                filename="paper.pdf",
                file_path="paper.pdf",
                file_size_bytes=1,
                mime_type="application/pdf",
                document_type=DocumentType.PDF,
                processing_status=ProcessingStatus.COMPLETED,
            )
        )
        await db.commit()
    from src.services.threads import thread_summarization_service

    monkeypatch.setattr(
        thread_summarization_service, "enqueue_summarization", lambda *args: None
    )
    yield BranchHarness(postgres_schema, factory, actor, org, workspace, chat, paper)


async def test_accept_assistant_retry_and_selected_sibling_history(
    branch_harness: BranchHarness,
) -> None:
    first = await branch_harness.accept()
    answer = await branch_harness.answer(first, stopped=True)
    assert await branch_harness.answer(first, stopped=True) == answer
    async with branch_harness.factory() as db:
        assert await execution._persist_assistant_message(
            db,
            thread_id=str(branch_harness.chat),
            content="Resume retry",
            model_name="gpt-4o",
            tool_executions_out=None,
            parent_message_id=first.user_message_id,
            client_message_id=str(uuid4()),
        ) == str(answer)
    await branch_harness.finish(first)
    second = await branch_harness.accept()
    second_answer = await branch_harness.answer(second)
    await branch_harness.finish(second)
    async with branch_harness.factory() as db:
        rows = list(
            (
                await db.scalars(select(ChatMessage).order_by(ChatMessage.created_at))
            ).all()
        )
        assert len(rows) == 4
        assert all(row.superseded_by_message_id is None for row in rows)
        assert rows[1].parent_message_id == UUID(str(first.user_message_id))
        assert rows[3].parent_message_id == UUID(str(second.user_message_id))
        # Selection must advance even after a clock skew/future timestamp.
        old_max = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            microsecond=0
        )
        await db.execute(update(ChatMessage).values(selected_at=old_max))
        await db.commit()
        selected = await branches.select_branch(
            db,
            actor_id=branch_harness.actor,
            thread_id=branch_harness.chat,
            message_id=UUID(str(first.user_message_id)),
        )
        assert [item[0] for item in selected] == [
            UUID(str(first.user_message_id)),
            answer,
        ]
        assert all(item[1] > old_max for item in selected)
        assert all(
            int(item[1].timestamp() * 1000) > int(old_max.timestamp() * 1000)
            for item in selected
        ), "Branch selection repeats a selectedAt timestamp at millisecond precision"
        thread = await db.get(Thread, branch_harness.chat)
        assert thread is not None and thread.selected_message_id == answer
        path = await branches.get_branch_path(
            db, thread_id=branch_harness.chat, head_message_id=answer
        )
        assert [row.id for row in path] == [UUID(str(first.user_message_id)), answer]
        assert second_answer not in [row.id for row in path]
        rapid = await branches.select_branch(
            db,
            actor_id=branch_harness.actor,
            thread_id=branch_harness.chat,
            message_id=UUID(str(second.user_message_id)),
        )
        assert min(int(item[1].timestamp() * 1000) for item in rapid) > max(
            int(item[1].timestamp() * 1000) for item in selected
        )


async def test_branch_accept_rolls_back_user_binding_and_selection(
    branch_harness: BranchHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        submission,
        "_insert_outbox",
        AsyncMock(side_effect=RuntimeError("outbox failed")),
    )
    with pytest.raises(RuntimeError, match="outbox failed"):
        await branch_harness.accept()
    async with branch_harness.factory() as db:
        thread = await db.get(Thread, branch_harness.chat)
        assert thread is not None and not thread.message_branching
        assert thread.branch_document_id is None and thread.selected_message_id is None
        assert await db.scalar(select(func.count()).select_from(ChatMessage)) == 0
        assert await db.scalar(select(func.count()).select_from(AgentRun)) == 0


async def test_accept_reserves_answer_capacity_but_allows_full_history_replay(
    branch_harness: BranchHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(branches, "MAX_BRANCH_MESSAGES", 4)
    first_request = branch_harness.request()
    first = await branch_harness.accept(request=first_request)
    await branch_harness.answer(first)
    await branch_harness.finish(first)
    second_request = branch_harness.request()
    second = await branch_harness.accept(request=second_request)
    await branch_harness.finish(second)
    # Three rows fit, but another new question would strand its answer.
    with pytest.raises(branches.BranchAccessError, match="no room"):
        await branch_harness.accept()
    async with branch_harness.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ChatMessage)) == 3
        assert await db.scalar(select(func.count()).select_from(AgentRun)) == 2
        assert await db.scalar(
            select(Thread.selected_message_id).where(Thread.id == branch_harness.chat)
        ) == UUID(str(second.user_message_id))
        # The already accepted question still has exactly one reserved slot.
        await branches.reserve_branch_reply_capacity(
            db,
            thread_id=branch_harness.chat,
            client_message_id=second_request.messages[0].client_message_id,
        )
    await branch_harness.answer(second)
    replay = await branch_harness.accept(request=first_request)
    assert replay.replayed and replay.run_id == first.run_id
    async with branch_harness.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ChatMessage)) == 4
        assert await db.scalar(select(func.count()).select_from(AgentRun)) == 2


async def test_accept_rejects_http_actor_organization_drift(
    branch_harness: BranchHarness,
) -> None:
    fresh_org = uuid4()
    async with branch_harness.factory() as db:
        await db.execute(
            insert(Organization).values(
                id=fresh_org, name=str(fresh_org), storage_limit_bytes=1000000
            )
        )
        await db.execute(
            update(User)
            .where(User.id == branch_harness.actor)
            .values(organization_id=fresh_org)
        )
        # The new tenant owns the PDF, so document authorization alone would
        # succeed while the HTTP actor still supplies the original tenant.
        await db.execute(
            update(Document)
            .where(Document.id == branch_harness.paper)
            .values(organization_id=fresh_org)
        )
        await db.commit()
    with pytest.raises(branches.BranchAccessError, match="authority changed"):
        await branch_harness.accept()
    async with branch_harness.factory() as db:
        thread = await db.get(Thread, branch_harness.chat)
        assert thread is not None and not thread.message_branching
        assert await db.scalar(select(func.count()).select_from(ChatMessage)) == 0
        assert await db.scalar(select(func.count()).select_from(AgentRun)) == 0


@pytest.mark.parametrize(
    "revoked",
    ["document", "document_type", "readiness", "actor", "workspace", "run", "parent"],
)
async def test_resume_refreshes_authority_and_exact_accepted_parent(
    branch_harness: BranchHarness, revoked: str
) -> None:
    accepted = await branch_harness.accept()
    async with branch_harness.factory() as reader:
        # Warm the identity map, then mutate using another real connection.
        document = await reader.get(Document, branch_harness.paper)
        assert document is not None
        assert document.processing_status == ProcessingStatus.COMPLETED
        await branches.authorize_branch_resume(
            reader,
            actor_id=branch_harness.actor,
            thread_id=branch_harness.chat,
            run_id=accepted.run_id,
        )
        async with branch_harness.factory() as writer:
            if revoked in {"document", "document_type", "readiness"}:
                changes: dict[str, dict[str, Any]] = {
                    "document": {"is_deleted": True},
                    "document_type": {"document_type": DocumentType.TEXT},
                    "readiness": {"processing_status": ProcessingStatus.FAILED},
                }
                await writer.execute(
                    update(Document)
                    .where(Document.id == branch_harness.paper)
                    .values(**changes[revoked])
                )
            elif revoked == "actor":
                await writer.execute(
                    update(User)
                    .where(User.id == branch_harness.actor)
                    .values(is_active=False)
                )
            elif revoked == "workspace":
                await writer.execute(
                    update(Workspace)
                    .where(Workspace.id == branch_harness.workspace)
                    .values(is_deleted=True)
                )
            elif revoked == "run":
                await writer.execute(
                    update(AgentRun)
                    .where(AgentRun.job_id == accepted.run_id)
                    .values(execution_provider="codex")
                )
            else:
                await writer.execute(
                    update(ChatMessage)
                    .where(ChatMessage.id == UUID(str(accepted.user_message_id)))
                    .values(is_deleted=True)
                )
            await writer.commit()
        with pytest.raises(branches.BranchAccessError):
            await branches.authorize_branch_resume(
                reader,
                actor_id=branch_harness.actor,
                thread_id=branch_harness.chat,
                run_id=accepted.run_id,
            )


@pytest.mark.parametrize("mutation", ["parent", "document", "message"])
async def test_replayed_key_cannot_change_branch_identity(
    branch_harness: BranchHarness, mutation: str
) -> None:
    request = branch_harness.request()
    accepted = await branch_harness.accept(request=request)
    assert (await branch_harness.accept(request=request)).run_id == accepted.run_id
    if mutation == "parent":
        request.parent_message_id = uuid4()
    elif mutation == "document":
        request.branch_document_id = uuid4()
    else:
        request.messages[0].content = "Changed question"
    with pytest.raises(branches.BranchAccessError):
        await branch_harness.accept(request=request)


@pytest.mark.parametrize(
    "revoked", ["actor", "organization", "workspace", "document", "parent"]
)
async def test_branch_access_revocations_fail_closed(
    branch_harness: BranchHarness, revoked: str
) -> None:
    accepted = await branch_harness.accept()
    answer = await branch_harness.answer(accepted)
    await branch_harness.finish(accepted)
    async with branch_harness.factory() as db:
        if revoked == "actor":
            await db.execute(
                update(User)
                .where(User.id == branch_harness.actor)
                .values(is_active=False)
            )
        elif revoked == "organization":
            await db.execute(
                update(Organization)
                .where(Organization.id == branch_harness.org)
                .values(is_active=False)
            )
        elif revoked == "workspace":
            await db.execute(
                update(Workspace)
                .where(Workspace.id == branch_harness.workspace)
                .values(is_deleted=True)
            )
        elif revoked == "parent":
            await db.execute(
                update(ChatMessage)
                .where(ChatMessage.id == UUID(str(accepted.user_message_id)))
                .values(is_deleted=True)
            )
        else:
            await db.execute(
                update(Document)
                .where(Document.id == branch_harness.paper)
                .values(is_deleted=True)
            )
        await db.commit()
    with pytest.raises(branches.BranchAccessError):
        await branch_harness.accept(answer)


async def test_bound_chat_rejects_native_and_generic_writes(
    branch_harness: BranchHarness,
) -> None:
    accepted = await branch_harness.accept()
    await branch_harness.finish(accepted)
    native = branch_harness.request()
    native.branch_document_id = None
    with pytest.raises(branches.BranchAccessError):
        await branch_harness.accept(request=native)
    async with branch_harness.factory() as db:
        with pytest.raises(branches.BranchAccessError):
            await execution._persist_user_message(
                db,
                cast(
                    User,
                    SimpleNamespace(
                        id=branch_harness.actor, organization_id=branch_harness.org
                    ),
                ),
                native,
            )
        await db.rollback()
        assert (
            await ChatService(db).create_assistant_message(
                branch_harness.chat, "Unscoped"
            )
            is None
        )


async def test_nonempty_legacy_history_cannot_be_reinterpreted(
    branch_harness: BranchHarness,
) -> None:
    request = branch_harness.request()
    request.branch_document_id = None
    accepted = await branch_harness.accept(request=request)
    await branch_harness.finish(accepted)
    with pytest.raises(branches.BranchAccessError, match="Start a new PDF chat"):
        await branch_harness.accept()
    async with branch_harness.factory() as db:
        thread = await db.get(Thread, branch_harness.chat)
        assert thread is not None and not thread.message_branching
        assert await db.scalar(select(func.count()).select_from(ChatMessage)) == 1


async def test_deleted_pdf_binding_does_not_revert_to_unscoped_thread(
    branch_harness: BranchHarness,
) -> None:
    accepted = await branch_harness.accept()
    await branch_harness.finish(accepted)
    async with branch_harness.factory() as db:
        await db.execute(
            text("DELETE FROM documents WHERE id=:id"), {"id": branch_harness.paper}
        )
        await db.commit()
        thread = await db.get(Thread, branch_harness.chat)
        assert thread is not None and thread.message_branching
        assert thread.branch_document_id is None
        with pytest.raises(branches.BranchAccessError, match="Document not found"):
            await branches.select_branch(
                db,
                actor_id=branch_harness.actor,
                thread_id=branch_harness.chat,
                message_id=UUID(str(accepted.user_message_id)),
            )


async def test_followup_can_parent_an_unanswered_user_message(
    branch_harness: BranchHarness,
) -> None:
    first = await branch_harness.accept()
    await branch_harness.finish(first)
    second = await branch_harness.accept(UUID(str(first.user_message_id)))
    async with branch_harness.factory() as db:
        path = await branches.get_branch_path(
            db,
            thread_id=branch_harness.chat,
            head_message_id=UUID(str(second.user_message_id)),
        )
        assert [row.id for row in path] == [
            UUID(str(first.user_message_id)),
            UUID(str(second.user_message_id)),
        ]


async def test_accept_and_select_serialize_on_thread(
    branch_harness: BranchHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior = await branch_harness.accept()
    await branch_harness.answer(prior)
    await branch_harness.finish(prior)
    inserted, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_selection = asyncio.Event()
    original = submission._insert_user_message
    original_messages = branches._messages
    accepting: asyncio.Task[Any] | None = None
    selecting: asyncio.Task[Any] | None = None

    async def pause_after_insert(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        inserted.set()
        await asyncio.wait_for(release.wait(), 10)
        return result

    async def pause_after_selection_read(
        db: AsyncSession, thread_id: UUID
    ) -> dict[UUID, ChatMessage]:
        rows = await original_messages(db, thread_id)
        if asyncio.current_task() is selecting:
            # If the early selection lock is removed, preserve the real
            # idle/history observation until the new acceptance commits.
            attempted.set()
            await asyncio.wait_for(release_selection.wait(), 10)
        return rows

    def observe_lock(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if (
            asyncio.current_task() is selecting
            and "FOR UPDATE" in statement
            and "threads" in statement
        ):
            attempted.set()

    async def select_old() -> None:
        async with branch_harness.factory() as db:
            await branches.select_branch(
                db,
                actor_id=branch_harness.actor,
                thread_id=branch_harness.chat,
                message_id=UUID(str(prior.user_message_id)),
            )

    monkeypatch.setattr(submission, "_insert_user_message", pause_after_insert)
    monkeypatch.setattr(branches, "_messages", pause_after_selection_read)
    event.listen(
        branch_harness.engine.sync_engine, "before_cursor_execute", observe_lock
    )
    try:
        accepting = asyncio.create_task(branch_harness.accept())
        await asyncio.wait_for(inserted.wait(), 10)
        selecting = asyncio.create_task(select_old())
        await asyncio.wait_for(attempted.wait(), 10)
        release.set()
        accepted = await asyncio.wait_for(accepting, 10)
        release_selection.set()
        with pytest.raises(branches.BranchAccessError, match="already in progress"):
            await asyncio.wait_for(selecting, 10)
        async with branch_harness.factory() as db:
            assert await db.scalar(
                select(Thread.selected_message_id).where(
                    Thread.id == branch_harness.chat
                )
            ) == UUID(str(accepted.user_message_id))
    finally:
        release.set()
        release_selection.set()
        tasks = [task for task in (accepting, selecting) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event.remove(
            branch_harness.engine.sync_engine, "before_cursor_execute", observe_lock
        )


BRANCH_INDEXES = ("ix_chat_messages_parent", "uq_chat_messages_branch_assistant")
BRANCH_FOREIGN_KEYS = (
    "fk_chat_messages_parent_message_id",
    "fk_threads_branch_document_id",
    "fk_threads_selected_message_id",
)


async def _branch_schema_state(engine: AsyncEngine) -> dict[str, bool]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT c.relname, i.indisvalid FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relnamespace = current_schema()::regnamespace "
                    "UNION ALL "
                    "SELECT conname, convalidated FROM pg_constraint "
                    "WHERE connamespace = current_schema()::regnamespace"
                )
            )
        ).all()
    wanted = (*BRANCH_INDEXES, *BRANCH_FOREIGN_KEYS)
    return {name: valid for name, valid in rows if name in wanted}


async def test_migration_roundtrip_unique_reply_and_retention_links(
    postgres_schema: AsyncEngine,
) -> None:
    path = (
        Path(__file__).resolve().parents[3]
        / "alembic/versions/pb01_pdf_message_branches.py"
    )
    spec = importlib.util.spec_from_file_location("pdf_branch_migration_test", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    executed: list[str] = []

    def migrate(connection: Any, direction: str) -> None:
        # Mirrors alembic/env.py: one context transaction that the migration's
        # autocommit_block commits before its CONCURRENTLY / VALIDATE steps.
        context = MigrationContext.configure(connection)
        with Operations.context(context), context.begin_transaction():
            getattr(migration, direction)()

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        executed.append(" ".join(statement.split()))

    async def run(direction: str) -> None:
        async with postgres_schema.connect() as connection:
            await connection.run_sync(lambda sync: migrate(sync, direction))

    event.listen(postgres_schema.sync_engine, "before_cursor_execute", record)
    try:
        async with postgres_schema.begin() as db:
            await db.execute(text("CREATE TABLE documents (id UUID PRIMARY KEY)"))
            await db.execute(text("CREATE TABLE threads (id UUID PRIMARY KEY)"))
            await db.execute(
                text(
                    "CREATE TABLE chat_messages (id UUID PRIMARY KEY, thread_id UUID REFERENCES threads(id) ON DELETE CASCADE, role TEXT NOT NULL)"
                )
            )
        await run("upgrade")
        await run("upgrade")
    finally:
        event.remove(postgres_schema.sync_engine, "before_cursor_execute", record)
    # The table scans happen outside the column transaction, without blocking.
    assert sum("CREATE UNIQUE INDEX CONCURRENTLY" in sql for sql in executed) == 2
    assert sum("CREATE INDEX CONCURRENTLY" in sql for sql in executed) == 2
    assert sum(sql.endswith("NOT VALID; END IF; END $$") for sql in executed) == 6
    assert sum("VALIDATE CONSTRAINT" in sql for sql in executed) == 6
    assert await _branch_schema_state(postgres_schema) == dict.fromkeys(
        (*BRANCH_INDEXES, *BRANCH_FOREIGN_KEYS), True
    )

    chat, user, assistant = (uuid4() for _ in range(3))
    async with postgres_schema.begin() as db:
        await db.execute(text("INSERT INTO threads(id) VALUES (:id)"), {"id": chat})
        await db.execute(
            text(
                "INSERT INTO chat_messages(id,thread_id,role) VALUES (:id,:chat,'user')"
            ),
            {"id": user, "chat": chat},
        )
        await db.execute(
            text(
                "INSERT INTO chat_messages(id,thread_id,role,parent_message_id) VALUES (:id,:chat,'assistant',:parent)"
            ),
            {"id": assistant, "chat": chat, "parent": user},
        )
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text(
                        "INSERT INTO chat_messages(id,thread_id,role,parent_message_id) VALUES (:id,:chat,'assistant',:parent)"
                    ),
                    {"id": uuid4(), "chat": chat, "parent": user},
                )

    # An interrupted concurrent build leaves an INVALID index that IF NOT EXISTS
    # would accept. Produce a real one: rebuild the unique index over a
    # duplicate reply, then remove the duplicate and re-run the upgrade.
    duplicate = uuid4()
    async with postgres_schema.connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.execute(
            text("DROP INDEX CONCURRENTLY uq_chat_messages_branch_assistant")
        )
        await autocommit.execute(
            text(
                "INSERT INTO chat_messages(id,thread_id,role,parent_message_id) VALUES (:id,:chat,'assistant',:parent)"
            ),
            {"id": duplicate, "chat": chat, "parent": user},
        )
        with pytest.raises(IntegrityError):
            await autocommit.execute(
                text(
                    "CREATE UNIQUE INDEX CONCURRENTLY uq_chat_messages_branch_assistant "
                    "ON chat_messages (parent_message_id) "
                    "WHERE parent_message_id IS NOT NULL AND role = 'assistant'"
                )
            )
        await autocommit.execute(
            text("DELETE FROM chat_messages WHERE id=:id"), {"id": duplicate}
        )
    assert (await _branch_schema_state(postgres_schema))[
        "uq_chat_messages_branch_assistant"
    ] is False
    await run("upgrade")
    assert await _branch_schema_state(postgres_schema) == dict.fromkeys(
        (*BRANCH_INDEXES, *BRANCH_FOREIGN_KEYS), True
    )

    async with postgres_schema.begin() as db:
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text(
                        "INSERT INTO chat_messages(id,thread_id,role,parent_message_id) VALUES (:id,:chat,'assistant',:parent)"
                    ),
                    {"id": uuid4(), "chat": chat, "parent": user},
                )
        await db.execute(
            text("UPDATE threads SET selected_message_id=:id WHERE id=:chat"),
            {"id": assistant, "chat": chat},
        )
        await db.execute(
            text("DELETE FROM chat_messages WHERE thread_id=:chat"), {"chat": chat}
        )
        assert (
            await db.scalar(
                text("SELECT selected_message_id FROM threads WHERE id=:chat"),
                {"chat": chat},
            )
            is None
        )
        await db.execute(text("DELETE FROM threads WHERE id=:chat"), {"chat": chat})
    await run("downgrade")
    await run("downgrade")
    async with postgres_schema.connect() as db:
        columns = list(
            (
                await db.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='threads'"
                    )
                )
            ).scalars()
        )
    assert columns == ["id"]
    assert await _branch_schema_state(postgres_schema) == {}
    await run("upgrade")
    assert await _branch_schema_state(postgres_schema) == dict.fromkeys(
        (*BRANCH_INDEXES, *BRANCH_FOREIGN_KEYS), True
    )
