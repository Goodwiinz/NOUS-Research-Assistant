"""Native completion persists intent atomically and publishes exact saved bytes."""

import hashlib
from typing import Any, AsyncIterator, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.artifact import ArtifactVersion
from src.models.generated_draft import GeneratedDraft
from src.models.native_artifact_intent import NativeArtifactIntent
from src.models.thread import Thread
from src.models.workspace import WorkspaceMember
from src.schemas.artifact import ArtifactAccessDenied
from src.schemas.integration_context import IntegrationContext
from src.services.artifacts.native import (
    create_native_context,
    drain_native_artifacts,
    enqueue_native_artifact,
)
from src.services.artifacts.service import read_version_content
from tests.unit.services.artifacts.test_publication import (
    EDITOR_USER,
    ORG,
    PROJECT,
    THREAD,
    USER,
    WORKSPACE,
    MemoryArtifactStorage,
)
from tests.unit.services.artifacts.test_publication import (
    _use_memory_storage as _use_memory_storage,
)
from tests.unit.services.artifacts.test_publication import db as artifact_db
from tests.unit.services.artifacts.test_publication import storage as storage

pytestmark = pytest.mark.unit


@pytest.fixture
async def db(artifact_db: AsyncSession) -> AsyncIterator[AsyncSession]:
    engine = cast(AsyncEngine, artifact_db.bind)
    assert engine is not None
    async with engine.begin() as conn:
        await conn.run_sync(GeneratedDraft.__table__.create)
        await conn.run_sync(NativeArtifactIntent.__table__.create)
        await conn.run_sync(AgentRunEvent.__table__.create)
    await artifact_db.execute(
        update(Thread).where(Thread.id == THREAD).values(source_project_id=PROJECT)
    )
    await artifact_db.commit()
    yield artifact_db


