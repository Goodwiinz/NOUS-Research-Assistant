"""GOO-297: retained draft task terminal results.

aiosqlite, table subset only (pattern ``test_kpi_tenant_isolation.py``). The
concurrent real-PostgreSQL versions live in
``backend/tests/integration/test_draft_task_results_postgres.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.models.draft_task_result import DraftTaskResult
from src.models.generated_draft import GeneratedDraft
from src.services.research.draft_generation_service import (
    DraftGenerationService,
    _generation_status,
    finish_task,
    reconcile_task,
    start_task,
)

pytestmark = pytest.mark.unit

# The pipeline fixture mocks asyncio.sleep; race tests need the real one.
_real_sleep = asyncio.sleep


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(DraftTaskResult.__table__.create)
        await conn.run_sync(GeneratedDraft.__table__.create)
    yield eng
    await eng.dispose()


async def test_completed_requires_exact_artifact(engine: AsyncEngine) -> None:
    async with AsyncSession(engine) as db:
        db.add(
            DraftTaskResult(
                task_id="t1",
                collection_id=uuid4(),
                actor_user_id=uuid4(),
                state="completed",
                request_fingerprint="a" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


PROJECT = UUID("11111111-1111-1111-1111-111111111111")
ACTOR = UUID("22222222-2222-2222-2222-222222222222")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def _start(db: AsyncSession, task_id: str) -> None:
    await start_task(
        db,
        task_id=task_id,
        collection_id=PROJECT,
        actor_user_id=ACTOR,
        request_fingerprint="f" * 64,
    )
    await db.commit()


async def _draft(
    db: AsyncSession, version: int, content: str, *, is_current: bool
) -> GeneratedDraft:
    draft = GeneratedDraft(
        project_id=PROJECT,
        version=version,
        title=f"Draft {version}",
        content=content,
        themes=[],
        is_current=is_current,
    )
    db.add(draft)
    await db.flush()
    return draft


async def _row(engine: AsyncEngine, task_id: str) -> DraftTaskResult:
    async with AsyncSession(engine) as db:
        row = await db.get(DraftTaskResult, task_id)
        assert isinstance(row, DraftTaskResult)
        return row


async def test_finish_task_is_one_shot(engine: AsyncEngine) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await _start(db, "t1")
        draft_a = await _draft(db, 1, "draft a", is_current=False)
        draft_b = await _draft(db, 2, "draft b", is_current=True)
        await db.commit()

        assert await finish_task(db, task_id="t1", state="completed", artifact=draft_a)
        await db.commit()
        second = await finish_task(
            db, task_id="t1", state="completed", artifact=draft_b
        )
        await db.commit()

    row = await _row(engine, "t1")
    assert second is False, "a terminal row was overwritten by a second write"
    assert row.artifact_id == draft_a.id, "terminal association moved to draft_b"
    assert row.artifact_hash == _sha("draft a")


async def test_cancelled_task_cannot_complete(engine: AsyncEngine) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await _start(db, "t1")
        draft = await _draft(db, 1, "late draft", is_current=True)
        assert await finish_task(
            db, task_id="t1", state="cancelled", error_code="cancelled_by_user"
        )
        await db.commit()
        completed = await finish_task(
            db, task_id="t1", state="completed", artifact=draft
        )
        await db.commit()

    row = await _row(engine, "t1")
    assert completed is False, "a cancelled task was allowed to complete"
    assert row.state == "cancelled"
    assert row.artifact_id is None


async def test_completion_binds_the_task_own_draft(engine: AsyncEngine) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await _start(db, "task-a")
        await _start(db, "task-b")
        draft_a = await _draft(db, 1, "content of task a", is_current=False)
        draft_b = await _draft(db, 2, "content of task b", is_current=True)
        await db.commit()

        assert await finish_task(
            db, task_id="task-b", state="completed", artifact=draft_b
        )
        assert await finish_task(
            db, task_id="task-a", state="completed", artifact=draft_a
        )
        await db.commit()

    row_a = await _row(engine, "task-a")
    row_b = await _row(engine, "task-b")
    assert row_a.artifact_id == draft_a.id, "task A bound another task's draft"
    assert row_a.artifact_version == 1
    assert row_a.artifact_hash == _sha("content of task a")
    assert row_a.artifact_hash != row_b.artifact_hash
    assert row_b.artifact_id == draft_b.id


async def test_stale_running_becomes_interrupted(engine: AsyncEngine) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await _start(db, "stale")
        await _start(db, "fresh")
        now = datetime.now(timezone.utc)
        for task_id, heartbeat in (
            ("stale", now - timedelta(minutes=10)),
            ("fresh", now),
        ):
            await db.execute(
                update(DraftTaskResult)
                .where(DraftTaskResult.task_id == task_id)
                .values(heartbeat_at=heartbeat)
            )
        await db.commit()

    async with AsyncSession(engine) as db:
        reconciled = await reconcile_task(db, "stale")
        assert reconciled is not None
        assert (reconciled.state, reconciled.error_code) == (
            "interrupted",
            "process_lost",
        )
        still_running = await reconcile_task(db, "fresh")
        assert still_running is not None
        assert still_running.state == "running", "a live task was marked interrupted"
        assert await reconcile_task(db, "missing") is None

    assert (await _row(engine, "stale")).state == "interrupted"


async def test_cancel_latest_marks_the_row_cancelled(engine: AsyncEngine) -> None:
    """Cancel without a task id must not leave a running row behind (it would
    later flip to interrupted and misattribute the user's cancel)."""
    _generation_status["latest-1"] = {
        "status": "generating",
        "progress": 30,
        "current_step": "Generating content",
        "started_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
        "project_id": str(PROJECT),
        "user_id": str(ACTOR),
    }
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await _start(db, "latest-1")
            cancelled = await DraftGenerationService.cancel_latest_task(
                db, project_id=PROJECT, user_id=ACTOR
            )
    finally:
        _generation_status.pop("latest-1", None)

    row = await _row(engine, "latest-1")
    assert cancelled == ("latest-1", True)
    assert (row.state, row.error_code) == ("cancelled", "cancelled_by_user")


# ---------------------------------------------------------------------------
# Pipeline wiring: the real ``_generate_draft_async`` and the real
# ``finish_task`` against a file-backed SQLite schema (several connections).
# ---------------------------------------------------------------------------

_MODULE = "src.services.research.draft_generation_service"
_PIPELINE_TABLES = (
    "organizations",
    "users",
    "workspaces",
    "collections",
    "documents",
    "collection_documents",
    "generated_drafts",
    "draft_citations",
    "draft_reviews",
    "draft_task_results",
)


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
                "evidence": "Seeded evidence.",
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
async def pipeline(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], UUID]]:
    """File-backed schema + one project with one document; returns the
    session factory patched in as ``AsyncSessionLocal`` and the document id."""
    from src.models import Base
    from src.models.collection import Collection, CollectionDocument
    from src.models.document import Document, DocumentType, ProcessingStatus
    from src.models.workspace import Workspace

    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'drafts.db'}")
    async with eng.begin() as conn:
        for name in _PIPELINE_TABLES:
            await conn.run_sync(Base.metadata.tables[name].create)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    document_id = uuid4()
    workspace_id = uuid4()
    org_id = uuid4()
    async with factory() as db:
        # project_documents_query joins the workspace owner (users) and
        # requires the document to share the workspace organization.
        await db.execute(
            text("""INSERT INTO users
                (id,email,password_hash,first_name,last_name,role,is_active,
                 login_count,organization_id,created_at,updated_at,is_deleted)
                VALUES (:id,'owner@test.invalid','x','x','x','USER',1,0,:org,
                        CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,0)"""),
            {"id": str(ACTOR), "org": str(org_id)},
        )
        db.add(
            Workspace(id=workspace_id, name="w", owner_id=ACTOR, organization_id=org_id)
        )
        db.add(Collection(id=PROJECT, name="p", workspace_id=workspace_id))
        db.add(
            Document(
                id=document_id,
                title="Seeded source",
                filename="s.txt",
                file_path="/unused/s.txt",
                file_size_bytes=16,
                mime_type="text/plain",
                document_type=DocumentType.TEXT,
                processing_status=ProcessingStatus.COMPLETED,
                content_text="Seeded evidence.",
                content_summary="Seeded evidence.",
                organization_id=org_id,
                is_deleted=False,
            )
        )
        db.add(
            CollectionDocument(
                collection_id=PROJECT, document_id=document_id, is_deleted=False
            )
        )
        await db.commit()
    with (
        patch(f"{_MODULE}.AsyncSessionLocal", factory),
        patch(f"{_MODULE}.get_redis", new=AsyncMock(return_value=_FakeRedis())),
        patch.object(
            DraftGenerationService, "_init_openai_client", return_value=(None, "")
        ),
        patch.object(
            DraftGenerationService,
            "_review_citations",
            new=AsyncMock(side_effect=_passing_review),
        ),
        patch.object(
            DraftGenerationService,
            "_build_draft_content",
            new=AsyncMock(return_value=("A seeded finding [Doc 1].", False)),
        ),
        patch(f"{_MODULE}.asyncio.sleep", new=AsyncMock()),
    ):
        yield factory, document_id
    _generation_status.clear()
    await eng.dispose()


