"""Exact chat/run stops using real SQLite authorization and native transitions."""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.conversation import Conversation
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.services.agent.agent_run_service import get_run
from src.services.agent.run_event_store import read_events
from src.services.compat import alphaxiv_stop as stop
from src.services.compat.alphaxiv import CompatibilityError
from src.shared.enums import JobStatus

ACTOR = UUID("10000000-0000-4000-8000-000000000001")
ORG = UUID("10000000-0000-4000-8000-000000000002")
WORKSPACE = UUID("10000000-0000-4000-8000-000000000003")
CONVERSATION = UUID("10000000-0000-4000-8000-000000000004")
CHAT = UUID("10000000-0000-4000-8000-000000000005")
RUN = "10000000-0000-4000-8000-000000000006"
OTHER = UUID("20000000-0000-4000-8000-000000000001")


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    # Real rollback around the ledger's SAVEPOINT requires explicit BEGIN on
    # SQLite. These tests do not claim PostgreSQL cross-connection contention.
    @event.listens_for(engine.sync_engine, "connect")
    def disable_driver_transactions(connection: Any, _record: Any) -> None:
        connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def emit_begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as connection:
        for table in (
            Organization.__table__,
            User.__table__,
            Workspace.__table__,
            WorkspaceMember.__table__,
            Conversation.__table__,
            Thread.__table__,
            AgentRun.__table__,
            AgentRunEvent.__table__,
        ):
            await connection.run_sync(table.create)
    monkeypatch.setattr(stop, "compare_and_set_status", AsyncMock(return_value=True))
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(
            insert(Organization).values(id=ORG, name="PDF", storage_limit_bytes=1000000)
        )
        await session.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, "
                "first_name, last_name, role, is_active, login_count, created_at, "
                "updated_at, is_deleted) VALUES (:id, :org, 'pdf@example.test', "
                "'unused', 'PDF', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, 0)"
            ),
            {"id": str(ACTOR), "org": str(ORG)},
        )
        await session.execute(
            insert(Workspace).values(
                id=WORKSPACE, name="PDF", owner_id=ACTOR, organization_id=ORG
            )
        )
        await session.execute(
            insert(Conversation).values(
                id=CONVERSATION,
                workspace_id=WORKSPACE,
                title="PDF",
                created_by_id=ACTOR,
            )
        )
        await session.execute(
            insert(Thread).values(id=CHAT, conversation_id=CONVERSATION, title="PDF")
        )
        await session.execute(
            insert(AgentRun).values(
                job_id=RUN,
                thread_id=CHAT,
                user_id=ACTOR,
                organization_id=ORG,
                status=JobStatus.RUNNING.value,
                execution_provider="nous",
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


async def _stop(db: AsyncSession, **kwargs: Any) -> stop.RunCancellationResult:
    return await stop.stop_pdf_chat_run(
        db,
        caller=cast(User, SimpleNamespace(id=ACTOR, organization_id=ORG)),
        chat_id=CHAT,
        run_id=RUN,
        **kwargs,
    )


async def _run_status(db: AsyncSession, run_id: str = RUN) -> str:
    db.expire_all()
    run = await get_run(db, run_id, organization_id=ORG, user_id=ACTOR)
    assert run is not None
    return str(run.status)


async def _events(db: AsyncSession) -> list[str]:
    events = await read_events(db, RUN, organization_id=ORG, user_id=ACTOR)
    return [str(item.event_type) for item in events]


@pytest.mark.parametrize("status", ["queued", "running", "recovering"])
async def test_normal_stop_reuses_native_claim_and_is_idempotent(
    db: AsyncSession, status: str
) -> None:
    await db.execute(update(AgentRun).values(status=status))
    await db.commit()
    first = await _stop(db, request_id="request-1")
    second = await _stop(db)
    assert first == stop.RunCancellationResult(RUN, JobStatus.STOPPING, True)
    assert second == stop.RunCancellationResult(RUN, JobStatus.STOPPING, False)
    assert await _run_status(db) == "stopping"
    assert await _events(db) == ["run.stopping"]
    events = await read_events(db, RUN, organization_id=ORG, user_id=ACTOR)
    assert events[0].payload["request_id"] == "request-1"


async def test_parked_stop_uses_native_terminal_claim_and_mirror(
    db: AsyncSession,
) -> None:
    await db.execute(update(AgentRun).values(status="awaiting_confirmation"))
    await db.commit()
    assert await _stop(db) == stop.RunCancellationResult(RUN, JobStatus.CANCELLED, True)
    assert await _stop(db) == stop.RunCancellationResult(
        RUN, JobStatus.CANCELLED, False
    )
    assert await _events(db) == ["run.cancelled"]
    cast(AsyncMock, stop.compare_and_set_status).assert_awaited_once_with(
        RUN, JobStatus.AWAITING_CONFIRMATION, JobStatus.CANCELLED, project=False
    )


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_terminal_run_conflicts_without_cancelling_newer_run(
    db: AsyncSession, status: str
) -> None:
    await db.execute(update(AgentRun).values(status=status))
    newer = str(uuid4())
    await db.execute(
        insert(AgentRun).values(
            job_id=newer,
            thread_id=CHAT,
            user_id=ACTOR,
            organization_id=ORG,
            status="running",
        )
    )
    await db.commit()
    with pytest.raises(CompatibilityError) as caught:
        await _stop(db)
    assert caught.value.status_code == 409
    assert await _run_status(db, newer) == "running"
    assert await _events(db) == []


async def test_repeated_cancelled_stop_never_targets_a_newer_run(
    db: AsyncSession,
) -> None:
    await db.execute(update(AgentRun).values(status="cancelled"))
    newer = str(uuid4())
    await db.execute(
        insert(AgentRun).values(
            job_id=newer,
            thread_id=CHAT,
            user_id=ACTOR,
            organization_id=ORG,
            status="running",
        )
    )
    await db.commit()
    assert (await _stop(db)).status is JobStatus.CANCELLED
    assert await _run_status(db, newer) == "running"
    assert await _events(db) == []


@pytest.mark.parametrize(
    "field,value",
    [("thread_id", OTHER), ("user_id", OTHER), ("organization_id", OTHER)],
)
async def test_exact_run_requires_same_chat_user_and_org(
    db: AsyncSession, field: str, value: UUID
) -> None:
    await db.execute(update(AgentRun).values(**{field: value}))
    await db.commit()
    with pytest.raises(CompatibilityError) as caught:
        await _stop(db)
    assert caught.value.status_code == 404
    assert (await db.execute(select(AgentRun.status))).scalar_one() == "running"
    assert (await db.execute(select(AgentRunEvent.id))).all() == []


@pytest.mark.parametrize(
    "model,values",
    [
        (User, {"is_active": False}),
        (User, {"is_deleted": True}),
        (User, {"organization_id": OTHER}),
        (Organization, {"is_active": False}),
        (Organization, {"is_deleted": True}),
        (Thread, {"is_deleted": True}),
        (Conversation, {"is_deleted": True}),
        (Workspace, {"is_deleted": True}),
        (Workspace, {"owner_id": OTHER, "is_public": False}),
    ],
)
async def test_fresh_authority_revocation_blocks_stop(
    db: AsyncSession, model: Any, values: dict[str, Any]
) -> None:
    actor = await stop.authorize_pdf_chat(db, actor_id=ACTOR, chat_id=CHAT)
    cached_thread = await stop.workspace_access.get_thread(
        db, CHAT, ACTOR, include_messages=False
    )
    assert cached_thread is not None
    await db.execute(
        update(model).values(**values).execution_options(synchronize_session=False)
    )
    await db.commit()
    with pytest.raises(CompatibilityError) as caught:
        await stop.stop_pdf_chat_run(db, caller=actor, chat_id=CHAT, run_id=RUN)
    assert caught.value.status_code == 404
    assert await _run_status(db) == "running"
    assert await _events(db) == []


@pytest.mark.parametrize("role", [WorkspaceRole.VIEWER, WorkspaceRole.EDITOR])
async def test_workspace_visibility_does_not_grant_edit_permission(
    db: AsyncSession, role: WorkspaceRole
) -> None:
    await db.execute(update(Workspace).values(owner_id=OTHER, is_public=True))
    await db.execute(
        insert(WorkspaceMember).values(workspace_id=WORKSPACE, user_id=ACTOR, role=role)
    )
    await db.commit()
    actor = await stop.authorize_pdf_chat(
        db, actor_id=ACTOR, chat_id=CHAT, require_edit=False
    )
    assert actor.id == ACTOR
    if role is WorkspaceRole.VIEWER:
        with pytest.raises(CompatibilityError) as caught:
            await _stop(db)
        assert caught.value.status_code == 404
    else:
        assert (await _stop(db)).status is JobStatus.STOPPING


# Mutation verified: removing populate_existing in alphaxiv_stop.py makes this
# test accept the revoked membership. Restore before the passing full rerun.
async def test_revoked_cached_membership_cannot_stop(db: AsyncSession) -> None:
    await db.execute(update(Workspace).values(owner_id=OTHER))
    await db.execute(
        insert(WorkspaceMember).values(
            workspace_id=WORKSPACE, user_id=ACTOR, role=WorkspaceRole.EDITOR
        )
    )
    await db.commit()
    actor = await stop.authorize_pdf_chat(db, actor_id=ACTOR, chat_id=CHAT)
    cached_thread = await stop.workspace_access.get_thread(
        db, CHAT, ACTOR, include_messages=False
    )
    assert cached_thread is not None
    await db.execute(
        update(WorkspaceMember)
        .values(is_deleted=True)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    with pytest.raises(CompatibilityError) as caught:
        await stop.stop_pdf_chat_run(db, caller=actor, chat_id=CHAT, run_id=RUN)
    assert caught.value.status_code == 404
    assert await _run_status(db) == "running"


# Mutation verified: removing expected_run_id from the parked claim in
# alphaxiv_stop.py makes the [True] case cancel the replacement, not conflict.
@pytest.mark.parametrize("parked", [False, True])
async def test_replacement_between_read_and_claim_is_never_stopped(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, parked: bool
) -> None:
    if parked:
        await db.execute(update(AgentRun).values(status="awaiting_confirmation"))
        await db.commit()
    primitive_name = (
        "abandon_awaiting_submission" if parked else "request_run_cancellation"
    )
    primitive = getattr(stop, primitive_name)
    newer = str(uuid4())

    async def replace_before_claim(session: AsyncSession, **kwargs: Any) -> Any:
        # Deterministic interleaving: replacement commits before the stale
        # claim. The real native SQL still decides whether it may mutate.
        await session.execute(
            update(AgentRun).where(AgentRun.job_id == RUN).values(status="completed")
        )
        await session.execute(
            insert(AgentRun).values(
                job_id=newer,
                thread_id=CHAT,
                user_id=ACTOR,
                organization_id=ORG,
                status="awaiting_confirmation" if parked else "running",
            )
        )
        await session.commit()
        return await primitive(session, **kwargs)

    monkeypatch.setattr(stop, primitive_name, replace_before_claim)
    with pytest.raises(CompatibilityError) as caught:
        await _stop(db)
    assert caught.value.status_code == 409
    assert await _run_status(db, newer) == (
        "awaiting_confirmation" if parked else "running"
    )
    assert (await db.execute(select(AgentRunEvent.id))).all() == []


async def test_commit_failure_rolls_back_claim_and_event(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        db, "commit", AsyncMock(side_effect=RuntimeError("private failure"))
    )
    with pytest.raises(CompatibilityError) as caught:
        await _stop(db)
    assert caught.value.status_code == 503
    assert "private" not in str(caught.value)
    assert await _run_status(db) == "running"
    assert await _events(db) == []


async def test_other_execution_provider_cannot_use_pdf_stop(db: AsyncSession) -> None:
    await db.execute(update(AgentRun).values(execution_provider="codex"))
    await db.commit()
    with pytest.raises(CompatibilityError) as caught:
        await _stop(db)
    assert caught.value.status_code == 409
    assert await _run_status(db) == "running"
