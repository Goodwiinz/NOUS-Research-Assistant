"""Durable, fail-closed note actions shared by native NOUS and external harnesses."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.collection import Collection
from src.models.integration_grant import IntegrationGrant
from src.models.organization import Organization
from src.models.project_note import ProjectNote
from src.models.research_project import ResearchProject
from src.models.research_project_role import ResearchProjectRoleAssignment
from src.models.tool_action import IntegrationToolAction
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.integration_tools import ToolInvocation
from src.services.agent.tool_actions import (
    STALE_EXECUTION,
    ActionActor,
    ActionConflict,
    ActionNotFound,
    ToolActionArgumentError,
    decide_action,
    drain_integration_actions,
    execute_action,
    get_action_status,
    request_action,
    sweep_stale_actions,
)

pytestmark = pytest.mark.unit
USER, OTHER_USER, ORG, PROJECT, WORKSPACE, GRANT = (uuid4() for _ in range(6))
NOTE_ARGS = {"title": "Findings", "content": "# Findings\n", "tags": ["a"]}


def _grant_values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": GRANT,
        "user_id": USER,
        "organization_id": ORG,
        "project_id": PROJECT,
        "scopes": ["tools:write"],
        "token_hash": "x" * 64,
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "consented_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return values


async def _seed(session: AsyncSession) -> None:
    await session.execute(
        insert(Organization).values(id=ORG, name="Test", storage_limit_bytes=1000000)
    )
    for user_id, email in ((USER, "owner@example.test"), (OTHER_USER, "o@e.test")):
        await session.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, :email, 'unused', 'Test', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
            ),
            {"id": str(user_id), "org": str(ORG), "email": email},
        )
    await session.execute(
        insert(Workspace).values(
            id=WORKSPACE, name="Workspace", owner_id=USER, organization_id=ORG
        )
    )
    await session.execute(
        insert(Collection).values(id=PROJECT, name="Project", workspace_id=WORKSPACE)
    )
    await session.execute(insert(IntegrationGrant).values(**_grant_values()))
    await session.commit()


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[Any]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'actions.db'}")
    tables: list[Any] = [
        Organization,
        User,
        Workspace,
        WorkspaceMember,
        Collection,
        ResearchProject,
        ResearchProjectRoleAssignment,
        ProjectNote,
        IntegrationGrant,
        IntegrationToolAction,
    ]
    async with engine.begin() as conn:
        for model in tables:
            await conn.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await _seed(session)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db(engine: Any) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


def _actor(grant_id: UUID | None = GRANT) -> ActionActor:
    return ActionActor(
        user_id=USER, organization_id=ORG, project_id=PROJECT, grant_id=grant_id
    )


def _invocation(invocation_id: UUID | None = None, **args: Any) -> ToolInvocation:
    return ToolInvocation(
        tool_name="create_project_note",
        arguments={**NOTE_ARGS, **args},
        invocation_id=invocation_id or uuid4(),
    )


async def _user(db: AsyncSession, user_id: UUID = USER) -> User:
    user = await db.get(User, user_id)
    assert user is not None
    return cast(User, user)


async def _note_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(ProjectNote)) or 0)


async def _approve(db: AsyncSession, invocation_id: UUID) -> None:
    await decide_action(db, await _user(db), invocation_id, approved=True)


async def test_request_is_idempotent_and_a_changed_payload_conflicts(
    db: AsyncSession,
) -> None:
    invocation = _invocation()
    first = await request_action(db, _actor(), invocation)
    assert first.state == "awaiting_approval"
    again = await request_action(db, _actor(), invocation)
    assert again.invocation_id == first.invocation_id
    assert await db.scalar(select(func.count()).select_from(IntegrationToolAction)) == 1
    with pytest.raises(ActionConflict):
        await request_action(
            db, _actor(), _invocation(invocation.invocation_id, title="Changed")
        )


async def test_identity_arguments_and_other_tools_are_rejected(
    db: AsyncSession,
) -> None:
    with pytest.raises(ToolActionArgumentError):
        await request_action(db, _actor(), _invocation(project_id=str(uuid4())))
    with pytest.raises(ToolActionArgumentError):
        await request_action(
            db,
            _actor(),
            ToolInvocation(
                tool_name="forget_memory", arguments={}, invocation_id=uuid4()
            ),
        )
    with pytest.raises(ToolActionArgumentError):
        await request_action(db, _actor(), _invocation(title=""))


async def test_decision_binds_once_and_only_to_the_requesting_user(
    db: AsyncSession,
) -> None:
    status = await request_action(db, _actor(), _invocation())
    with pytest.raises(ActionNotFound):
        await decide_action(
            db, await _user(db, OTHER_USER), status.invocation_id, approved=True
        )
    denied = await decide_action(
        db, await _user(db), status.invocation_id, approved=False
    )
    assert denied.state == "failed"
    with pytest.raises(ActionConflict):
        await decide_action(db, await _user(db), status.invocation_id, approved=True)
    assert await execute_action(db, status.invocation_id) is None
    assert await _note_count(db) == 0


async def test_approved_action_creates_exactly_one_note(db: AsyncSession) -> None:
    status = await request_action(db, _actor(), _invocation())
    assert await execute_action(db, status.invocation_id) is None  # not approved
    await _approve(db, status.invocation_id)
    done = await execute_action(db, status.invocation_id)
    assert done is not None and done.state == "succeeded"
    assert done.result is not None and done.result.is_error is False
    assert await _note_count(db) == 1
    note_id = UUID(done.result.content[0]["note_id"])
    note = await db.get(ProjectNote, note_id)
    assert note is not None and note.project_id == PROJECT and note.user_id == USER
    # Re-running never repeats the effect.
    assert await execute_action(db, status.invocation_id) is None
    assert await _note_count(db) == 1
    read = await get_action_status(db, _actor(), status.invocation_id)
    assert read.state == "succeeded"
    with pytest.raises(ActionNotFound):
        await get_action_status(db, _actor(grant_id=uuid4()), status.invocation_id)


async def test_revoked_grant_fails_before_execution(db: AsyncSession) -> None:
    status = await request_action(db, _actor(), _invocation())
    await _approve(db, status.invocation_id)
    await db.execute(
        update(IntegrationGrant)
        .where(IntegrationGrant.id == GRANT)
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await db.commit()
    failed = await execute_action(db, status.invocation_id)
    assert failed is not None and failed.state == "failed"
    assert await _note_count(db) == 0


async def test_native_actor_without_grant_executes_after_decision(
    db: AsyncSession,
) -> None:
    status = await request_action(db, _actor(grant_id=None), _invocation())
    await _approve(db, status.invocation_id)
    done = await execute_action(db, status.invocation_id)
    assert done is not None and done.state == "succeeded"
    assert await _note_count(db) == 1


async def test_concurrent_claim_executes_once(engine: Any, db: AsyncSession) -> None:
    status = await request_action(db, _actor(), _invocation())
    await _approve(db, status.invocation_id)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as one, maker() as two:
        results = await asyncio.gather(
            execute_action(one, status.invocation_id),
            execute_action(two, status.invocation_id),
            return_exceptions=True,
        )
    states = sorted(
        "none" if r is None else (r.state if hasattr(r, "state") else "error")
        for r in results
    )
    assert states == ["none", "succeeded"], results
    assert await _note_count(db) == 1


async def _approved_action(db: AsyncSession) -> UUID:
    status = await request_action(db, _actor(), _invocation())
    await _approve(db, status.invocation_id)
    return status.invocation_id


async def test_effect_that_committed_before_the_error_is_never_repeated(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation_id = await _approved_action(db)
    real_commit = db.commit
    calls = {"n": 0}

    async def commit_then_lose_connection() -> None:
        calls["n"] += 1
        await real_commit()
        if calls["n"] == 2:  # the claim commits first; the effect commit is second
            raise ConnectionResetError("lost after commit")

    monkeypatch.setattr(db, "commit", commit_then_lose_connection)
    with pytest.raises(ConnectionResetError):
        await execute_action(db, invocation_id)
    monkeypatch.setattr(db, "commit", real_commit)
    # Effect and receipt landed together, so the row already says so.
    assert await _note_count(db) == 1
    assert (await get_action_status(db, _actor(), invocation_id)).state == "succeeded"
    assert await execute_action(db, invocation_id) is None
    assert await drain_integration_actions(db) == 0
    assert await _note_count(db) == 1


async def test_unconfirmed_commit_stays_unknown_and_is_never_repeated(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation_id = await _approved_action(db)
    real_commit = db.commit
    calls = {"n": 0}

    async def die_before_the_effect_commits() -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionResetError("worker died mid-commit")
        await real_commit()

    monkeypatch.setattr(db, "commit", die_before_the_effect_commits)
    with pytest.raises(ConnectionResetError):
        await execute_action(db, invocation_id)
    monkeypatch.setattr(db, "commit", real_commit)
    await db.rollback()
    # The claim is durable, the effect is not; nothing re-claims an
    # `executing` row, and the sweeper only ever marks it unknown.
    assert await _note_count(db) == 0
    assert (await get_action_status(db, _actor(), invocation_id)).state == "executing"
    assert await execute_action(db, invocation_id) is None
    assert await drain_integration_actions(db) == 0
    assert await sweep_stale_actions(db) == 0  # not stale yet
    await db.execute(
        update(IntegrationToolAction).values(
            claimed_at=datetime.now(timezone.utc) - STALE_EXECUTION - timedelta(1)
        )
    )
    await db.commit()
    assert await sweep_stale_actions(db) == 1
    read = await get_action_status(db, _actor(), invocation_id)
    assert read.state == "outcome_unknown"
    assert await execute_action(db, invocation_id) is None
    assert await drain_integration_actions(db) == 0
    assert await _note_count(db) == 0


async def test_drain_executes_only_approved_rows(db: AsyncSession) -> None:
    approved = await request_action(db, _actor(), _invocation())
    await request_action(db, _actor(), _invocation())  # still awaiting approval
    await _approve(db, approved.invocation_id)
    assert await drain_integration_actions(db) == 1
    assert await _note_count(db) == 1
    assert await drain_integration_actions(db) == 0
