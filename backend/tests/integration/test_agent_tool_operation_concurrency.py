"""Real-Postgres evidence for atomic and concurrent agent tool operations."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Coroutine, cast
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.runnables import RunnableConfig
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.agent_tool_receipt import AgentToolOperation
from src.models.collection import Collection, CollectionDocument
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.organization import Organization
from src.models.project_note import ProjectNote
from src.models.user import User
from src.models.workspace import Workspace
from src.services.agent.tool_operations import ToolOperationKey
from src.services.agent.tools_impl import _MAX_TOOL_RESULT_BYTES

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


@dataclass(frozen=True)
class _ToolDatabase:
    session_factory: async_sessionmaker[AsyncSession]
    user_id: uuid.UUID
    organization_id: uuid.UUID
    workspace_id: uuid.UUID
    document_id: uuid.UUID


class _InsertWatchingSession:
    """Signal when a second real session reaches the unique operation insert."""

    def __init__(self, db: AsyncSession, insert_started: asyncio.Event) -> None:
        self._db = db
        self._insert_started = insert_started

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "INSERT INTO agent_tool_operations" in str(statement):
            self._insert_started.set()
        return await self._db.execute(statement, *args, **kwargs)


def _async_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    raise ValueError("ORCHESTRATION_TEST_DATABASE_URL must be a PostgreSQL URL")


def _ensure_fixture_encryption() -> Any:
    """Install an in-memory field key so the real User model loads cleanly."""
    from src.core import encryption

    try:
        return encryption.get_field_encryption()
    except encryption.EncryptionError:
        manager = object.__new__(encryption.KeyManager)
        manager.master_key_env_var = "TASK_2_TEST_ONLY"
        manager._keys = {}
        manager._master_key = b"\x00" * 32
        manager.generate_key(encryption.EncryptionKeyType.DATA)
        aes = encryption.AESEncryption(manager)
        field_encryption = encryption.FieldEncryption(aes)
        encryption._key_manager = manager
        encryption._aes_encryption = aes
        encryption._field_encryption = field_encryption
        return field_encryption


@asynccontextmanager
async def _postgres_tool_schema(dsn: str | None) -> AsyncIterator[_ToolDatabase]:
    """Create a disposable schema containing real local business tables."""
    if dsn is None:
        raise ValueError("ORCHESTRATION_TEST_DATABASE_URL is required")
    field_encryption = _ensure_fixture_encryption()
    schema = "agent_tool_ops_" + uuid.uuid4().hex
    admin_engine = create_async_engine(_async_dsn(dsn))
    try:
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')

        engine = create_async_engine(
            _async_dsn(dsn),
            connect_args={"server_settings": {"search_path": schema}},
        )
        try:
            async with engine.begin() as connection:
                for model in (
                    Organization,
                    User,
                    Workspace,
                    Collection,
                    Document,
                    CollectionDocument,
                    ProjectNote,
                    AgentToolOperation,
                ):
                    await connection.run_sync(cast(Any, model).__table__.create)

            user_id = uuid.uuid4()
            organization_id = uuid.uuid4()
            workspace_id = uuid.uuid4()
            document_id = uuid.uuid4()
            async with engine.begin() as connection:
                await connection.execute(
                    text("""INSERT INTO organizations (
                            id, created_at, updated_at, is_deleted, name,
                            storage_tier, storage_used_bytes, storage_limit_bytes,
                            is_active
                        ) VALUES (
                            :id, now(), now(), false, :name, 'FREE', 0,
                            10737418240, true
                        )"""),
                    {"id": organization_id, "name": f"org-{schema}"},
                )
                await connection.execute(
                    text("""INSERT INTO users (
                            id, created_at, updated_at, is_deleted, email,
                            password_hash, first_name, last_name, role, is_active,
                            organization_id, login_count
                        ) VALUES (
                            :id, now(), now(), false, :email, 'unused-test-hash',
                            :first_name, :last_name, 'USER', true,
                            :organization_id, 0
                        )"""),
                    {
                        "id": user_id,
                        "email": f"{user_id}@example.test",
                        "first_name": field_encryption.encrypt_field(
                            "Operation", "first_name"
                        ),
                        "last_name": field_encryption.encrypt_field(
                            "Test", "last_name"
                        ),
                        "organization_id": organization_id,
                    },
                )

            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as setup:
                setup.add(
                    Workspace(
                        id=workspace_id,
                        name="Task 2 operation workspace",
                        owner_id=user_id,
                        organization_id=organization_id,
                    )
                )
                setup.add(
                    Document(
                        id=document_id,
                        title="Task 2 source document",
                        filename="source.pdf",
                        file_path="/unused/source.pdf",
                        file_size_bytes=17,
                        mime_type="application/pdf",
                        document_type=DocumentType.PDF,
                        processing_status=ProcessingStatus.PENDING,
                        organization_id=organization_id,
                        uploaded_by_user_id=user_id,
                    )
                )
                await setup.commit()

            yield _ToolDatabase(
                session_factory=factory,
                user_id=user_id,
                organization_id=organization_id,
                workspace_id=workspace_id,
                document_id=document_id,
            )
        finally:
            await engine.dispose()
    finally:
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(
                f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
            )
        await admin_engine.dispose()


def _operation_key(
    *,
    database: _ToolDatabase,
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str,
    turn_id: str = "turn-pg-1",
    thread_id: str = "thread-pg-1",
) -> ToolOperationKey:
    from src.services.agent.tools_impl import _validated_tool_arguments

    effective_args = _validated_tool_arguments(tool_name, arguments)
    return ToolOperationKey.from_context(
        organization_id=database.organization_id,
        user_id=database.user_id,
        thread_id=thread_id,
        turn_id=turn_id,
        tool_call_id=call_id,
        tool_name=tool_name,
        arguments=effective_args,
    )


def _tool_session_scope(
    factory: async_sessionmaker[AsyncSession],
    *,
    wrap_second_session: Callable[[AsyncSession], AsyncSession] | None = None,
) -> Callable[[], Any]:
    opened = 0

    @asynccontextmanager
    async def _scope() -> AsyncIterator[AsyncSession]:
        nonlocal opened
        opened += 1
        async with factory() as session:
            if opened == 2 and wrap_second_session is not None:
                yield wrap_second_session(session)
            else:
                yield session

    return _scope


def _execute_scalar_tool(
    database: _ToolDatabase,
    tool_name: str,
    arguments: dict[str, Any],
    operation_key: ToolOperationKey,
) -> Coroutine[Any, Any, dict[str, Any]]:
    from src.services.agent.tools_impl import execute_tool

    return execute_tool(
        tool_name,
        arguments,
        user_id=str(database.user_id),
        organization_id=str(database.organization_id),
        thread_id=operation_key.thread_id,
        operation_key=operation_key,
    )


async def _create_pg_project(database: _ToolDatabase, call_id: str) -> str:
    """Create a real project row for draft recovery integration cases."""
    from src.services.agent import tools_impl

    arguments = {
        "name": f"Task 2 draft project {call_id}",
        "workspace_id": str(database.workspace_id),
    }
    key = _operation_key(
        database=database,
        tool_name="create_project",
        arguments=arguments,
        call_id=call_id,
    )
    with patch(
        "src.services.agent.tool_session.tool_session",
        _tool_session_scope(database.session_factory),
    ):
        result = await tools_impl.execute_tool(
            "create_project",
            arguments,
            user_id=str(database.user_id),
            organization_id=str(database.organization_id),
            thread_id=key.thread_id,
            operation_key=key,
        )
    return str(result["project_id"])


async def _seed_dispatched_operation(
    database: _ToolDatabase,
    key: ToolOperationKey,
    result: dict[str, Any],
) -> None:
    """Persist a realistic external dispatch using the production claim API."""
    from src.services.agent.tool_operations import claim_operation, record_dispatch

    async with database.session_factory() as session:
        async with session.begin():
            claim = await claim_operation(session, key, external=True)
            assert claim.status == "claimed"
            assert claim.owner_token is not None
            await record_dispatch(session, key, claim.owner_token, result)


def _encoded_result_size(result: Any) -> int:
    return len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))


async def test_postgres_local_effects_commit_with_results_and_replay_ids() -> None:
    """All three local mutations persist their actual effects with results."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent.tools_impl import execute_tool

    async with _postgres_tool_schema(dsn) as database:
        project_args = {
            "name": "Durable operation project",
            "workspace_id": str(database.workspace_id),
        }
        project_key = _operation_key(
            database=database,
            tool_name="create_project",
            arguments=project_args,
            call_id="same-raw-provider-id",
        )
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            project_result = await execute_tool(
                "create_project",
                project_args,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=project_key.thread_id,
                operation_key=project_key,
            )

        assert project_result["status"] == "success"
        project_id = uuid.UUID(project_result["project_id"])

        # Re-enter through the production scalar-ID path after the original
        # tool session has closed. The saved result is the replay response.
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            replay = await execute_tool(
                "create_project",
                project_args,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=project_key.thread_id,
                operation_key=project_key,
            )
        assert replay == project_result

        note_args = {
            "project_id": str(project_id),
            "title": "Durable note",
            "content": "Stored in PostgreSQL with its operation result.",
        }
        note_key = _operation_key(
            database=database,
            tool_name="create_project_note",
            arguments=note_args,
            call_id="note-provider-id",
        )
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            note_result = await execute_tool(
                "create_project_note",
                note_args,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=note_key.thread_id,
                operation_key=note_key,
            )

        attach_args = {
            "project_id": str(project_id),
            "document_id": str(database.document_id),
        }
        attach_key = _operation_key(
            database=database,
            tool_name="add_document_to_project",
            arguments=attach_args,
            call_id="attach-provider-id",
        )
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            attach_result = await execute_tool(
                "add_document_to_project",
                attach_args,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=attach_key.thread_id,
                operation_key=attach_key,
            )

        assert note_result["status"] == "success"
        assert note_result["project_id"] == str(project_id)
        assert attach_result["status"] == "success"
        async with database.session_factory() as verify:
            project_count = await verify.scalar(
                select(func.count(Collection.id)).where(Collection.id == project_id)
            )
            notes = await verify.scalars(
                select(ProjectNote).where(ProjectNote.project_id == project_id)
            )
            links = await verify.scalars(
                select(CollectionDocument).where(
                    CollectionDocument.collection_id == project_id
                )
            )
            operation_count = await verify.scalar(
                select(func.count(AgentToolOperation.operation_id))
            )
            assert project_count == 1
            assert [note.id for note in notes] == [uuid.UUID(note_result["note_id"])]
            assert [link.document_id for link in links] == [database.document_id]
            assert operation_count == 3


