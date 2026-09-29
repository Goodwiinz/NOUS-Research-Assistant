"""GOO-297: retained draft task terminal results.

aiosqlite, table subset only (pattern ``test_kpi_tenant_isolation.py``). The
concurrent real-PostgreSQL versions live in
``backend/tests/integration/test_draft_task_results_postgres.py``.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

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
        assert row is not None
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
    assert cancelled == "latest-1"
    assert (row.state, row.error_code) == ("cancelled", "cancelled_by_user")