async def _accept_running(
    factory: async_sessionmaker[AsyncSession], task_id: str
) -> None:
    async with factory() as db:
        await _start(db, task_id)
    _generation_status[task_id] = {
        "status": "pending",
        "progress": 0,
        "current_step": "Initializing",
        "started_at": datetime.utcnow().isoformat(),
        "project_id": str(PROJECT),
        "user_id": str(ACTOR),
    }


async def _run(task_id: str, document_id: UUID) -> None:
    await DraftGenerationService(db=cast(AsyncSession, None))._generate_draft_async(
        task_id=task_id,
        project_id=PROJECT,
        user_id=ACTOR,
        themes=["t"],
        document_ids=[document_id],
        style="academic",
        max_sections=3,
        include_abstract=False,
        selection_mode="explicit",
        generation_request_hash="f" * 64,
    )


async def _draft_count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as db:
        return int(
            await db.scalar(select(func.count()).select_from(GeneratedDraft)) or 0
        )


async def _db_state(
    factory: async_sessionmaker[AsyncSession], task_id: str
) -> DraftTaskResult:
    async with factory() as db:
        row = await db.get(DraftTaskResult, task_id)
        assert isinstance(row, DraftTaskResult)
        return row


async def test_completion_on_a_cancelled_row_rolls_the_draft_back(
    pipeline: tuple[async_sessionmaker[AsyncSession], UUID],
) -> None:
    """Row cancelled elsewhere (DB only): the real finish_task refuses the
    completion and the draft insert is rolled back."""
    factory, document_id = pipeline
    await _accept_running(factory, "pre-cancelled")
    async with factory() as db:
        assert await finish_task(
            db,
            task_id="pre-cancelled",
            state="cancelled",
            error_code="cancelled_by_user",
        )
        await db.commit()

    await _run("pre-cancelled", document_id)

    assert await _draft_count(factory) == 0, "a cancelled task landed a draft"
    assert (await _db_state(factory, "pre-cancelled")).state == "cancelled"
    assert _generation_status["pre-cancelled"]["status"] == "cancelled"
    assert _generation_status["pre-cancelled"]["current_step"] == (
        "Draft task already finished; the draft was not saved"
    ), "superseded completion leaked a raw exception message"