async def test_postgres_cap_is_the_saved_returned_and_replayed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real executor stores exactly the bounded artifact response."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent import tools_impl

    async with _postgres_tool_schema(dsn) as database:
        arguments = {
            "name": "Large bounded result project",
            "workspace_id": str(database.workspace_id),
        }
        key = _operation_key(
            database=database,
            tool_name="create_project",
            arguments=arguments,
            call_id="large-result-provider-id",
        )
        original_create = tools_impl._tool_create_project

        async def _large_result(
            args: dict[str, Any],
            db: AsyncSession | None,
            current_user: User | None,
            *,
            commit: bool = True,
        ) -> dict[str, Any]:
            result = await original_create(args, db, current_user, commit=commit)
            result["documents"] = [
                {
                    "id": str(uuid.uuid4()),
                    "title": f"文献 {index} 📄" + ("λ" * 420),
                    "status": "indexed",
                    "project_id": result["project_id"],
                }
                for index in range(96)
            ]
            result["message"] = "研究結果 🔬 " * 9000
            return result

        monkeypatch.setattr(tools_impl, "_tool_create_project", _large_result)
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            returned = await tools_impl.execute_tool(
                "create_project",
                arguments,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=key.thread_id,
                operation_key=key,
            )

        assert len(tools_impl._result_json(returned).encode("utf-8")) <= (
            _MAX_TOOL_RESULT_BYTES
        )
        assert returned["truncated"] is True
        assert returned["status"] == "success"
        assert uuid.UUID(returned["project_id"])
        assert returned["_tool_result_bounds"]["identity_coverage"]["incomplete"]

        async with database.session_factory() as verify:
            operation = await verify.get(AgentToolOperation, key.operation_id)
            assert operation is not None
            assert operation.state == "completed"
            assert operation.result == returned

        monkeypatch.setattr(tools_impl, "_tool_create_project", original_create)
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            replay = await tools_impl.execute_tool(
                "create_project",
                arguments,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=key.thread_id,
                operation_key=key,
            )
        assert replay == returned
        async with database.session_factory() as verify:
            project_count = await verify.scalar(
                select(func.count(Collection.id)).where(
                    Collection.id == uuid.UUID(returned["project_id"])
                )
            )
            assert project_count == 1


