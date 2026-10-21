"""Real PostgreSQL evidence for native completion and the aw03 migration.

Requires ORCHESTRATION_TEST_DATABASE_URL; otherwise skipped (NOT RUN). Every
test owns a random schema and drops only that schema, including on failures.
The real aw03 revision runs against model-built prerequisite tables.

Mutation checks (docs/engineering/testing.md), with this file as TEST_FILE:
  ORCHESTRATION_TEST_DATABASE_URL=postgresql+asyncpg:///meridian_test \
    /tmp/nous-qa-fix-venv/bin/python -m pytest -q TEST_FILE::TEST_NAME
Remove enqueue_native_artifact's draft SELECT FOR UPDATE in
services/artifacts/native.py: test_concurrent_enqueue_has_one_intent fails
with the native draft unique constraint violation. Remove _eligible(now) from
the conditional claim UPDATE: test_concurrent_drains_publish_once fails with
"duplicate worker claim". Remove claim_token == token from the acknowledgement
UPDATE: test_expired_claim_cannot_acknowledge_new_worker fails with "expired
claimant overwrote replacement worker state". Remove expected_token from the
publication guard: the same test fails with "expired claimant published a
version under a replacement claim". Remove the exhausted-intent skip_locked
lock: test_final_attempt_publication_is_not_reconciled_while_locked fails with
"reconciler read an uncommitted receipt". Restore each guard before rerunning.
Remove the post-claim reload status/token check:
test_superseded_claim_cannot_adopt_reloaded_lease fails with "superseded
claimant adopted a replacement lease before publishing".
Exact focused commands and guard file:line references are recorded in
docs/testing/artifact-workspace-completion-2026-10-11.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, func, inspect, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.config import settings
from src.models.agent_run import AgentRun
from src.models.agent_runtime_snapshot import AgentRuntimeSnapshot
from src.models.artifact import (
    Artifact,
    ArtifactLifecycleOutbox,
    ArtifactReference,
    ArtifactUpload,
    ArtifactVersion,
)
from src.models.artifact_share import ArtifactShare, ArtifactShareAudience
from src.models.base import Base
from src.models.chat_message import ChatMessage
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.document import Document
from src.models.generated_draft import GeneratedDraft
from src.models.native_artifact_intent import NativeArtifactIntent
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.integration_context import IntegrationContext
from src.services.artifacts import native, service
from src.services.artifacts.storage import MemoryArtifactStorage

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

_PARENTS: list[Any] = [
    Organization,
    User,
    Document,
    Workspace,
    WorkspaceMember,
    Collection,
    Conversation,
    Thread,
    ChatMessage,
    AgentRun,
    AgentRuntimeSnapshot,
    Artifact,
    ArtifactVersion,
    ArtifactUpload,
    ArtifactReference,
    ArtifactLifecycleOutbox,
    GeneratedDraft,
]
_ADDED: list[Any] = [ArtifactShare, ArtifactShareAudience, NativeArtifactIntent]
CONTENT = "# Saved review\n"


def _migrate(connection: Connection, direction: str) -> None:
    path = Path(__file__).parents[2] / "alembic/versions/aw03_artifact_completion.py"
    spec = importlib.util.spec_from_file_location("artifact_completion_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "ic02_selected_skill_snapshot"
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, direction)()


@dataclass
class CompletionDatabase:
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    schema: str
    context: IntegrationContext
    draft_id: UUID
    storage: MemoryArtifactStorage


@pytest.fixture
async def completion_db(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[CompletionDatabase]:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("Artifact completion PostgreSQL test database is not configured")
        return
    if dsn.startswith("postgresql://"):
        dsn = "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    schema = "artifact_completion_" + uuid4().hex
    admin = create_async_engine(dsn)
    engine = create_async_engine(
        dsn, connect_args={"server_settings": {"search_path": schema}}
    )
    created = False
    try:
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            created = True
        async with engine.begin() as connection:
            await connection.run_sync(
                Base.metadata.create_all, tables=[model.__table__ for model in _PARENTS]
            )
            await connection.run_sync(_migrate, "upgrade")
        user, org, workspace, project, draft_id = (uuid4() for _ in range(5))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            await db.execute(
                text("""INSERT INTO organizations (id, created_at, updated_at,
                    is_deleted, name, storage_tier, storage_used_bytes,
                    storage_limit_bytes, is_active) VALUES (:id, now(), now(),
                    false, 'org', 'FREE', 0, 1000000, true)"""),
                {"id": org},
            )
            await db.execute(
                text("""INSERT INTO users (id, created_at, updated_at, is_deleted,
                    email, password_hash, first_name, last_name, role, is_active,
                    organization_id, login_count) VALUES (:id, now(), now(), false,
                    :email, 'unused', 'x', 'y', 'USER', true, :org, 0)"""),
                {"id": user, "email": f"{user}@example.test", "org": org},
            )
            for row in (
                Workspace(id=workspace, name="w", owner_id=user, organization_id=org),
                Collection(id=project, workspace_id=workspace, name="p"),
                GeneratedDraft(
                    id=draft_id,
                    project_id=project,
                    version=1,
                    title="Saved review",
                    content=CONTENT,
                    themes=[],
                ),
            ):
                db.add(row)
                await db.flush()
            await db.commit()
        storage = MemoryArtifactStorage()
        monkeypatch.setattr(settings, "ARTIFACTS_ENABLED", True)
        monkeypatch.setattr(service, "get_artifact_storage", lambda: storage)
        yield CompletionDatabase(
            engine,
            factory,
            schema,
            native.create_native_context(
                user_id=user, organization_id=org, project_id=project
            ),
            draft_id,
            storage,
        )
    finally:
        await engine.dispose()
        try:
            if created:
                async with admin.begin() as connection:
                    await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await admin.dispose()


async def _enqueue(database: CompletionDatabase) -> None:
    async with database.factory() as db:
        await native.enqueue_native_artifact(
            db, draft_id=database.draft_id, context=database.context
        )
        await db.commit()


async def _wait_for_lock(database: CompletionDatabase, application_name: str) -> None:
    async with asyncio.timeout(10):
        async with database.factory() as db:
            while not await db.scalar(
                text("""SELECT EXISTS (SELECT 1 FROM pg_stat_activity
                    WHERE application_name = :name AND wait_event_type = 'Lock')"""),
                {"name": application_name},
            ):
                await db.rollback()  # Refresh PostgreSQL's activity snapshot.
                await asyncio.sleep(0.01)


async def test_concurrent_enqueue_has_one_intent(
    completion_db: CompletionDatabase,
) -> None:
    first_ready, release = asyncio.Event(), asyncio.Event()
    name = "enqueue_" + completion_db.schema

    async def first() -> None:
        async with completion_db.factory() as db:
            await native.enqueue_native_artifact(
                db, draft_id=completion_db.draft_id, context=completion_db.context
            )
            first_ready.set()
            await release.wait()
            await db.commit()

    async def second() -> None:
        async with completion_db.factory() as db:
            await db.execute(
                text("SELECT set_config('application_name', :name, true)"),
                {"name": name},
            )
            await native.enqueue_native_artifact(
                db, draft_id=completion_db.draft_id, context=completion_db.context
            )
            await db.commit()

    tasks = [asyncio.create_task(first())]
    try:
        await asyncio.wait_for(first_ready.wait(), 10)
        tasks.append(asyncio.create_task(second()))
        # The first draft transaction stays open until PostgreSQL confirms
        # that the competing transaction is waiting on its real lock.
        await _wait_for_lock(completion_db, name)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        async with completion_db.factory() as db:
            assert (
                await db.scalar(select(func.count()).select_from(NativeArtifactIntent))
                == 1
            )
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_concurrent_drains_publish_once(
    completion_db: CompletionDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enqueue(completion_db)
    readers, ready = 0, asyncio.Event()

    async def worker() -> int:
        async with completion_db.factory() as db:
            original = db.scalars

            async def synchronized_read(*args: Any, **kwargs: Any) -> Any:
                nonlocal readers
                result = await original(*args, **kwargs)
                selected = args[0].column_descriptions[0]
                if (
                    selected["entity"] is NativeArtifactIntent
                    and selected["name"] == "id"
                    and "native_artifact_intents.attempts <" in str(args[0])
                ):
                    readers += 1
                    if readers == 2:
                        ready.set()
                    await asyncio.wait_for(ready.wait(), 10)
                return result

            # Change scheduling only: both real candidate SELECTs finish
            # before either worker issues its conditional claim UPDATE.
            monkeypatch.setattr(db, "scalars", synchronized_read)
            return await native.drain_native_artifacts(db)

    delivered = await asyncio.wait_for(asyncio.gather(worker(), worker()), 15)
    async with completion_db.factory() as db:
        row = (await db.scalars(select(NativeArtifactIntent))).one()
        assert row.attempts == 1, "duplicate worker claim incremented attempts"
        assert sum(delivered) == 1, "duplicate workers acknowledged one publication"
        assert row.status == "delivered" and row.version_id is not None
        assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 1
        assert await db.scalar(select(func.count()).select_from(ArtifactReference)) == 1
        assert (
            await db.scalar(select(func.count()).select_from(ArtifactLifecycleOutbox))
            == 1
        )


async def test_expired_claim_cannot_acknowledge_new_worker(
    completion_db: CompletionDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enqueue(completion_db)
    started = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    put = completion_db.storage.put
    calls = 0

    async def delayed_put(key: str, content: bytes, mime_type: str) -> None:
        nonlocal calls
        index = calls
        calls += 1
        assert index < 2
        started[index].set()
        await release[index].wait()
        await put(key, content, mime_type)

    async def worker() -> int:
        async with completion_db.factory() as db:
            return await native.drain_native_artifacts(db)

    monkeypatch.setattr(completion_db.storage, "put", delayed_put)
    tasks = [asyncio.create_task(worker())]
    try:
        await asyncio.wait_for(started[0].wait(), 10)
        async with completion_db.factory() as db:
            await db.execute(
                update(NativeArtifactIntent).values(
                    claimed_at=datetime.now(timezone.utc) - timedelta(minutes=6)
                )
            )
            await db.commit()
        tasks.append(asyncio.create_task(worker()))
        await asyncio.wait_for(started[1].wait(), 10)
        async with completion_db.factory() as db:
            replacement = (await db.scalars(select(NativeArtifactIntent))).one()
            replacement_token = replacement.claim_token
            assert replacement.attempts == 2
        release[0].set()
        assert (
            await asyncio.wait_for(tasks[0], 10) == 0
        ), "expired claimant acknowledged newer claim"
        async with completion_db.factory() as db:
            row = (await db.scalars(select(NativeArtifactIntent))).one()
            assert (row.status, row.claim_token, row.version_id) == (
                "processing",
                replacement_token,
                None,
            ), "expired claimant overwrote replacement worker state"
            assert (
                await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 0
            ), "expired claimant published a version under a replacement claim"
        release[1].set()
        assert await asyncio.wait_for(tasks[1], 10) == 1
        async with completion_db.factory() as db:
            row = (await db.scalars(select(NativeArtifactIntent))).one()
            assert row.status == "delivered" and row.claim_token == replacement_token
            assert (
                await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 1
            )
    finally:
        for event in release:
            event.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_final_attempt_publication_is_not_reconciled_while_locked(
    completion_db: CompletionDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enqueue(completion_db)
    async with completion_db.factory() as db:
        await db.execute(update(NativeArtifactIntent).values(attempts=4))
        await db.commit()
    version_ready, release, receipt_read = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    put = completion_db.storage.put

    async def stale_put(key: str, content: bytes, mime_type: str) -> None:
        await put(key, content, mime_type)
        # Expire the fifth claim before its publication guard takes the lock.
        async with completion_db.factory() as db:
            await db.execute(
                update(NativeArtifactIntent).values(
                    claimed_at=datetime.now(timezone.utc) - timedelta(minutes=6)
                )
            )
            await db.commit()

    async def publish() -> int:
        async with completion_db.factory() as db:
            flush = db.flush

            async def held_version_flush(*args: Any, **kwargs: Any) -> None:
                if any(isinstance(row, ArtifactVersion) for row in db.new):
                    version_ready.set()
                    await release.wait()
                await flush(*args, **kwargs)

            monkeypatch.setattr(db, "flush", held_version_flush)
            return await native.drain_native_artifacts(db)

    async def reconcile() -> int:
        async with completion_db.factory() as db:
            scalar = db.scalar

            async def observed_receipt(*args: Any, **kwargs: Any) -> Any:
                selected = args[0].column_descriptions[0]
                if (
                    selected["entity"] is ArtifactUpload
                    and selected["name"] == "version_id"
                ):
                    receipt_read.set()
                return await scalar(*args, **kwargs)

            monkeypatch.setattr(db, "scalar", observed_receipt)
            return await native.drain_native_artifacts(db)

    monkeypatch.setattr(completion_db.storage, "put", stale_put)
    tasks: list[asyncio.Task[Any]] = [asyncio.create_task(publish())]
    try:
        await asyncio.wait_for(version_ready.wait(), 10)
        tasks.append(asyncio.create_task(reconcile()))
        receipt_wait = asyncio.create_task(receipt_read.wait())
        tasks.append(receipt_wait)
        done, _ = await asyncio.wait(
            [tasks[1], receipt_wait], timeout=10, return_when=asyncio.FIRST_COMPLETED
        )
        assert done, "reconciler stalled on final publication's held intent lock"
        assert (
            not receipt_read.is_set()
        ), "reconciler read an uncommitted receipt for a locked final publication"
        assert await tasks[1] == 0
        release.set()
        assert await asyncio.wait_for(tasks[0], 10) == 1
        async with completion_db.factory() as db:
            row = (await db.scalars(select(NativeArtifactIntent))).one()
            assert row.status == "delivered" and row.version_id is not None
            assert row.attempts == 5
            assert (
                await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 1
            )
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_superseded_claim_cannot_adopt_reloaded_lease(
    completion_db: CompletionDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enqueue(completion_db)
    before_reload, reload_release, new_upload, upload_release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    put = completion_db.storage.put
    calls = 0

    async def held_put(key: str, content: bytes, mime_type: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            new_upload.set()
            await upload_release.wait()
        await put(key, content, mime_type)

    async def old_worker() -> int:
        async with completion_db.factory() as db:
            get = db.get

            async def delayed_reload(*args: Any, **kwargs: Any) -> Any:
                if args[0] is NativeArtifactIntent:
                    before_reload.set()
                    await reload_release.wait()
                return await get(*args, **kwargs)

            monkeypatch.setattr(db, "get", delayed_reload)
            return await native.drain_native_artifacts(db)

    async def new_worker() -> int:
        async with completion_db.factory() as db:
            return await native.drain_native_artifacts(db)

    monkeypatch.setattr(completion_db.storage, "put", held_put)
    tasks = [asyncio.create_task(old_worker())]
    try:
        await asyncio.wait_for(before_reload.wait(), 10)
        async with completion_db.factory() as db:
            await db.execute(
                update(NativeArtifactIntent).values(
                    claimed_at=datetime.now(timezone.utc) - timedelta(minutes=6)
                )
            )
            await db.commit()
        tasks.append(asyncio.create_task(new_worker()))
        await asyncio.wait_for(new_upload.wait(), 10)
        reload_release.set()
        assert await asyncio.wait_for(tasks[0], 10) == 0
        async with completion_db.factory() as db:
            assert (
                await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 0
            ), "superseded claimant adopted a replacement lease before publishing"
        upload_release.set()
        assert await asyncio.wait_for(tasks[1], 10) == 1
        async with completion_db.factory() as db:
            row = (await db.scalars(select(NativeArtifactIntent))).one()
            assert row.status == "delivered" and row.attempts == 2
            assert (
                await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 1
            )
    finally:
        reload_release.set()
        upload_release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _assert_migration_contract(connection: Connection) -> None:
    inspector = inspect(connection)
    for model in _ADDED:
        table = model.__table__
        columns = {
            column["name"]: column for column in inspector.get_columns(table.name)
        }
        assert set(columns) == set(table.columns.keys())
        for column in table.columns:
            assert columns[column.name]["nullable"] == column.nullable
            if hasattr(column.type, "timezone"):
                assert getattr(columns[column.name]["type"], "timezone") == getattr(
                    column.type, "timezone"
                )
        assert {
            index["name"]
            for index in inspector.get_indexes(table.name)
            if not index.get("duplicates_constraint")
        } == {index.name for index in table.indexes}
        actual_fks = {
            (
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                fk["options"].get("ondelete"),
            )
            for fk in inspector.get_foreign_keys(table.name)
        }
        expected_fks = {
            ((fk.parent.name,), fk.column.table.name, fk.ondelete)
            for fk in table.foreign_keys
        }
        assert actual_fks == expected_fks
    assert {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("artifact_share_audiences")
    } == {("share_id", "user_id")}
    assert {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("native_artifact_intents")
    } == {("draft_id",)}


async def _assert_database_constraints(database: CompletionDatabase) -> None:
    await _enqueue(database)
    async with database.factory() as db:
        assert await native.drain_native_artifacts(db) == 1
        version = await db.scalar(select(ArtifactVersion.id))
        share_id = uuid4()
        db.add(
            ArtifactShare(
                id=share_id,
                organization_id=database.context.organization_id,
                creator_id=database.context.user_id,
                version_id=version,
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
        )
        await db.flush()
        db.add(
            ArtifactShareAudience(share_id=share_id, user_id=database.context.user_id)
        )
        await db.flush()
        with pytest.raises(IntegrityError, match="uq_artifact_share_audience_user"):
            async with db.begin_nested():
                db.add(
                    ArtifactShareAudience(
                        share_id=share_id, user_id=database.context.user_id
                    )
                )
                await db.flush()
        with pytest.raises(
            IntegrityError, match="native_artifact_intents_draft_id_key"
        ):
            async with db.begin_nested():
                db.add(
                    NativeArtifactIntent(
                        draft_id=database.draft_id,
                        organization_id=database.context.organization_id,
                        actor_user_id=database.context.user_id,
                        project_id=database.context.project_id,
                        draft_sha256=hashlib.sha256(CONTENT.encode()).hexdigest(),
                        title="Saved review",
                    )
                )
                await db.flush()
        await db.execute(delete(ArtifactShare).where(ArtifactShare.id == share_id))
        assert (
            await db.scalar(select(func.count()).select_from(ArtifactShareAudience))
            == 0
        )
        await db.commit()


async def test_aw03_upgrade_downgrade_reupgrade(
    completion_db: CompletionDatabase,
) -> None:
    for iteration in range(2):
        async with completion_db.engine.begin() as connection:
            await connection.run_sync(_assert_migration_contract)
            protected = (
                await connection.execute(
                    text("""SELECT c.relname, c.relrowsecurity FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = :schema AND c.relname = ANY(:tables)"""),
                    {
                        "schema": completion_db.schema,
                        "tables": [model.__tablename__ for model in _ADDED],
                    },
                )
            ).all()
            assert len(protected) == 3 and all(enabled for _, enabled in protected)
            roles: list[str] = list(
                (
                    (
                        await connection.execute(
                            text(
                                "SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            )
            for role in roles:
                for model in _ADDED:
                    assert not await connection.scalar(
                        text(
                            "SELECT has_table_privilege(:role, :table, 'SELECT,INSERT,UPDATE,DELETE')"
                        ),
                        {
                            "role": role,
                            "table": f"{completion_db.schema}.{model.__tablename__}",
                        },
                    )
        await _assert_database_constraints(completion_db)
        if iteration == 0:
            async with completion_db.engine.begin() as connection:
                await connection.run_sync(_migrate, "downgrade")
                tables = await connection.run_sync(
                    lambda sync: inspect(sync).get_table_names()
                )
                assert not {model.__tablename__ for model in _ADDED}.intersection(
                    tables
                )
                assert "generated_drafts" in tables and "artifact_versions" in tables
                await connection.run_sync(_migrate, "upgrade")


async def test_native_intent_foreign_keys_allow_parent_purge(
    completion_db: CompletionDatabase,
) -> None:
    project_id = completion_db.context.project_id
    assert project_id is not None
    async with completion_db.factory() as db:
        workspace_id = await db.scalar(
            select(Collection.workspace_id).where(
                cast(Any, Collection.id) == completion_db.context.project_id
            )
        )
        conversation_id, thread_id, run_id, extra_project = (uuid4() for _ in range(4))
        for row in (
            Conversation(
                id=conversation_id,
                workspace_id=workspace_id,
                title="purge proof",
                created_by_id=completion_db.context.user_id,
            ),
            Thread(id=thread_id, conversation_id=conversation_id, title="purge proof"),
            AgentRun(
                job_id=str(run_id),
                status="running",
                organization_id=completion_db.context.organization_id,
                user_id=completion_db.context.user_id,
            ),
            Collection(
                id=extra_project, workspace_id=workspace_id, name="purge project"
            ),
        ):
            db.add(row)
            await db.flush()
        cases: list[tuple[Any, Any, UUID, dict[str, Any]]] = [
            (
                Thread,
                Thread.id == thread_id,
                project_id,
                {"thread_id": thread_id},
            ),
            (
                AgentRun,
                AgentRun.job_id == str(run_id),
                project_id,
                {"run_id": str(run_id)},
            ),
            (Collection, Collection.id == extra_project, extra_project, {}),
        ]
        for index, (parent, predicate, project_id, binding) in enumerate(cases):
            draft_id, intent_id = uuid4(), uuid4()
            db.add(
                GeneratedDraft(
                    id=draft_id,
                    project_id=project_id,
                    version=index + 2,
                    title="purge proof",
                    content=CONTENT,
                    themes=[],
                )
            )
            await db.flush()
            db.add(
                NativeArtifactIntent(
                    id=intent_id,
                    draft_id=draft_id,
                    project_id=project_id,
                    organization_id=completion_db.context.organization_id,
                    actor_user_id=completion_db.context.user_id,
                    draft_sha256=hashlib.sha256(CONTENT.encode()).hexdigest(),
                    title="purge proof",
                    **binding,
                )
            )
            await db.flush()
            # Core DELETE matches retention purges, without ORM cascades.
            await db.execute(delete(parent).where(predicate))
            assert (
                await db.scalar(
                    select(NativeArtifactIntent.id).where(
                        NativeArtifactIntent.id == intent_id
                    )
                )
                is None
            ), f"native intent prevented {parent.__tablename__} purge"
        await db.commit()