async def test_cancel_after_the_draft_commit_is_refused(
    pipeline: tuple[async_sessionmaker[AsyncSession], UUID],
) -> None:
    """Cancel lands between the draft commit and the COMPLETED status write:
    the DB already says completed, so the cancel must report failure and the
    cache must end up mirroring the DB, with no CancelledError escaping."""
    factory, document_id = pipeline
    await _accept_running(factory, "late-cancel")
    real_set_status = DraftGenerationService._set_status
    cancel_results: list[Any] = []

    async def cancel_then_set(
        self: DraftGenerationService, task_id: str, status: str, *args: Any, **kw: Any
    ) -> None:
        if status == "completed" and not cancel_results:
            async with factory() as db:
                cancel_results.append(
                    await DraftGenerationService.cancel_task(db, task_id)
                )
        await real_set_status(self, task_id, status, *args, **kw)

    with patch.object(DraftGenerationService, "_set_status", new=cancel_then_set):
        await _run("late-cancel", document_id)

    assert cancel_results == [False], "a cancel was reported for a completed task"
    row = await _db_state(factory, "late-cancel")
    assert row.state == "completed" and row.artifact_id is not None
    assert await _draft_count(factory) == 1
    assert _generation_status["late-cancel"]["status"] == "completed"


async def test_cancel_latest_after_the_draft_commit_is_refused(
    pipeline: tuple[async_sessionmaker[AsyncSession], UUID],
) -> None:
    """Same race as above through the no-task-id cancel: the row is already
    completed, so the cancel reports it and the cache mirrors the DB."""
    factory, document_id = pipeline
    await _accept_running(factory, "late-latest")
    real_set_status = DraftGenerationService._set_status
    cancel_results: list[Any] = []

    async def cancel_then_set(
        self: DraftGenerationService, task_id: str, status: str, *args: Any, **kw: Any
    ) -> None:
        if status == "completed" and not cancel_results:
            async with factory() as db:
                cancel_results.append(
                    await DraftGenerationService.cancel_latest_task(
                        db, project_id=PROJECT, user_id=ACTOR
                    )
                )
        await real_set_status(self, task_id, status, *args, **kw)

    with patch.object(DraftGenerationService, "_set_status", new=cancel_then_set):
        await _run("late-latest", document_id)

    assert cancel_results == [
        ("late-latest", False)
    ], "a cancel was reported for a completed task"
    row = await _db_state(factory, "late-latest")
    assert row.state == "completed" and row.artifact_id is not None
    assert await _draft_count(factory) == 1
    assert _generation_status["late-latest"]["status"] == "completed"