async def test_postgres_local_effect_and_claim_roll_back_on_result_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-flush result write failure leaves neither durable half behind."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent import tool_operations
    from src.services.agent.tools_impl import execute_tool

    async with _postgres_tool_schema(dsn) as database:
        arguments = {
            "name": "Must roll back",
            "workspace_id": str(database.workspace_id),
        }
        key = _operation_key(
            database=database,
            tool_name="create_project",
            arguments=arguments,
            call_id="rollback-provider-id",
        )

        async def _fail_complete(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("injected result write failure")

        monkeypatch.setattr(tool_operations, "complete_operation", _fail_complete)
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            try:
                await execute_tool(
                    "create_project",
                    arguments,
                    user_id=str(database.user_id),
                    organization_id=str(database.organization_id),
                    thread_id=key.thread_id,
                    operation_key=key,
                )
            except RuntimeError as error:
                assert str(error) == "injected result write failure"

        async with database.session_factory() as verify:
            projects = await verify.scalar(select(func.count(Collection.id)))
            operations = await verify.scalar(
                select(func.count(AgentToolOperation.operation_id))
            )
            assert projects == 0
            assert operations == 0


async def test_postgres_only_claim_owner_can_record_completion() -> None:
    """A stale worker token cannot complete another operation's claim."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent.tool_operations import claim_operation, complete_operation

    async with _postgres_tool_schema(dsn) as database:
        arguments = {
            "name": "Owner compare-and-set project",
            "workspace_id": str(database.workspace_id),
        }
        key = _operation_key(
            database=database,
            tool_name="create_project",
            arguments=arguments,
            call_id="owner-cas-provider-id",
        )
        async with database.session_factory() as session:
            async with session.begin():
                claim = await claim_operation(session, key)
        assert claim.status == "claimed"
        assert claim.owner_token is not None

        async with database.session_factory() as session:
            async with session.begin():
                with pytest.raises(
                    RuntimeError,
                    match="operation state update lost its claim or fingerprint",
                ):
                    await complete_operation(
                        session,
                        key,
                        uuid.uuid4(),
                        {"status": "completed", "project_id": str(uuid.uuid4())},
                    )

        wrong_fingerprint = replace(key, args_hash="0" * 64)
        async with database.session_factory() as session:
            async with session.begin():
                with pytest.raises(
                    RuntimeError,
                    match="operation state update lost its claim or fingerprint",
                ):
                    await complete_operation(
                        session,
                        wrong_fingerprint,
                        claim.owner_token,
                        {"status": "wrong fingerprint"},
                    )

        async with database.session_factory() as session:
            async with session.begin():
                with pytest.raises(
                    RuntimeError,
                    match="operation state update lost its claim or fingerprint",
                ):
                    async with session.begin_nested():
                        await session.execute(
                            update(AgentToolOperation)
                            .where(AgentToolOperation.operation_id == key.operation_id)
                            .values(thread_id="changed-out-of-band")
                        )
                        await complete_operation(
                            session,
                            key,
                            claim.owner_token,
                            {"status": "wrong full scope"},
                        )

        async with database.session_factory() as verify:
            operation = await verify.get(AgentToolOperation, key.operation_id)
            assert operation is not None
            assert operation.state == "claimed"
            assert operation.result is None

        async with database.session_factory() as session:
            async with session.begin():
                await complete_operation(
                    session,
                    key,
                    claim.owner_token,
                    {"status": "completed", "project_id": str(uuid.uuid4())},
                )

        async with database.session_factory() as verify:
            operation = await verify.get(AgentToolOperation, key.operation_id)
            assert operation is not None
            assert operation.state == "completed"
            assert operation.result["status"] == "completed"


async def test_postgres_same_scoped_operation_concurrently_creates_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real unique-index loser returns the committed winner's result."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent import tools_impl

    async with _postgres_tool_schema(dsn) as database:
        arguments = {
            "name": "Only one concurrent project",
            "workspace_id": str(database.workspace_id),
        }
        key = _operation_key(
            database=database,
            tool_name="create_project",
            arguments=arguments,
            call_id="concurrent-provider-id",
        )
        first_effect = asyncio.Event()
        release_first = asyncio.Event()
        second_insert = asyncio.Event()
        original_dispatch = tools_impl._dispatch_tool
        dispatch_count = 0

        async def _paused_dispatch(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal dispatch_count
            result = await original_dispatch(*args, **kwargs)
            dispatch_count += 1
            if dispatch_count == 1:
                first_effect.set()
                await release_first.wait()
            return result

        monkeypatch.setattr(tools_impl, "_dispatch_tool", _paused_dispatch)
        scope = _tool_session_scope(
            database.session_factory,
            wrap_second_session=lambda session: cast(
                AsyncSession,
                _InsertWatchingSession(session, second_insert),
            ),
        )
        with patch("src.services.agent.tool_session.tool_session", scope):
            first = asyncio.create_task(
                _execute_scalar_tool(database, "create_project", arguments, key)
            )
            await asyncio.wait_for(first_effect.wait(), timeout=5)
            second = asyncio.create_task(
                _execute_scalar_tool(database, "create_project", arguments, key)
            )
            await asyncio.wait_for(second_insert.wait(), timeout=5)
            release_first.set()
            first_result, second_result = await asyncio.gather(first, second)

        assert first_result == second_result
        assert dispatch_count == 1
        async with database.session_factory() as verify:
            projects = await verify.scalars(
                select(Collection).where(Collection.name == arguments["name"])
            )
            operations = await verify.scalars(select(AgentToolOperation))
            project_rows = list(projects)
            operation_rows = list(operations)
            assert len(project_rows) == 1
            assert len(operation_rows) == 1
            assert operation_rows[0].state == "completed"


async def test_postgres_raw_call_id_is_scoped_and_fingerprint_conflicts() -> None:
    """Turn and actor scopes separate calls while changed args cannot reuse one."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent import tool_operations
    from src.services.agent.tools_impl import execute_tool

    async with _postgres_tool_schema(dsn) as database:
        arguments = {
            "name": "Reusable provider ID project",
            "workspace_id": str(database.workspace_id),
        }
        first_key = _operation_key(
            database=database,
            tool_name="create_project",
            arguments=arguments,
            call_id="provider-reuses-this-id",
            turn_id="turn-before-reuse",
        )
        second_user_id = uuid.uuid4()
        second_organization_id = uuid.uuid4()
        second_workspace_id = uuid.uuid4()
        field_encryption = _ensure_fixture_encryption()
        async with database.session_factory() as setup:
            await setup.execute(
                text("""INSERT INTO organizations (
                        id, created_at, updated_at, is_deleted, name,
                        storage_tier, storage_used_bytes, storage_limit_bytes,
                        is_active
                    ) VALUES (
                        :id, now(), now(), false, :name, 'FREE', 0,
                        10737418240, true
                    )"""),
                {
                    "id": second_organization_id,
                    "name": f"org-second-{second_organization_id}",
                },
            )
            await setup.execute(
                text("""INSERT INTO users (
                        id, created_at, updated_at, is_deleted, email,
                        password_hash, first_name, last_name, role, is_active,
                        organization_id, login_count
                    ) VALUES (
                        :id, now(), now(), false, :email, 'unused-test-hash',
                        :first_name, :last_name, 'USER', true, :organization_id, 0
                    )"""),
                {
                    "id": second_user_id,
                    "email": f"{second_user_id}@example.test",
                    "first_name": field_encryption.encrypt_field(
                        "Second", "first_name"
                    ),
                    "last_name": field_encryption.encrypt_field("Actor", "last_name"),
                    "organization_id": second_organization_id,
                },
            )
            setup.add(
                Workspace(
                    id=second_workspace_id,
                    name="Second actor operation workspace",
                    owner_id=second_user_id,
                    organization_id=second_organization_id,
                )
            )
            await setup.commit()
        second_database = _ToolDatabase(
            session_factory=database.session_factory,
            user_id=second_user_id,
            organization_id=second_organization_id,
            workspace_id=second_workspace_id,
            document_id=database.document_id,
        )
        second_arguments = {
            "name": arguments["name"],
            "workspace_id": str(second_workspace_id),
        }
        second_key = _operation_key(
            database=second_database,
            tool_name="create_project",
            arguments=second_arguments,
            call_id="provider-reuses-this-id",
            turn_id="turn-after-reuse",
            thread_id="thread-after-reuse",
        )
        later_turn_key = _operation_key(
            database=database,
            tool_name="create_project",
            arguments=arguments,
            call_id="provider-reuses-this-id",
            turn_id="later-turn-same-thread",
        )
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            first_result = await execute_tool(
                "create_project",
                arguments,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=first_key.thread_id,
                operation_key=first_key,
            )
            second_result = await execute_tool(
                "create_project",
                second_arguments,
                user_id=str(second_database.user_id),
                organization_id=str(second_database.organization_id),
                thread_id=second_key.thread_id,
                operation_key=second_key,
            )
            later_turn_result = await execute_tool(
                "create_project",
                arguments,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=later_turn_key.thread_id,
                operation_key=later_turn_key,
            )

        assert first_result["project_id"] != second_result["project_id"]
        assert first_key.operation_id != second_key.operation_id
        assert first_key.operation_id != later_turn_key.operation_id
        assert first_result["project_id"] != later_turn_result["project_id"]

        changed_arguments = {
            **arguments,
            "name": "Changed fingerprint must not overwrite",
        }
        conflicting_key = _operation_key(
            database=database,
            tool_name="create_project",
            arguments=changed_arguments,
            call_id="provider-reuses-this-id",
            turn_id="turn-before-reuse",
        )
        assert conflicting_key.operation_id == first_key.operation_id
        async with database.session_factory() as session:
            async with session.begin():
                claim = await tool_operations.claim_operation(session, conflicting_key)
        assert claim.status == "conflict"
        assert claim.result is None

        async with database.session_factory() as verify:
            projects = await verify.scalars(select(Collection))
            operations = await verify.scalars(select(AgentToolOperation))
            assert len(list(projects)) == 3
            assert len(list(operations)) == 3


async def test_postgres_authentication_precedes_saved_result_lookup() -> None:
    """A result is hidden from unauthenticated and wrong-actor scalar callers."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent.tools_impl import execute_tool

    async with _postgres_tool_schema(dsn) as database:
        arguments = {
            "name": "Private saved result",
            "workspace_id": str(database.workspace_id),
        }
        key = _operation_key(
            database=database,
            tool_name="create_project",
            arguments=arguments,
            call_id="private-result-provider-id",
        )
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            saved = await execute_tool(
                "create_project",
                arguments,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=key.thread_id,
                operation_key=key,
            )

        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            denied = await execute_tool(
                "create_project",
                arguments,
                user_id=str(uuid.uuid4()),
                organization_id=str(database.organization_id),
                thread_id=key.thread_id,
                operation_key=key,
            )

        second_user_id = uuid.uuid4()
        field_encryption = _ensure_fixture_encryption()
        async with database.session_factory() as setup:
            await setup.execute(
                text("""INSERT INTO users (
                        id, created_at, updated_at, is_deleted, email,
                        password_hash, first_name, last_name, role, is_active,
                        organization_id, login_count
                    ) VALUES (
                        :id, now(), now(), false, :email, 'unused-test-hash',
                        :first_name, :last_name, 'USER', true,
                        :organization_id, 0
                    )"""),
                {
                    "id": second_user_id,
                    "email": f"{second_user_id}@example.test",
                    "first_name": field_encryption.encrypt_field(
                        "Second", "first_name"
                    ),
                    "last_name": field_encryption.encrypt_field("Actor", "last_name"),
                    "organization_id": database.organization_id,
                },
            )
            await setup.commit()

        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            wrong_actor = await execute_tool(
                "create_project",
                arguments,
                user_id=str(second_user_id),
                organization_id=str(database.organization_id),
                thread_id=key.thread_id,
                operation_key=key,
            )

        assert denied["error_category"] == "tool_authentication_failed"
        assert denied["automatic_retry_allowed"] is False
        assert "project_id" not in denied
        assert "project_id" not in wrong_actor
        assert wrong_actor["error_category"] == "operation_context_conflict"
        assert saved["project_id"]


async def test_postgres_external_claim_store_failure_never_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External effects fail closed when their durable claim cannot be written."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent import tool_operations, tools_impl

    async with _postgres_tool_schema(dsn) as database:
        arguments = {
            "code": "print('offline test')",
            "description": "Claim-store failure protection",
            "language": "python",
            "packages": None,
        }
        key = _operation_key(
            database=database,
            tool_name="execute_code",
            arguments=arguments,
            call_id="must-not-dispatch-provider-id",
        )
        dispatch_count = 0

        async def _fail_claim(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("injected operation-store failure")

        async def _dispatch(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal dispatch_count
            dispatch_count += 1
            return {"status": "success"}

        monkeypatch.setattr(tool_operations, "claim_operation", _fail_claim)
        monkeypatch.setattr(tools_impl, "_dispatch_tool", _dispatch)
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            result = await tools_impl.execute_tool(
                "execute_code",
                arguments,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=key.thread_id,
                operation_key=key,
            )

        assert dispatch_count == 0
        assert result["error_category"] == "operation_store_unavailable"
        assert result["automatic_retry_allowed"] is False

        local_arguments = {
            "name": "No project without a durable claim",
            "workspace_id": str(database.workspace_id),
        }
        local_key = _operation_key(
            database=database,
            tool_name="create_project",
            arguments=local_arguments,
            call_id="local-claim-store-failure",
        )
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            with pytest.raises(RuntimeError, match="injected operation-store failure"):
                await tools_impl.execute_tool(
                    "create_project",
                    local_arguments,
                    user_id=str(database.user_id),
                    organization_id=str(database.organization_id),
                    thread_id=local_key.thread_id,
                    operation_key=local_key,
                )

        assert dispatch_count == 0
        async with database.session_factory() as verify:
            assert (
                await verify.scalar(select(func.count(AgentToolOperation.operation_id)))
                == 0
            )
            assert await verify.scalar(select(func.count(Collection.id))) == 0


async def test_postgres_draft_identity_is_committed_before_status_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Draft polling sees a durable task key with no request-session transaction."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent import tools_impl
    from src.services.research import draft_generation_service

    async with _postgres_tool_schema(dsn) as database:
        project_args = {
            "name": "Draft status project",
            "workspace_id": str(database.workspace_id),
        }
        project_key = _operation_key(
            database=database,
            tool_name="create_project",
            arguments=project_args,
            call_id="draft-project-provider-id",
        )
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            project = await tools_impl.execute_tool(
                "create_project",
                project_args,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=project_key.thread_id,
                operation_key=project_key,
            )
        project_id = project["project_id"]
        draft_args = {
            "project_id": project_id,
            "themes": ["durable task identity"],
            "style": "summary",
        }
        draft_key = _operation_key(
            database=database,
            tool_name="create_draft",
            arguments=draft_args,
            call_id="draft-provider-id",
            turn_id="draft-turn-1",
        )
        observed: dict[str, Any] = {}
        task_id = "draft-task-arbitrary-id-17"
        draft_id = str(uuid.uuid4())

        def _init(self: Any, db: AsyncSession) -> None:
            self.db = db

        async def _generate(
            self: Any,
            *,
            project_id: uuid.UUID,
            user_id: uuid.UUID,
            themes: list[str],
            style: str,
        ) -> dict[str, Any]:
            assert self.db.in_transaction() is False
            observed["project_id"] = project_id
            observed["user_id"] = user_id
            observed["themes"] = themes
            observed["style"] = style
            return {
                "task_id": task_id,
                "status": "pending",
                "message": "Draft generation started",
            }

        async def _wait(
            _cls: type[Any],
            requested_task_id: str,
            *,
            timeout_seconds: float = 120.0,
        ) -> dict[str, Any]:
            assert requested_task_id == task_id
            assert timeout_seconds == 105.0
            assert observed["db"].in_transaction() is False
            async with database.session_factory() as inspect_session:
                operation = await inspect_session.get(
                    AgentToolOperation, draft_key.operation_id
                )
                assert operation is not None
                assert operation.state == "dispatched"
                assert operation.result == {
                    "task_id": task_id,
                    "project_id": project_id,
                    "project_name": "Draft status project",
                    "user_id": str(database.user_id),
                    "status": "pending",
                    "message": "Draft generation started",
                }
            return {
                "task_id": task_id,
                "status": "completed",
                "draft_id": draft_id,
                "current_step": "Draft completed",
            }

        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService, "__init__", _init
        )
        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService,
            "generate_draft",
            _generate,
        )
        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService,
            "wait_for_terminal_status",
            classmethod(_wait),
        )
        original_dispatch = tools_impl._dispatch_tool

        async def _observe_dispatch(*args: Any, **kwargs: Any) -> dict[str, Any]:
            session = args[3]
            assert isinstance(session, AsyncSession)
            observed["db"] = session
            return await original_dispatch(*args, **kwargs)

        monkeypatch.setattr(tools_impl, "_dispatch_tool", _observe_dispatch)
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            result = await tools_impl.execute_tool(
                "create_draft",
                draft_args,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=draft_key.thread_id,
                operation_key=draft_key,
            )

        assert result["status"] == "completed"
        assert result["task_id"] == task_id
        assert result["draft_id"] == draft_id
        assert result["project_id"] == project_id
        assert result["project_name"] == "Draft status project"
        assert observed["project_id"] == uuid.UUID(project_id)
        assert observed["user_id"] == database.user_id
        async with database.session_factory() as verify:
            operation = await verify.get(AgentToolOperation, draft_key.operation_id)
            assert operation is not None
            assert operation.state == "completed"
            assert operation.result == result


async def test_postgres_draft_replay_uses_shared_status_and_bounds_saved_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved draft task resumes by status lookup and generation runs once."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent import tools_impl
    from src.services.research import draft_generation_service

    async with _postgres_tool_schema(dsn) as database:
        project_args = {
            "name": "Recoverable draft project",
            "workspace_id": str(database.workspace_id),
        }
        project_key = _operation_key(
            database=database,
            tool_name="create_project",
            arguments=project_args,
            call_id="recovery-project-provider-id",
        )
        opened_sessions: list[AsyncSession] = []

        @asynccontextmanager
        async def _tracked_scope() -> AsyncIterator[AsyncSession]:
            async with database.session_factory() as session:
                opened_sessions.append(session)
                yield session

        with patch("src.services.agent.tool_session.tool_session", _tracked_scope):
            project = await tools_impl.execute_tool(
                "create_project",
                project_args,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=project_key.thread_id,
                operation_key=project_key,
            )

        project_id = project["project_id"]
        arguments = {
            "project_id": project_id,
            "themes": ["recover the accepted task"],
            "style": "summary",
        }
        key = _operation_key(
            database=database,
            tool_name="create_draft",
            arguments=arguments,
            call_id="recoverable-draft-provider-id",
            turn_id="recoverable-draft-turn",
        )
        task_id = "draft-recovery-task-17"
        draft_id = str(uuid.uuid4())
        generation_calls = 0
        recovery_wait_calls = 0
        status_phase = "pending"

        def _init(self: Any, db: AsyncSession) -> None:
            self.db = db

        async def _generate(
            self: Any,
            *,
            project_id: uuid.UUID,
            user_id: uuid.UUID,
            themes: list[str],
            style: str,
        ) -> dict[str, Any]:
            nonlocal generation_calls
            generation_calls += 1
            assert self.db.in_transaction() is False
            assert project_id == uuid.UUID(project["project_id"])
            assert user_id == database.user_id
            assert themes == ["recover the accepted task"]
            assert style == "summary"
            return {
                "task_id": task_id,
                "status": "pending",
                "message": "Draft generation started",
            }

        async def _initial_wait(
            _cls: type[Any],
            requested_task_id: str,
            *,
            timeout_seconds: float = 120.0,
        ) -> dict[str, Any]:
            assert requested_task_id == task_id
            assert timeout_seconds == 105.0
            assert all(not session.in_transaction() for session in opened_sessions)
            async with database.session_factory() as inspect_session:
                operation = await inspect_session.get(
                    AgentToolOperation, key.operation_id
                )
                assert operation is not None
                assert operation.state == "dispatched"
                assert operation.result["task_id"] == task_id
                assert operation.result["project_id"] == project_id
                assert operation.result["user_id"] == str(database.user_id)
            return {
                "task_id": task_id,
                "status": "pending",
                "project_id": project_id,
                "user_id": str(database.user_id),
                "current_step": "Still generating",
            }

        class _SharedStatusStore:
            def __init__(self) -> None:
                self.reads = 0
                self.observed_statuses: list[str] = []

            async def get(self, redis_key: str) -> bytes:
                assert redis_key == f"research:draft-status:{task_id}"
                assert all(not session.in_transaction() for session in opened_sessions)
                self.reads += 1
                status: dict[str, Any] = {
                    "task_id": task_id,
                    "status": status_phase,
                    "project_id": project_id,
                    "user_id": str(database.user_id),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "current_step": "研究生成中 📚 " * 9000,
                    "documents": [
                        {
                            "id": str(uuid.uuid4()),
                            "title": f"Recovered source {index}",
                            "status": (
                                "ready" if status_phase == "completed" else "pending"
                            ),
                            "project_id": project_id,
                        }
                        for index in range(80)
                    ],
                }
                if status_phase == "completed":
                    status["draft_id"] = draft_id
                self.observed_statuses.append(status_phase)
                return json.dumps(status).encode("utf-8")

        shared_store = _SharedStatusStore()

        async def _get_test_redis() -> _SharedStatusStore:
            return shared_store

        # The real get_status_shared() implementation must find this key in
        # the fake shared store while the process-local task cache stays empty.
        assert draft_generation_service._generation_status.get(task_id) is None
        monkeypatch.setattr(draft_generation_service, "get_redis", _get_test_redis)

        async def _recovery_wait(
            _cls: type[Any],
            requested_task_id: str,
            *,
            timeout_seconds: float = 120.0,
        ) -> dict[str, Any]:
            nonlocal recovery_wait_calls
            recovery_wait_calls += 1
            assert requested_task_id == task_id
            assert timeout_seconds == 105.0
            assert all(not session.in_transaction() for session in opened_sessions)
            raise AssertionError("recovery must use the shared status snapshot")

        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService, "__init__", _init
        )
        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService,
            "generate_draft",
            _generate,
        )
        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService,
            "wait_for_terminal_status",
            classmethod(_initial_wait),
        )
        with patch("src.services.agent.tool_session.tool_session", _tracked_scope):
            first_result = await tools_impl.execute_tool(
                "create_draft",
                arguments,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=key.thread_id,
                operation_key=key,
            )

        assert first_result["status"] == "pending"
        assert first_result["task_id"] == task_id
        async with database.session_factory() as verify:
            operation = await verify.get(AgentToolOperation, key.operation_id)
            assert operation is not None
            assert operation.state == "dispatched"
            assert operation.result["task_id"] == task_id

        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService,
            "wait_for_terminal_status",
            classmethod(_recovery_wait),
        )
        with patch("src.services.agent.tool_session.tool_session", _tracked_scope):
            pending_replay = await tools_impl.execute_tool(
                "create_draft",
                arguments,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=key.thread_id,
                operation_key=key,
            )

        assert pending_replay["status"] == "pending"
        assert pending_replay["task_id"] == task_id
        assert _encoded_result_size(pending_replay) <= _MAX_TOOL_RESULT_BYTES
        assert pending_replay["project_id"] == project_id
        assert (
            pending_replay["_tool_result_bounds"]["identity_coverage"]["incomplete"]
            is True
        )
        assert (
            pending_replay["_tool_result_bounds"]["identity_coverage"][
                "omitted_entries"
            ]
            > 0
        )
        assert recovery_wait_calls == 0
        assert shared_store.reads == 1
        assert shared_store.observed_statuses == ["pending"]
        async with database.session_factory() as verify:
            operation = await verify.get(AgentToolOperation, key.operation_id)
            assert operation is not None
            assert operation.state == "dispatched"

        status_phase = "completed"
        with patch("src.services.agent.tool_session.tool_session", _tracked_scope):
            terminal_replay = await tools_impl.execute_tool(
                "create_draft",
                arguments,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=key.thread_id,
                operation_key=key,
            )

        assert generation_calls == 1
        assert terminal_replay["status"] == "completed"
        assert terminal_replay["task_id"] == task_id
        assert terminal_replay["draft_id"] == draft_id
        assert terminal_replay["project_id"] == project_id
        assert _encoded_result_size(terminal_replay) <= _MAX_TOOL_RESULT_BYTES
        assert (
            terminal_replay["_tool_result_bounds"]["identity_coverage"]["incomplete"]
            is True
        )
        assert recovery_wait_calls == 0
        assert shared_store.reads == 2
        assert shared_store.observed_statuses == ["pending", "completed"]
        async with database.session_factory() as verify:
            operation = await verify.get(AgentToolOperation, key.operation_id)
            assert operation is not None
            assert operation.state == "completed"
            assert operation.result == terminal_replay

        with patch("src.services.agent.tool_session.tool_session", _tracked_scope):
            completed_replay = await tools_impl.execute_tool(
                "create_draft",
                arguments,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=key.thread_id,
                operation_key=key,
            )
        assert completed_replay == terminal_replay
        assert _encoded_result_size(completed_replay) <= _MAX_TOOL_RESULT_BYTES


async def test_postgres_draft_cancellation_preserves_dispatched_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after record_dispatch keeps the task recoverable."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent import tools_impl
    from src.services.research import draft_generation_service

    async with _postgres_tool_schema(dsn) as database:
        project_id = await _create_pg_project(database, "round2-cancel-project")
        arguments = {
            "project_id": project_id,
            "themes": ["preserve identity after cancellation"],
            "style": "summary",
        }
        key = _operation_key(
            database=database,
            tool_name="create_draft",
            arguments=arguments,
            call_id="draft-cancel-after-recorder",
            turn_id="draft-cancel-round2",
        )
        task_id = "cancelled-wait-task-17"
        draft_id = str(uuid.uuid4())
        generation_calls = 0
        wait_calls = 0

        def _init(self: Any, db: AsyncSession) -> None:
            self.db = db

        async def _generate(
            self: Any,
            *,
            project_id: uuid.UUID,
            user_id: uuid.UUID,
            themes: list[str],
            style: str,
        ) -> dict[str, Any]:
            nonlocal generation_calls
            generation_calls += 1
            assert self.db.in_transaction() is False
            assert project_id == uuid.UUID(cast(str, arguments["project_id"]))
            assert user_id == database.user_id
            assert themes == arguments["themes"]
            assert style == arguments["style"]
            return {"task_id": task_id, "status": "pending"}

        async def _wait(
            _cls: type[Any],
            requested_task_id: str,
            *,
            timeout_seconds: float = 120.0,
        ) -> dict[str, Any]:
            nonlocal wait_calls
            wait_calls += 1
            assert requested_task_id == task_id
            assert timeout_seconds == 105.0
            if wait_calls == 1:
                async with database.session_factory() as verify:
                    row = await verify.get(AgentToolOperation, key.operation_id)
                    assert row is not None
                    assert row.state == "dispatched"
                    assert row.result["task_id"] == task_id
            raise asyncio.CancelledError()

        async def _shared_status(
            _cls: type[Any], requested_task_id: str
        ) -> dict[str, Any]:
            assert requested_task_id == task_id
            return {
                "task_id": task_id,
                "project_id": project_id,
                "user_id": str(database.user_id),
                "status": "completed",
                "draft_id": draft_id,
                "current_step": "Draft completed",
            }

        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService, "__init__", _init
        )
        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService,
            "generate_draft",
            _generate,
        )
        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService,
            "wait_for_terminal_status",
            classmethod(_wait),
        )
        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService,
            "get_status_shared",
            classmethod(_shared_status),
        )
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            with pytest.raises(asyncio.CancelledError):
                await tools_impl.execute_tool(
                    "create_draft",
                    arguments,
                    user_id=str(database.user_id),
                    organization_id=str(database.organization_id),
                    thread_id=key.thread_id,
                    operation_key=key,
                )

            async with database.session_factory() as verify:
                row = await verify.get(AgentToolOperation, key.operation_id)
                assert row is not None
                assert row.state == "dispatched"
                assert row.result["task_id"] == task_id
                assert row.result["project_id"] == project_id
                assert row.result["user_id"] == str(database.user_id)

            recovered = await tools_impl.execute_tool(
                "create_draft",
                arguments,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=key.thread_id,
                operation_key=key,
            )

        assert generation_calls == 1
        assert wait_calls == 1
        assert recovered["status"] == "completed"
        assert recovered["task_id"] == task_id
        assert recovered["draft_id"] == draft_id
        assert recovered["project_id"] == project_id
        async with database.session_factory() as verify:
            row = await verify.get(AgentToolOperation, key.operation_id)
            assert row is not None
            assert row.state == "completed"
            assert row.result == recovered


async def test_postgres_draft_status_poll_exception_returns_recoverable_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poll exception after dispatch does not replace the task identity."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent import tools_impl
    from src.services.research import draft_generation_service

    async with _postgres_tool_schema(dsn) as database:
        project_id = await _create_pg_project(database, "round2-poll-error-project")
        arguments = {
            "project_id": project_id,
            "themes": ["recover after ordinary wait error"],
            "style": "summary",
        }
        key = _operation_key(
            database=database,
            tool_name="create_draft",
            arguments=arguments,
            call_id="draft-poll-exception",
            turn_id="draft-poll-exception-turn",
        )
        task_id = "poll-exception-task-19"
        generation_calls = 0

        def _init(self: Any, db: AsyncSession) -> None:
            self.db = db

        async def _generate(
            self: Any,
            *,
            project_id: uuid.UUID,
            user_id: uuid.UUID,
            themes: list[str],
            style: str,
        ) -> dict[str, Any]:
            nonlocal generation_calls
            generation_calls += 1
            return {"task_id": task_id, "status": "pending"}

        async def _wait(
            _cls: type[Any],
            requested_task_id: str,
            *,
            timeout_seconds: float = 120.0,
        ) -> dict[str, Any]:
            assert requested_task_id == task_id
            assert timeout_seconds == 105.0
            async with database.session_factory() as verify:
                row = await verify.get(AgentToolOperation, key.operation_id)
                assert row is not None
                assert row.state == "dispatched"
                assert row.result["task_id"] == task_id
            raise RuntimeError("shared status poll interrupted")

        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService, "__init__", _init
        )
        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService,
            "generate_draft",
            _generate,
        )
        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService,
            "wait_for_terminal_status",
            classmethod(_wait),
        )
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            result = await tools_impl.execute_tool(
                "create_draft",
                arguments,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=key.thread_id,
                operation_key=key,
            )

        assert generation_calls == 1
        assert result["status"] == "pending"
        assert result["task_id"] == task_id
        assert result["project_id"] == project_id
        assert result["error_category"] == "draft_status_unavailable"
        assert result["automatic_retry_allowed"] is False
        async with database.session_factory() as verify:
            row = await verify.get(AgentToolOperation, key.operation_id)
            assert row is not None
            assert row.state == "dispatched"
            assert row.result["task_id"] == task_id


async def test_postgres_mark_unknown_cannot_erase_recorded_dispatch() -> None:
    """The store rejects any attempt to clear an already dispatched identity."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent.tool_operations import mark_unknown

    async with _postgres_tool_schema(dsn) as database:
        arguments = {
            "project_id": str(uuid.uuid4()),
            "themes": ["direct state guard"],
            "style": "summary",
        }
        key = _operation_key(
            database=database,
            tool_name="create_draft",
            arguments=arguments,
            call_id="mark-unknown-dispatched-guard",
            turn_id="mark-unknown-dispatched-turn",
        )
        dispatched = {
            "task_id": "must-survive-mark-unknown",
            "project_id": arguments["project_id"],
            "user_id": str(database.user_id),
            "status": "pending",
        }
        await _seed_dispatched_operation(database, key, dispatched)
        async with database.session_factory() as verify:
            row = await verify.get(AgentToolOperation, key.operation_id)
            assert row is not None
            owner_token = cast(uuid.UUID | None, row.owner_token)
            assert owner_token is not None

        with pytest.raises(RuntimeError, match="lost its claim or fingerprint"):
            async with database.session_factory() as session:
                async with session.begin():
                    await mark_unknown(session, key, owner_token)

        async with database.session_factory() as verify:
            row = await verify.get(AgentToolOperation, key.operation_id)
            assert row is not None
            assert row.state == "dispatched"
            assert row.result == dispatched


async def test_postgres_draft_pre_recorder_cancellation_and_record_failure_stay_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrecorded external dispatches stay non-replayable after failures."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent import tool_operations, tools_impl
    from src.services.research import draft_generation_service

    async with _postgres_tool_schema(dsn) as database:
        project_id = await _create_pg_project(database, "round2-unknown-project")
        arguments = {
            "project_id": project_id,
            "themes": ["unknown before recorder"],
            "style": "summary",
        }
        cancel_key = _operation_key(
            database=database,
            tool_name="create_draft",
            arguments=arguments,
            call_id="draft-before-recorder-cancel",
            turn_id="draft-before-recorder-turn",
        )
        original_dispatch = tools_impl._dispatch_tool

        async def _cancel_before_recorder(*_args: Any, **_kwargs: Any) -> Any:
            raise asyncio.CancelledError()

        monkeypatch.setattr(tools_impl, "_dispatch_tool", _cancel_before_recorder)
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            with pytest.raises(asyncio.CancelledError):
                await tools_impl.execute_tool(
                    "create_draft",
                    arguments,
                    user_id=str(database.user_id),
                    organization_id=str(database.organization_id),
                    thread_id=cancel_key.thread_id,
                    operation_key=cancel_key,
                )
        async with database.session_factory() as verify:
            cancelled = await verify.get(AgentToolOperation, cancel_key.operation_id)
            assert cancelled is not None
            assert cancelled.state == "unknown"
            assert cancelled.result is None

        recording_key = _operation_key(
            database=database,
            tool_name="create_draft",
            arguments=arguments,
            call_id="draft-recorder-store-failure",
            turn_id="draft-recorder-failure-turn",
        )

        def _init(self: Any, db: AsyncSession) -> None:
            self.db = db

        async def _generate(
            self: Any,
            *,
            project_id: uuid.UUID,
            user_id: uuid.UUID,
            themes: list[str],
            style: str,
        ) -> dict[str, Any]:
            return {"task_id": "started-but-not-recorded", "status": "pending"}

        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService, "__init__", _init
        )
        monkeypatch.setattr(
            draft_generation_service.DraftGenerationService,
            "generate_draft",
            _generate,
        )
        monkeypatch.setattr(tools_impl, "_dispatch_tool", original_dispatch)

        async def _fail_record(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("injected dispatch recorder failure")

        monkeypatch.setattr(tool_operations, "record_dispatch", _fail_record)
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            with pytest.raises(Exception, match="recovery identity was not recorded"):
                await tools_impl.execute_tool(
                    "create_draft",
                    arguments,
                    user_id=str(database.user_id),
                    organization_id=str(database.organization_id),
                    thread_id=recording_key.thread_id,
                    operation_key=recording_key,
                )

        async with database.session_factory() as verify:
            failed_record = await verify.get(
                AgentToolOperation, recording_key.operation_id
            )
            assert failed_record is not None
            assert failed_record.state == "unknown"
            assert failed_record.result is None


async def test_postgres_graph_keeps_bounded_failed_observation_on_fresh_and_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graph messages retain the exact protected error observation on replay."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent import _nodes_tools, graph, tools_impl

    async with _postgres_tool_schema(dsn) as database:
        project_id = await _create_pg_project(database, "round2-graph-project")
        arguments = {
            "project_id": project_id,
            "themes": ["preserve graph failure identity"],
            "style": "summary",
        }
        call_id = "graph-draft-partial-result"
        key = _operation_key(
            database=database,
            tool_name="create_draft",
            arguments=arguments,
            call_id=call_id,
            turn_id="graph-round2-turn",
            thread_id="graph-round2-thread",
        )
        dispatch_calls = 0
        observation: dict[str, Any] = {
            "status": "failed",
            "error": "Draft persisted but some sources failed.",
            "error_category": "draft_generation_partial_failure",
            "automatic_retry_allowed": False,
            "task_id": "graph-recoverable-task-22",
            "project_id": project_id,
            "user_id": str(database.user_id),
            "draft_id": str(uuid.uuid4()),
            "documents": [
                {
                    "id": str(uuid.uuid4()),
                    "title": f"Retained source {index}",
                    "status": "partial",
                    "project_id": project_id,
                }
                for index in range(80)
            ],
            "detail": "部分的な結果 📚 " * 9000,
        }

        async def _dispatch(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal dispatch_calls
            dispatch_calls += 1
            return observation

        monkeypatch.setattr(tools_impl, "_dispatch_tool", _dispatch)
        monkeypatch.setattr(graph, "_get_execute_tool", lambda: tools_impl.execute_tool)
        config = cast(
            RunnableConfig,
            {
                "configurable": {
                    "user_id": str(database.user_id),
                    "organization_id": str(database.organization_id),
                    "thread_id": key.thread_id,
                }
            },
        )
        operation_context = {
            "tool_operation_protocol_version": 1,
            "tool_operation_turn_id": key.turn_id,
        }
        tool_call = {"name": "create_draft", "id": call_id, "args": arguments}

        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            fresh = await _nodes_tools._execute_single_tool(
                tool_call, config, {}, operation_context
            )
            replay = await _nodes_tools._execute_single_tool(
                tool_call, config, {}, operation_context
            )

        async with database.session_factory() as verify:
            row = await verify.get(AgentToolOperation, key.operation_id)
            assert row is not None
            assert row.state == "completed"
            assert row.result is not None
            saved_result = row.result

        assert dispatch_calls == 1
        assert fresh["execution"]["status"] == "failed"
        assert fresh["message"].status == "error"
        assert fresh["error_info"]["category"] != ""
        assert fresh["execution"]["result"] == saved_result
        assert json.loads(fresh["message"].content) == saved_result
        assert replay["execution"]["status"] == "failed"
        assert replay["execution"]["result"] == saved_result
        assert json.loads(replay["message"].content) == saved_result
        assert saved_result["status"] == "failed"
        assert saved_result["task_id"] == "graph-recoverable-task-22"
        assert saved_result["project_id"] == project_id
        assert saved_result["draft_id"] == observation["draft_id"]
        assert _encoded_result_size(saved_result) <= _MAX_TOOL_RESULT_BYTES
        assert (
            saved_result["_tool_result_bounds"]["identity_coverage"]["incomplete"]
            is True
        )


async def test_postgres_draft_recovery_store_failure_is_uncertain_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed recovered-result write reports uncertainty and retains dispatch."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent import tool_operations, tools_impl

    async with _postgres_tool_schema(dsn) as database:
        project_id = await _create_pg_project(database, "round2-store-failure-project")
        arguments = {
            "project_id": project_id,
            "themes": ["recover safely after storage error"],
            "style": "summary",
        }
        key = _operation_key(
            database=database,
            tool_name="create_draft",
            arguments=arguments,
            call_id="draft-recovery-store-failure",
            turn_id="draft-recovery-store-failure-turn",
        )
        task_id = "saved-dispatch-task-77"
        dispatched = {
            "task_id": task_id,
            "project_id": project_id,
            "project_name": "Storage recovery project",
            "user_id": str(database.user_id),
            "status": "pending",
            "message": "Draft generation started",
        }
        await _seed_dispatched_operation(database, key, dispatched)
        terminal = {
            **dispatched,
            "status": "completed",
            "draft_id": str(uuid.uuid4()),
            "_terminal_status": True,
        }
        original_complete = tool_operations.complete_operation
        monkeypatch.setattr(
            tools_impl, "_recover_draft_status", AsyncMock(return_value=terminal)
        )

        async def _fail_complete(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("injected recovered result storage failure")

        monkeypatch.setattr(tool_operations, "complete_operation", _fail_complete)
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            uncertain = await tools_impl.execute_tool(
                "create_draft",
                arguments,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=key.thread_id,
                operation_key=key,
            )

        assert uncertain["error_category"] == "operation_result_uncertain"
        assert uncertain["automatic_retry_allowed"] is False
        assert uncertain["task_id"] == task_id
        assert uncertain["project_id"] == project_id
        assert uncertain["draft_id"] == terminal["draft_id"]
        assert _encoded_result_size(uncertain) <= _MAX_TOOL_RESULT_BYTES
        async with database.session_factory() as verify:
            row = await verify.get(AgentToolOperation, key.operation_id)
            assert row is not None
            assert row.state == "dispatched"
            assert row.result == dispatched

        monkeypatch.setattr(tool_operations, "complete_operation", original_complete)
        monkeypatch.setattr(
            tools_impl, "_recover_draft_status", AsyncMock(return_value=terminal)
        )
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            recovered = await tools_impl.execute_tool(
                "create_draft",
                arguments,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=key.thread_id,
                operation_key=key,
            )

        expected = dict(terminal)
        expected.pop("_terminal_status")
        assert recovered == expected
        async with database.session_factory() as verify:
            row = await verify.get(AgentToolOperation, key.operation_id)
            assert row is not None
            assert row.state == "completed"
            assert row.result == recovered


async def test_postgres_concurrent_draft_recovery_returns_committed_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completion CAS loser returns the exact committed database winner."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent import tools_impl

    async with _postgres_tool_schema(dsn) as database:
        project_id = await _create_pg_project(database, "round2-race-project")
        arguments = {
            "project_id": project_id,
            "themes": ["concurrent status recovery"],
            "style": "summary",
        }
        key = _operation_key(
            database=database,
            tool_name="create_draft",
            arguments=arguments,
            call_id="draft-recovery-concurrent-cas",
            turn_id="draft-recovery-concurrent-turn",
        )
        task_id = "concurrent-shared-task-81"
        await _seed_dispatched_operation(
            database,
            key,
            {
                "task_id": task_id,
                "project_id": project_id,
                "project_name": "Concurrent recovery project",
                "user_id": str(database.user_id),
                "status": "pending",
            },
        )
        both_recovered = asyncio.Event()
        recovery_calls = 0

        async def _terminal_recovery(
            _dispatched: dict[str, Any],
        ) -> dict[str, Any]:
            nonlocal recovery_calls
            recovery_calls += 1
            current = recovery_calls
            if recovery_calls == 2:
                both_recovered.set()
            await both_recovered.wait()
            return {
                "task_id": task_id,
                "project_id": project_id,
                "user_id": str(database.user_id),
                "status": "completed",
                "draft_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"winner-{current}")),
                "recovery_observation": f"candidate-{current}",
                "_terminal_status": True,
            }

        monkeypatch.setattr(tools_impl, "_recover_draft_status", _terminal_recovery)
        scope = _tool_session_scope(database.session_factory)
        with patch("src.services.agent.tool_session.tool_session", scope):
            first, second = await asyncio.gather(
                tools_impl.execute_tool(
                    "create_draft",
                    arguments,
                    user_id=str(database.user_id),
                    organization_id=str(database.organization_id),
                    thread_id=key.thread_id,
                    operation_key=key,
                ),
                tools_impl.execute_tool(
                    "create_draft",
                    arguments,
                    user_id=str(database.user_id),
                    organization_id=str(database.organization_id),
                    thread_id=key.thread_id,
                    operation_key=key,
                ),
            )

        async with database.session_factory() as verify:
            row = await verify.get(AgentToolOperation, key.operation_id)
            assert row is not None
            assert row.state == "completed"
            assert row.result is not None
            winner = row.result
        assert recovery_calls == 2
        assert first == winner
        assert second == winner
        assert winner["status"] == "completed"
        assert winner["task_id"] == task_id


async def test_postgres_uncertain_external_effect_blocks_new_call_id_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new provider call ID cannot bypass an uncertain same-turn operation."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent import tools_impl

    async with _postgres_tool_schema(dsn) as database:
        arguments = {
            "code": "print('offline test')",
            "description": "Exercise uncertain execution recovery",
            "language": "python",
            "packages": None,
        }
        first_key = _operation_key(
            database=database,
            tool_name="execute_code",
            arguments=arguments,
            call_id="uncertain-call-one",
            turn_id="uncertain-turn-1",
        )
        second_key = _operation_key(
            database=database,
            tool_name="execute_code",
            arguments=arguments,
            call_id="uncertain-call-two",
            turn_id="uncertain-turn-1",
        )
        dispatch_count = 0

        async def _uncertain_dispatch(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal dispatch_count
            dispatch_count += 1
            if dispatch_count == 1:
                raise RuntimeError("simulated timeout after provider acceptance")
            return {"status": "success", "provider_job_id": "job-second"}

        monkeypatch.setattr(tools_impl, "_dispatch_tool", _uncertain_dispatch)
        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            with pytest.raises(RuntimeError, match="simulated timeout"):
                await tools_impl.execute_tool(
                    "execute_code",
                    arguments,
                    user_id=str(database.user_id),
                    organization_id=str(database.organization_id),
                    thread_id=first_key.thread_id,
                    operation_key=first_key,
                )

            blocked = await tools_impl.execute_tool(
                "execute_code",
                arguments,
                user_id=str(database.user_id),
                organization_id=str(database.organization_id),
                thread_id=second_key.thread_id,
                operation_key=second_key,
            )

        assert dispatch_count == 1
        assert blocked["error_category"] == "operation_outcome_unknown"
        assert blocked["automatic_retry_allowed"] is False
        async with database.session_factory() as verify:
            operations = list(await verify.scalars(select(AgentToolOperation)))
            assert len(operations) == 1
            assert operations[0].state == "unknown"


async def test_postgres_errored_external_replay_is_marked_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8-B5: a new call id replaying a same-turn failure is marked and counted."""
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    from src.services.agent import _nodes_tools, graph, tools_impl

    async with _postgres_tool_schema(dsn) as database:
        dispatch_calls = 0

        async def _timeout(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal dispatch_calls
            dispatch_calls += 1
            return {"error": "arXiv request timed out"}

        monkeypatch.setattr(tools_impl, "_dispatch_tool", _timeout)
        monkeypatch.setattr(graph, "_get_execute_tool", lambda: tools_impl.execute_tool)
        config = cast(
            RunnableConfig,
            {
                "configurable": {
                    "user_id": str(database.user_id),
                    "organization_id": str(database.organization_id),
                    "thread_id": "b5-thread",
                }
            },
        )
        operation_context = {
            "tool_operation_protocol_version": 1,
            "tool_operation_turn_id": "b5-turn",
        }
        args = {"paper_ids": ["2401.12345"]}

        with patch(
            "src.services.agent.tool_session.tool_session",
            _tool_session_scope(database.session_factory),
        ):
            fresh = await _nodes_tools._execute_single_tool(
                {"name": "ingest_arxiv_papers", "id": "b5-call-1", "args": args},
                config,
                {},
                operation_context,
            )
            replay = await _nodes_tools._execute_single_tool(
                {"name": "ingest_arxiv_papers", "id": "b5-call-2", "args": args},
                config,
                {},
                operation_context,
            )

        first_key = _operation_key(
            database=database,
            tool_name="ingest_arxiv_papers",
            arguments=args,
            call_id="b5-call-1",
            turn_id="b5-turn",
            thread_id="b5-thread",
        )
        assert dispatch_calls == 1
        assert fresh["error_increment"] == 0  # transient: free the first time
        assert "replayed_from_operation" not in fresh["execution"]["result"]
        assert replay["execution"]["status"] == "failed"
        assert replay["error_increment"] == 1
        replayed = replay["execution"]["result"]
        assert replayed["error"] == "arXiv request timed out"
        assert replayed["replayed_from_operation"] == first_key.operation_id
        assert replayed["automatic_retry_allowed"] is False