@pytest.fixture(autouse=True)
def enable_native(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core.config import settings

    monkeypatch.setattr(settings, "ARTIFACTS_ENABLED", True)


async def saved_draft(
    db: AsyncSession, content: str = "# Saved review\n"
) -> GeneratedDraft:
    draft = GeneratedDraft(
        id=uuid4(),
        project_id=PROJECT,
        version=1,
        title="Saved review",
        content=content,
        themes=[],
    )
    db.add(draft)
    await db.flush()
    return draft


def context(user_id: UUID = USER) -> IntegrationContext:
    return create_native_context(
        user_id=user_id, organization_id=ORG, project_id=PROJECT, thread_id=THREAD
    )


async def test_completion_survives_restart_and_replay(db: AsyncSession) -> None:
    draft = await saved_draft(db)
    draft_id = cast(UUID, draft.id)
    await enqueue_native_artifact(db, draft_id=draft_id, context=context())
    await db.commit()
    # New caller/worker discovers only the committed intent and persisted bytes.
    db.expunge_all()
    assert await drain_native_artifacts(db) == 1
    assert await drain_native_artifacts(db) == 0
    version = await db.scalar(select(ArtifactVersion))
    assert version is not None
    assert version.sha256 == hashlib.sha256(b"# Saved review\n").hexdigest()
    assert version.provenance["producer"] == "nous"
    assert version.provenance["native_item_id"] == str(draft_id)
    assert (
        await read_version_content(
            db, user_id=USER, organization_id=ORG, version_id=version.id
        )
    )[0] == b"# Saved review\n"
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 1


async def test_native_run_emits_existing_lifecycle_event(db: AsyncSession) -> None:
    from src.services.artifacts.lifecycle import drain_artifact_outbox

    run_id = uuid4()
    db.add(
        AgentRun(
            job_id=str(run_id),
            user_id=USER,
            organization_id=ORG,
            project_id=PROJECT,
            thread_id=THREAD,
            status="running",
        )
    )
    draft = await saved_draft(db)
    await db.flush()
    await enqueue_native_artifact(
        db,
        draft_id=cast(UUID, draft.id),
        context=create_native_context(
            user_id=USER,
            organization_id=ORG,
            project_id=PROJECT,
            thread_id=THREAD,
            run_id=run_id,
        ),
    )
    await db.commit()
    assert await drain_native_artifacts(db) == 1
    assert await drain_artifact_outbox(db) == 1
    version = await db.scalar(select(ArtifactVersion))
    event = await db.scalar(select(AgentRunEvent))
    assert version is not None and event is not None
    assert version.run_id == str(run_id)
    assert (
        event.run_id == str(run_id) and event.event_type == "artifact.version_created"
    )
    assert event.payload["version_id"] == str(version.id)


async def test_final_attempt_lost_ack_reconciles_committed_receipt(
    db: AsyncSession,
) -> None:
    from datetime import datetime, timedelta, timezone

    draft = await saved_draft(db)
    await enqueue_native_artifact(db, draft_id=cast(UUID, draft.id), context=context())
    await db.commit()
    assert await drain_native_artifacts(db) == 1
    await db.execute(
        update(NativeArtifactIntent).values(
            status="processing",
            version_id=None,
            attempts=5,
            claimed_at=datetime.now(timezone.utc) - timedelta(minutes=6),
        )
    )
    await db.commit()
    assert await drain_native_artifacts(db) == 1
    intent = await db.scalar(
        select(NativeArtifactIntent).execution_options(populate_existing=True)
    )
    assert intent is not None and intent.status == "delivered" and intent.attempts == 5
    assert intent.version_id is not None and intent.last_error is None
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 1


async def test_lost_acknowledgement_replays_one_version(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    draft = await saved_draft(db)
    await enqueue_native_artifact(db, draft_id=cast(UUID, draft.id), context=context())
    await db.commit()
    execute = db.execute
    updates = 0

    async def crash_before_ack(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal updates
        if (
            getattr(statement, "is_update", False)
            and statement.table.name == "native_artifact_intents"
        ):
            updates += 1
            if updates == 2:
                raise OSError("simulated worker loss")
        return await execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", crash_before_ack)
    with pytest.raises(OSError):
        await drain_native_artifacts(db)
    monkeypatch.setattr(db, "execute", execute)
    await db.rollback()
    await db.execute(
        update(NativeArtifactIntent).values(
            claimed_at=datetime.now(timezone.utc) - timedelta(minutes=6),
        )
    )
    await db.commit()
    assert await drain_native_artifacts(db) == 1
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 1
    intent = await db.scalar(select(NativeArtifactIntent))
    assert intent is not None and intent.status == "delivered" and intent.attempts == 2


async def test_storage_failure_is_bounded_and_safe(
    db: AsyncSession, storage: MemoryArtifactStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = await saved_draft(db)
    await enqueue_native_artifact(db, draft_id=cast(UUID, draft.id), context=context())
    await db.commit()

    async def unavailable(*args: Any, **kwargs: Any) -> None:
        raise OSError("secret/storage/local-path")

    monkeypatch.setattr(storage, "put", unavailable)
    for _ in range(6):
        assert await drain_native_artifacts(db) == 0
    intent = await db.scalar(select(NativeArtifactIntent))
    assert intent is not None and intent.status == "failed" and intent.attempts == 5
    assert intent.last_error == "publication_unavailable"


async def test_repeated_worker_crashes_exhaust_claim_budget(
    db: AsyncSession, storage: MemoryArtifactStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    class WorkerLost(BaseException):
        pass

    async def lose_worker(*args: Any, **kwargs: Any) -> None:
        raise WorkerLost()

    draft = await saved_draft(db)
    await enqueue_native_artifact(db, draft_id=cast(UUID, draft.id), context=context())
    await db.commit()
    monkeypatch.setattr(storage, "put", lose_worker)
    for _ in range(5):
        with pytest.raises(WorkerLost):
            await drain_native_artifacts(db)
        await db.rollback()
        await db.execute(
            update(NativeArtifactIntent).values(
                claimed_at=datetime.now(timezone.utc) - timedelta(minutes=6)
            )
        )
        await db.commit()
    assert await drain_native_artifacts(db) == 0
    intent = await db.scalar(select(NativeArtifactIntent))
    assert intent is not None and intent.attempts == 5 and intent.status == "failed"
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 0


async def test_deleted_draft_never_publishes(db: AsyncSession) -> None:
    draft = await saved_draft(db)
    await enqueue_native_artifact(db, draft_id=cast(UUID, draft.id), context=context())
    await db.commit()
    await db.execute(
        update(GeneratedDraft)
        .where(GeneratedDraft.id == draft.id)
        .values(is_deleted=True)
    )
    await db.commit()
    assert await drain_native_artifacts(db) == 0
    intent = await db.scalar(select(NativeArtifactIntent))
    assert intent is not None and intent.last_error == "draft_unavailable"


async def test_forged_internal_grant_rejected(db: AsyncSession) -> None:
    draft = await saved_draft(db)
    forged = context().model_copy(update={"grant_id": uuid4()})
    with pytest.raises(ArtifactAccessDenied):
        await enqueue_native_artifact(db, draft_id=cast(UUID, draft.id), context=forged)


async def test_draft_rollback_removes_completion_intent(db: AsyncSession) -> None:
    draft = await saved_draft(db)
    await enqueue_native_artifact(db, draft_id=cast(UUID, draft.id), context=context())
    await db.rollback()
    assert await drain_native_artifacts(db) == 0
    assert await db.scalar(select(func.count()).select_from(NativeArtifactIntent)) == 0


async def test_repeated_enqueue_creates_one_intent(db: AsyncSession) -> None:
    draft = await saved_draft(db)
    await enqueue_native_artifact(db, draft_id=cast(UUID, draft.id), context=context())
    await enqueue_native_artifact(db, draft_id=cast(UUID, draft.id), context=context())
    await db.commit()
    assert await db.scalar(select(func.count()).select_from(NativeArtifactIntent)) == 1


async def test_changed_draft_never_publishes(db: AsyncSession) -> None:
    draft = await saved_draft(db)
    await enqueue_native_artifact(db, draft_id=cast(UUID, draft.id), context=context())
    await db.commit()
    await db.execute(
        update(GeneratedDraft)
        .where(GeneratedDraft.id == draft.id)
        .values(content="changed after acceptance")
    )
    await db.commit()
    assert await drain_native_artifacts(db) == 0
    intent = await db.scalar(select(NativeArtifactIntent))
    assert intent is not None and intent.status == "skipped"
    assert intent.last_error == "draft_changed"
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 0


async def test_chat_deleted_during_storage_cannot_publish(
    db: AsyncSession, storage: MemoryArtifactStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = await saved_draft(db)
    await enqueue_native_artifact(db, draft_id=cast(UUID, draft.id), context=context())
    await db.commit()
    put = storage.put

    async def revoke_during_put(*args: Any, **kwargs: Any) -> None:
        await put(*args, **kwargs)
        await db.execute(
            update(Thread).where(Thread.id == THREAD).values(is_deleted=True)
        )
        await db.commit()

    monkeypatch.setattr(storage, "put", revoke_during_put)
    assert await drain_native_artifacts(db) == 0
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 0
    intent = await db.scalar(select(NativeArtifactIntent))
    assert intent is not None and intent.status == "skipped"
    assert intent.last_error == "access_unavailable"


@pytest.mark.parametrize("change", ["content", "deleted"])
async def test_draft_changed_during_storage_cannot_publish(
    db: AsyncSession,
    storage: MemoryArtifactStorage,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    draft = await saved_draft(db)
    draft_id = cast(UUID, draft.id)
    await enqueue_native_artifact(db, draft_id=draft_id, context=context())
    await db.commit()
    put = storage.put

    async def change_during_put(*args: Any, **kwargs: Any) -> None:
        await put(*args, **kwargs)
        values: dict[str, Any] = (
            {"content": "changed"} if change == "content" else {"is_deleted": True}
        )
        await db.execute(
            update(GeneratedDraft).where(GeneratedDraft.id == draft_id).values(**values)
        )
        await db.commit()

    monkeypatch.setattr(storage, "put", change_during_put)
    assert await drain_native_artifacts(db) == 0
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 0


async def test_removed_editor_cannot_publish(db: AsyncSession) -> None:
    draft = await saved_draft(db)
    await enqueue_native_artifact(
        db, draft_id=cast(UUID, draft.id), context=context(EDITOR_USER)
    )
    await db.commit()
    await db.execute(
        update(WorkspaceMember)
        .where(
            WorkspaceMember.user_id == EDITOR_USER,
            WorkspaceMember.workspace_id == WORKSPACE,
        )
        .values(is_deleted=True)
    )
    await db.commit()
    assert await drain_native_artifacts(db) == 0
    intent = await db.scalar(select(NativeArtifactIntent))
    assert intent is not None and intent.status == "skipped"
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 0


async def test_external_context_cannot_become_native(db: AsyncSession) -> None:
    draft = await saved_draft(db)
    with pytest.raises(ArtifactAccessDenied):
        await enqueue_native_artifact(
            db,
            draft_id=cast(UUID, draft.id),
            context=IntegrationContext(
                user_id=USER,
                organization_id=ORG,
                project_id=PROJECT,
                grant_id=uuid4(),
                consent_id=uuid4(),
            ),
        )


async def test_write_flag_pauses_intents_preserves_pending(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.core.config import settings

    draft = await saved_draft(db)
    await enqueue_native_artifact(db, draft_id=cast(UUID, draft.id), context=context())
    await db.commit()
    monkeypatch.setattr(settings, "ARTIFACTS_ENABLED", False)
    assert await drain_native_artifacts(db) == 0
    intent = await db.scalar(select(NativeArtifactIntent))
    assert intent is not None and intent.status == "pending"
    monkeypatch.setattr(settings, "ARTIFACTS_ENABLED", True)
    assert await drain_native_artifacts(db) == 1