async def test_cancel_route_falls_back_to_the_retained_row(
    pipeline: tuple[async_sessionmaker[AsyncSession], UUID],
) -> None:
    """Another replica owns the task (no in-memory status here): the route
    cancels through the scoped DB row; a foreign actor gets 404, and a row
    that is already terminal gets 409."""
    from fastapi import HTTPException

    from src.api.research import drafts as drafts_api

    factory, _document_id = pipeline
    async with factory() as db:
        await _start(db, "remote-task")
        await _start(db, "remote-done")
        assert await finish_task(db, task_id="remote-done", state="failed")
        await db.commit()

    async def cancel(task_id: str, user_id: UUID, project_id: UUID = PROJECT) -> Any:
        async with factory() as db:
            return await drafts_api.cancel_generation(
                project_id=project_id,
                task_id=task_id,
                current_user=cast(Any, SimpleNamespace(id=user_id)),
                db=db,
            )

    with patch.object(drafts_api, "_validate_project_ownership", new=AsyncMock()):
        with pytest.raises(HTTPException) as foreign:
            await cancel("remote-task", uuid4())
        assert foreign.value.status_code == 404
        with pytest.raises(HTTPException) as other_project:
            await cancel("remote-task", ACTOR, project_id=uuid4())
        assert other_project.value.status_code == 404
        assert (await _db_state(factory, "remote-task")).state == "running"

        assert (await cancel("remote-task", ACTOR))["cancelled"] is True
        row = await _db_state(factory, "remote-task")
        assert (row.state, row.error_code) == ("cancelled", "cancelled_by_user")

        with pytest.raises(HTTPException) as finished:
            await cancel("remote-done", ACTOR)
        assert finished.value.status_code == 409


async def test_cancel_latest_route_falls_back_to_the_retained_row(
    pipeline: tuple[async_sessionmaker[AsyncSession], UUID],
) -> None:
    """``POST /cancel`` without a task id on a replica that does not hold the
    task: the scoped running row is found and cancelled; a foreign actor's or
    another project's running row is never touched."""
    from fastapi import HTTPException

    from src.api.research import drafts as drafts_api

    factory, _document_id = pipeline
    other_actor = uuid4()
    async with factory() as db:
        await _start(db, "remote-latest")
        await start_task(
            db,
            task_id="foreign-actor",
            collection_id=PROJECT,
            actor_user_id=other_actor,
            request_fingerprint="f" * 64,
        )
        await db.commit()

    async def cancel(user_id: UUID, project_id: UUID = PROJECT) -> Any:
        async with factory() as db:
            return await drafts_api.cancel_generation(
                project_id=project_id,
                task_id=None,
                current_user=cast(Any, SimpleNamespace(id=user_id)),
                db=db,
            )

    with patch.object(drafts_api, "_validate_project_ownership", new=AsyncMock()):
        with pytest.raises(HTTPException) as elsewhere:
            await cancel(ACTOR, project_id=uuid4())
        assert elsewhere.value.status_code == 400

        result = await cancel(ACTOR)
        assert (result["task_id"], result["cancelled"]) == ("remote-latest", True)
        row = await _db_state(factory, "remote-latest")
        assert (row.state, row.error_code) == ("cancelled", "cancelled_by_user")
        assert (await _db_state(factory, "foreign-actor")).state == "running"

        with pytest.raises(HTTPException) as nothing_left:
            await cancel(ACTOR)
        assert nothing_left.value.status_code == 400


