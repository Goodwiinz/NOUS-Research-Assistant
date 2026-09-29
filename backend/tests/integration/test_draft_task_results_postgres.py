"""GOO-297: real PostgreSQL proof that a draft task's terminal result survives
cache loss and process death, and names exactly the draft that task produced.

Fixture pattern: ``test_research_decision_ledger.py`` (per-test schema,
``create_all``, then the real migration for the table under test). Redis is a
dict; clearing it is "expiry". ``SystemExit`` stands in for SIGKILL: the
service's ``except Exception`` does not catch it, so the coroutine dies at
exactly the patched point.

Run: RESEARCH_DECISION_DATABASE_URL=postgresql://$(whoami)@localhost:5432/goo295_test \\
     pytest -q backend/tests/integration/test_draft_task_results_postgres.py
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, AsyncIterator, Callable, Iterator, cast
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.api.research.drafts import router as drafts_router
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models import Base
from src.models.collection import CollectionDocument
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.draft_task_result import DraftTaskResult
from src.models.generated_draft import GeneratedDraft
from src.services.research.draft_generation_service import (
    DraftGenerationService,
    _generation_status,
    finish_task,
    reconcile_task,
    start_task,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_postgres,
    pytest.mark.asyncio,
]

VERSIONS = Path(__file__).parents[2] / "alembic" / "versions"
_MODULE = "src.services.research.draft_generation_service"


def _load_migration() -> ModuleType:
    path = VERSIONS / "c9d1e2f3a4b5_create_draft_task_results.py"
    spec = importlib.util.spec_from_file_location("draft_task_results_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade(connection: Connection) -> None:
    module = _load_migration()
    setattr(module, "op", Operations(MigrationContext.configure(connection)))
    cast(Callable[[], None], module.upgrade)()


def _sha(content: Any) -> str:  # ORM attributes type as Column[str]
    return hashlib.sha256(content.encode()).hexdigest()


def _ensure_fixture_encryption() -> Any:
    # Copied from test_draft_source_scope_postgres.py: user names are
    # encrypted columns and the route's access check loads users.
    from src.core import encryption

    try:
        return encryption.get_field_encryption()
    except encryption.EncryptionError:
        manager = object.__new__(encryption.KeyManager)
        manager.master_key_env_var = "GOO_297_TEST_ONLY"
        manager._keys = {}
        manager._master_key = b"\x00" * 32
        manager.generate_key(encryption.EncryptionKeyType.DATA)
        aes = encryption.AESEncryption(manager)
        field_encryption = encryption.FieldEncryption(aes)
        encryption._key_manager = manager
        encryption._aes_encryption = aes
        encryption._field_encryption = field_encryption
        return field_encryption


@pytest.fixture
async def decision_engine(
    request: pytest.FixtureRequest,
) -> AsyncIterator[AsyncEngine]:
    configured = os.getenv("RESEARCH_DECISION_DATABASE_URL")
    url = make_url(
        configured or str(request.getfixturevalue("postgres_container")["url"])
    ).set(drivername="postgresql+asyncpg")
    schema = f"test_draft_task_results_{uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql("DROP TABLE draft_task_results")
        await connection.run_sync(_upgrade)
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


@pytest.fixture
def factory(decision_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(decision_engine, expire_on_commit=False)


async def _seed_org_user(
    connection: Any, org: UUID, user: UUID, field_encryption: Any
) -> None:
    await connection.execute(
        text("""INSERT INTO organizations
            (id,name,storage_tier,storage_used_bytes,storage_limit_bytes,
             is_active,created_at,updated_at,is_deleted)
            VALUES (:org,:name,'FREE',0,1,true,now(),now(),false)"""),
        {"org": org, "name": f"drafts-{org}"},
    )
    await connection.execute(
        text("""INSERT INTO users
            (id,email,password_hash,first_name,last_name,role,is_active,
             login_count,organization_id,created_at,updated_at,is_deleted)
            VALUES (:user,:email,'x',:first,:last,'USER',true,0,:org,
                    now(),now(),false)"""),
        {
            "user": user,
            "org": org,
            "email": f"draft-{user}@test.invalid",
            "first": field_encryption.encrypt_field("Task", "first_name"),
            "last": field_encryption.encrypt_field("Result", "last_name"),
        },
    )


async def _seed(engine: AsyncEngine) -> dict[str, UUID]:
    field_encryption = _ensure_fixture_encryption()
    ids = {
        name: uuid4()
        for name in (
            "org",
            "user",
            "workspace",
            "collection",
            "document",
            "foreign_org",
            "foreign_user",
        )
    }
    async with engine.begin() as connection:
        await _seed_org_user(connection, ids["org"], ids["user"], field_encryption)
        await _seed_org_user(
            connection, ids["foreign_org"], ids["foreign_user"], field_encryption
        )
        await connection.execute(
            text("""INSERT INTO workspaces
                (id,name,is_archived,is_public,owner_id,organization_id,
                 created_at,updated_at,is_deleted)
                VALUES (:workspace,'drafts',false,false,:user,:org,
                        now(),now(),false)"""),
            ids,
        )
        await connection.execute(
            text("""INSERT INTO collections
                (id,workspace_id,name,project_type,research_status,tags,is_private,
                 created_at,updated_at,is_deleted)
                VALUES (:collection,:workspace,'drafts','research','active',
                        '[]',true,now(),now(),false)"""),
            ids,
        )
    async with AsyncSession(engine) as db:
        content = "Evidence unique to the seeded source."
        db.add(
            Document(
                id=ids["document"],
                title="Seeded source",
                filename="seeded.txt",
                file_path="/unused/seeded.txt",
                file_size_bytes=len(content),
                mime_type="text/plain",
                document_type=DocumentType.TEXT,
                processing_status=ProcessingStatus.COMPLETED,
                content_text=content,
                content_summary=content,
                organization_id=ids["org"],
                is_deleted=False,
            )
        )
        db.add(
            CollectionDocument(
                collection_id=ids["collection"],
                document_id=ids["document"],
                is_deleted=False,
            )
        )
        await db.commit()
    return ids


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value

    async def get(self, key: str) -> str | None:
        return self.store.get(key)


async def _passing_review(
    _db: AsyncSession, content: str, _documents: list[Any]
) -> dict[str, Any]:
    indices = DraftGenerationService._citation_indices(content)
    return {
        "verdicts": [
            {
                "doc_index": index,
                "verdict": "exact",
                "evidence": "Evidence unique to the seeded source.",
                "page_number": 1,
                "location": "Page 1",
            }
            for index in indices
        ],
        "summary": {"exact": len(indices), "minor": 0, "major": 0, "unverified": 0},
        "docs_checked": len(indices),
        "docs_skipped": 0,
    }


@pytest.fixture
def fake_redis(factory: async_sessionmaker[AsyncSession]) -> Iterator[_FakeRedis]:
    redis = _FakeRedis()
    with (
        patch(f"{_MODULE}.AsyncSessionLocal", factory),
        patch(f"{_MODULE}.get_redis", new=AsyncMock(return_value=redis)),
        patch.object(
            DraftGenerationService, "_init_openai_client", return_value=(None, "")
        ),
        patch.object(
            DraftGenerationService,
            "_review_citations",
            new=AsyncMock(side_effect=_passing_review),
        ),
    ):
        yield redis
    _generation_status.clear()


async def _accept(
    factory: async_sessionmaker[AsyncSession], ids: dict[str, UUID], task_id: str
) -> None:
    """What generate_draft does at acceptance: running row + live status."""
    async with factory() as db:
        await start_task(
            db,
            task_id=task_id,
            collection_id=ids["collection"],
            actor_user_id=ids["user"],
            request_fingerprint="f" * 64,
        )
        await db.commit()
    _generation_status[task_id] = {
        "status": "pending",
        "progress": 0,
        "current_step": "Initializing",
        "started_at": datetime.utcnow().isoformat(),
        "project_id": str(ids["collection"]),
        "user_id": str(ids["user"]),
    }


async def _generate(ids: dict[str, UUID], task_id: str) -> None:
    await DraftGenerationService(db=cast(AsyncSession, None))._generate_draft_async(
        task_id=task_id,
        project_id=ids["collection"],
        user_id=ids["user"],
        themes=["retained results"],
        document_ids=[ids["document"]],
        style="academic",
        max_sections=3,
        include_abstract=False,
        selection_mode="explicit",
        generation_request_hash="f" * 64,
    )


def _content(label: str) -> tuple[str, bool]:
    return (f"A finding for {label} from the seeded source [Doc 1].", False)


async def _drafts(factory: async_sessionmaker[AsyncSession]) -> list[GeneratedDraft]:
    async with factory() as db:
        return list((await db.execute(select(GeneratedDraft))).scalars().all())


async def _fresh_row(
    factory: async_sessionmaker[AsyncSession], task_id: str
) -> DraftTaskResult:
    async with factory() as db:
        row = await reconcile_task(db, task_id)
        assert row is not None
        return row


async def _complete_one(
    factory: async_sessionmaker[AsyncSession],
    ids: dict[str, UUID],
    fake_redis: _FakeRedis,
    task_id: str,
) -> None:
    await _accept(factory, ids, task_id)
    with patch.object(
        DraftGenerationService,
        "_build_draft_content",
        new=AsyncMock(return_value=_content(task_id)),
    ):
        await _generate(ids, task_id)
    # Cache expiry + process-local loss.
    _generation_status.clear()
    fake_redis.store.clear()


# 1 -------------------------------------------------------------------------
async def test_terminal_result_survives_cache_and_memory_loss(
    decision_engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    fake_redis: _FakeRedis,
) -> None:
    ids = await _seed(decision_engine)
    await _complete_one(factory, ids, fake_redis, "durable-1")

    row = await _fresh_row(factory, "durable-1")
    (draft,) = await _drafts(factory)
    assert row.state == "completed"
    assert row.artifact_id == draft.id
    assert row.artifact_version == 1
    assert row.artifact_hash == _sha(draft.content)
    assert row.terminal_at is not None


# 2 -------------------------------------------------------------------------
async def test_kill_after_artifact_commit_before_publish_keeps_the_result(
    decision_engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    fake_redis: _FakeRedis,
) -> None:
    ids = await _seed(decision_engine)
    await _accept(factory, ids, "killed-late")
    real_publish = DraftGenerationService.publish_status.__func__  # type: ignore[attr-defined]

    async def dying_publish(cls: Any, task_id: str) -> None:
        if _generation_status.get(task_id, {}).get("status") == "completed":
            raise SystemExit("killed after the draft commit")
        await real_publish(cls, task_id)

    with (
        patch.object(
            DraftGenerationService,
            "_build_draft_content",
            new=AsyncMock(return_value=_content("killed-late")),
        ),
        patch.object(
            DraftGenerationService, "publish_status", classmethod(dying_publish)
        ),
        pytest.raises(SystemExit),
    ):
        await _generate(ids, "killed-late")

    cached = fake_redis.store.get("research:draft-status:killed-late") or ""
    assert '"completed"' not in cached, "the cache must not have seen completion"
    _generation_status.clear()

    row = await _fresh_row(factory, "killed-late")
    (draft,) = await _drafts(factory)
    assert (row.state, row.artifact_id) == ("completed", draft.id)
    assert row.artifact_hash == _sha(draft.content)


# 3 -------------------------------------------------------------------------
async def test_kill_before_artifact_commit_becomes_interrupted(
    decision_engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    fake_redis: _FakeRedis,
) -> None:
    ids = await _seed(decision_engine)
    await _accept(factory, ids, "killed-early")
    flushed: list[Any] = []

    async def die_at_completion(db: AsyncSession, **kwargs: Any) -> bool:
        # The draft is inserted and flushed in this transaction; the process
        # dies right before the commit that would have made it durable.
        if kwargs["state"] == "completed":
            flushed.append(kwargs["artifact"].id)
            raise SystemExit("killed after the draft flush, before commit")
        return await finish_task(db, **kwargs)

    with (
        patch.object(
            DraftGenerationService,
            "_build_draft_content",
            new=AsyncMock(return_value=_content("killed-early")),
        ),
        patch(f"{_MODULE}.finish_task", new=die_at_completion),
        pytest.raises(SystemExit),
    ):
        await _generate(ids, "killed-early")

    assert flushed and flushed[0] is not None, "the kill came before the flush"
    assert await _drafts(factory) == [], "an uncommitted draft survived the kill"
    assert (await _fresh_row(factory, "killed-early")).state == "running"

    async with factory() as db:  # what a dead process leaves behind
        await db.execute(
            update(DraftTaskResult)
            .where(DraftTaskResult.task_id == "killed-early")
            .values(heartbeat_at=datetime.now(timezone.utc) - timedelta(minutes=10))
        )
        await db.commit()

    row = await _fresh_row(factory, "killed-early")
    assert (row.state, row.error_code) == ("interrupted", "process_lost")
    assert row.artifact_id is None
    assert await _drafts(factory) == []


# 4 -------------------------------------------------------------------------
async def test_concurrent_tasks_each_bind_their_own_draft(
    decision_engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    fake_redis: _FakeRedis,
) -> None:
    ids = await _seed(decision_engine)
    await _accept(factory, ids, "concurrent-a")
    await _accept(factory, ids, "concurrent-b")
    contents = iter([_content("first"), _content("second")])

    async def build(*_args: Any, **_kwargs: Any) -> tuple[str, bool]:
        return next(contents)

    with patch.object(
        DraftGenerationService, "_build_draft_content", new=AsyncMock(side_effect=build)
    ):
        await asyncio.gather(
            _generate(ids, "concurrent-a"), _generate(ids, "concurrent-b")
        )

    drafts: dict[Any, GeneratedDraft] = {d.id: d for d in await _drafts(factory)}
    rows = [await _fresh_row(factory, t) for t in ("concurrent-a", "concurrent-b")]
    assert [row.state for row in rows] == ["completed", "completed"]
    assert {row.artifact_id for row in rows} == set(drafts), "a draft was shared"
    for row in rows:
        own = drafts[cast(UUID, row.artifact_id)]
        assert row.artifact_hash == _sha(own.content)
        assert row.artifact_version == own.version
    assert sum(draft.is_current for draft in drafts.values()) == 1
    (non_current,) = [d for d in drafts.values() if not d.is_current]
    assert non_current.id in {row.artifact_id for row in rows}


# 5 -------------------------------------------------------------------------
async def test_concurrent_duplicate_terminal_writes_yield_one_result(
    decision_engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    fake_redis: _FakeRedis,
) -> None:
    ids = await _seed(decision_engine)
    await _accept(factory, ids, "duplicate")
    async with factory() as db:
        draft = GeneratedDraft(
            project_id=ids["collection"],
            version=1,
            title="Draft 1",
            content="dup content",
            themes=[],
            is_current=True,
        )
        db.add(draft)
        await db.commit()

    async def write() -> bool:
        async with factory() as db:
            won = await finish_task(
                db, task_id="duplicate", state="completed", artifact=draft
            )
            await asyncio.sleep(0.05)  # hold the row lock across the race
            await db.commit()
            return won

    results = await asyncio.gather(write(), write())

    assert sorted(results) == [False, True], "both writers claimed the terminal"
    async with factory() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(DraftTaskResult)
            .where(DraftTaskResult.task_id == "duplicate")
        )
    assert count == 1
    row = await _fresh_row(factory, "duplicate")
    assert row.terminal_at is not None and row.artifact_id == draft.id


# 6 -------------------------------------------------------------------------
@pytest.mark.parametrize("cancel_via", ["cancel_task", "other_replica"])
async def test_cancel_before_completion_never_lands_a_draft(
    decision_engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    fake_redis: _FakeRedis,
    cancel_via: str,
) -> None:
    """``cancel_task``: this replica's in-memory flip stops the coroutine.
    ``other_replica``: only the DB row is cancelled (no in-memory flip), so the
    completion reaches window 3 and must be refused by the row guard."""
    ids = await _seed(decision_engine)
    await _accept(factory, ids, "cancel-race")
    release = asyncio.Event()
    entered = asyncio.Event()

    async def blocked_build(*_args: Any, **_kwargs: Any) -> tuple[str, bool]:
        entered.set()
        await release.wait()
        return _content("cancel-race")

    with patch.object(
        DraftGenerationService,
        "_build_draft_content",
        new=AsyncMock(side_effect=blocked_build),
    ):
        running = asyncio.create_task(_generate(ids, "cancel-race"))
        await entered.wait()
        async with factory() as db:
            if cancel_via == "cancel_task":
                assert await DraftGenerationService.cancel_task(db, "cancel-race")
            else:
                assert await finish_task(
                    db,
                    task_id="cancel-race",
                    state="cancelled",
                    error_code="cancelled_by_user",
                )
                await db.commit()
        release.set()
        await running

    assert await _drafts(factory) == [], "a cancelled task landed a draft"
    row = await _fresh_row(factory, "cancel-race")
    assert (row.state, row.artifact_id) == ("cancelled", None)
    assert _generation_status["cancel-race"]["status"] == "cancelled"


# 7 -------------------------------------------------------------------------
async def test_status_reload_is_scoped_by_project_access(
    decision_engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    fake_redis: _FakeRedis,
) -> None:
    ids = await _seed(decision_engine)
    await _complete_one(factory, ids, fake_redis, "reload-1")
    (draft,) = await _drafts(factory)
    current = {"user": ids["user"]}

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app = FastAPI()
    app.include_router(drafts_router)
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=current["user"]
    )
    base = f"/api/v1/projects/{ids['collection']}/drafts"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await client.get(f"{base}/status/reload-1")
        assert owner.status_code == 200, owner.text
        body = owner.json()
        assert body["status"] == "completed"
        assert body["draft_id"] == str(draft.id)
        assert body["artifact_version"] == 1
        assert body["artifact_hash"] == _sha(draft.content)
        assert body["state_source"] == "database"

        current["user"] = ids["foreign_user"]
        assert (await client.get(f"{base}/status/reload-1")).status_code == 404
        current["user"] = ids["user"]

        async with decision_engine.begin() as connection:
            await connection.execute(
                text("UPDATE collections SET research_status='archived' WHERE id=:c"),
                {"c": ids["collection"]},
            )
        assert (await client.get(f"{base}/status/reload-1")).status_code == 200
        assert (await client.post(f"{base}/cancel/reload-1")).status_code == 409

        async with decision_engine.begin() as connection:
            await connection.execute(
                text("UPDATE collections SET is_deleted=true WHERE id=:c"),
                {"c": ids["collection"]},
            )
        assert (await client.get(f"{base}/status/reload-1")).status_code == 404