async def test_duplicate_request_waits_for_durable_acceptance(
    pipeline: tuple[async_sessionmaker[AsyncSession], UUID],
) -> None:
    """Two identical requests: the first registers, then its retained-row
    commit fails. The duplicate must not have been told "already in
    progress" for that task; it registers (and commits) its own."""
    from src.services.research import draft_generation_service as module

    factory, document_id = pipeline
    real_start_task = module.start_task
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def failing_first_start(db: AsyncSession, **kw: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            raise RuntimeError("retained row commit failed")
        await real_start_task(db, **kw)

    def no_background(coro: Any) -> MagicMock:
        coro.close()
        return MagicMock()

    async def request() -> dict[str, Any]:
        async with factory() as db:
            return await DraftGenerationService(db=db).generate_draft(
                project_id=PROJECT,
                user_id=ACTOR,
                themes=["t"],
                document_ids=[document_id],
            )

    with (
        patch("asyncio.sleep", _real_sleep),
        patch(f"{_MODULE}.start_task", new=failing_first_start),
        patch.object(
            DraftGenerationService, "_fire_and_forget", new=staticmethod(no_background)
        ),
    ):
        first = asyncio.create_task(request())
        await entered.wait()
        second = asyncio.create_task(request())
        await _real_sleep(0.2)
        release.set()
        with pytest.raises(RuntimeError):
            await first
        duplicate = await asyncio.wait_for(second, timeout=5)

    assert "error" not in duplicate
    async with factory() as db:
        row = await db.get(DraftTaskResult, duplicate["task_id"])
    assert row is not None, "a duplicate was accepted for a task with no retained row"
    assert row.state == "running"


async def test_agent_recovery_reads_the_retained_row(
    pipeline: tuple[async_sessionmaker[AsyncSession], UUID],
) -> None:
    """Worker restart / Redis TTL expiry: no cached status anywhere. The
    agent's same-identity recovery must read the scoped retained row, not
    return a permanent pending ``draft_status_unavailable``."""
    from src.services.agent.tools_impl import _recover_draft_status

    factory, _document_id = pipeline
    async with factory() as db:
        for task_id in ("agent-done", "agent-stale", "agent-live"):
            await _start(db, task_id)
        draft = await _draft(db, 1, "recovered draft", is_current=True)
        assert await finish_task(
            db, task_id="agent-done", state="completed", artifact=draft
        )
        await db.execute(
            update(DraftTaskResult)
            .where(DraftTaskResult.task_id == "agent-stale")
            .values(heartbeat_at=datetime.now(timezone.utc) - timedelta(minutes=10))
        )
        await db.commit()

    def dispatched(task_id: str, user_id: UUID = ACTOR) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "project_id": str(PROJECT),
            "project_name": "P",
            "user_id": str(user_id),
            "status": "pending",
        }

    with patch("src.core.database.AsyncSessionLocal", factory):
        done = await _recover_draft_status(dispatched("agent-done"))
        stale = await _recover_draft_status(dispatched("agent-stale"))
        live = await _recover_draft_status(dispatched("agent-live"))
        foreign = await _recover_draft_status(dispatched("agent-done", uuid4()))

    assert done["_terminal_status"] is True, "a retained terminal row was ignored"
    assert (done["status"], done["draft_id"]) == ("completed", str(draft.id))
    assert (stale["status"], stale["_terminal_status"]) == ("interrupted", True)
    assert stale["automatic_retry_allowed"] is False
    assert (live["status"], live["_terminal_status"]) == ("pending", False)
    assert foreign["_terminal_status"] is False, "another actor's row was read"
    assert "draft_id" not in foreign
    assert (await _db_state(factory, "agent-done")).state == "completed"
