"""Versioned chat handoff record (Plan 06 slice 3)."""

from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.artifact import Artifact, ArtifactVersion
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.integration_handoff import IntegrationHandoff
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_handoff import HandoffCreate
from src.services.integrations import handoffs
from src.services.integrations.context import IntegrationAccessDenied
from src.services.integrations.handoffs import (
    HandoffConflict,
    HandoffInvalid,
    read_latest,
    read_latest_for_thread,
    save,
)

pytestmark = pytest.mark.unit
ORG, OTHER_ORG = uuid4(), uuid4()
USER, OTHER_USER = uuid4(), uuid4()
WORKSPACE, OTHER_WORKSPACE = uuid4(), uuid4()
PROJECT, OTHER_PROJECT, FOREIGN_PROJECT = uuid4(), uuid4(), uuid4()
CONVERSATION, OTHER_CONVERSATION = uuid4(), uuid4()
THREAD, SIBLING_THREAD, FOREIGN_THREAD = uuid4(), uuid4(), uuid4()
VERSION, OTHER_PROJECT_VERSION, FOREIGN_VERSION, DELETED_VERSION = (
    uuid4() for _ in range(4)
)


def _ctx(
    thread_id: UUID | None = THREAD,
    *,
    org: UUID = ORG,
    user: UUID = USER,
    project: UUID = PROJECT,
) -> IntegrationContext:
    return IntegrationContext(
        user_id=user,
        organization_id=org,
        project_id=project,
        thread_id=thread_id,
        grant_id=uuid4(),
        consent_id=uuid4(),
    )


def _payload(parent: int | None = None, **values: Any) -> HandoffCreate:
    data: dict[str, Any] = dict(
        handoff_id=uuid4(),
        expected_parent_version=parent,
        goal="Finish the literature review",
        decisions=["Use PRISMA 2020"],
        remaining=["Screen 40 abstracts"],
        results=[{"artifact_version_id": VERSION, "summary": "Search log"}],
        harness_name="codex",
        harness_session_id="sess-1",
    )
    data.update(values)
    return HandoffCreate(**data)


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables: list[Any] = [
        Organization,
        User,
        Workspace,
        WorkspaceMember,
        Collection,
        Conversation,
        Thread,
        Artifact,
        ArtifactVersion,
        IntegrationHandoff,
    ]
    async with engine.begin() as conn:
        for model in tables:
            await conn.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await session.execute(
            insert(Organization).values(
                [
                    dict(id=ORG, name="Org", storage_limit_bytes=10**6),
                    dict(id=OTHER_ORG, name="Other", storage_limit_bytes=10**6),
                ]
            )
        )
        for user_id, org, email in (
            (USER, ORG, "o@example.test"),
            (OTHER_USER, OTHER_ORG, "x@example.test"),
        ):
            await session.execute(
                text(
                    "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, :email, 'unused', 'T', 'U', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
                ),
                {"id": str(user_id), "org": str(org), "email": email},
            )
        await session.execute(
            insert(Workspace).values(
                [
                    dict(id=WORKSPACE, name="W", owner_id=USER, organization_id=ORG),
                    dict(
                        id=OTHER_WORKSPACE,
                        name="X",
                        owner_id=OTHER_USER,
                        organization_id=OTHER_ORG,
                    ),
                ]
            )
        )
        await session.execute(
            insert(Collection).values(
                [
                    dict(id=PROJECT, name="P", workspace_id=WORKSPACE),
                    dict(id=OTHER_PROJECT, name="Q", workspace_id=WORKSPACE),
                    dict(id=FOREIGN_PROJECT, name="F", workspace_id=OTHER_WORKSPACE),
                ]
            )
        )
        await session.execute(
            insert(Conversation).values(
                [
                    dict(
                        id=CONVERSATION,
                        workspace_id=WORKSPACE,
                        title="C",
                        created_by_id=USER,
                    ),
                    dict(
                        id=OTHER_CONVERSATION,
                        workspace_id=OTHER_WORKSPACE,
                        title="X",
                        created_by_id=OTHER_USER,
                    ),
                ]
            )
        )
        await session.execute(
            insert(Thread).values(
                [
                    dict(
                        id=THREAD,
                        conversation_id=CONVERSATION,
                        created_by_id=USER,
                        source_project_id=PROJECT,
                    ),
                    dict(
                        id=SIBLING_THREAD,
                        conversation_id=CONVERSATION,
                        created_by_id=USER,
                        source_project_id=PROJECT,
                    ),
                    dict(
                        id=FOREIGN_THREAD,
                        conversation_id=OTHER_CONVERSATION,
                        created_by_id=OTHER_USER,
                        source_project_id=FOREIGN_PROJECT,
                    ),
                ]
            )
        )
        artifacts = {PROJECT: uuid4(), OTHER_PROJECT: uuid4(), FOREIGN_PROJECT: uuid4()}
        await session.execute(
            insert(Artifact).values(
                [
                    dict(
                        id=artifact_id,
                        organization_id=(
                            OTHER_ORG if project == FOREIGN_PROJECT else ORG
                        ),
                        project_id=project,
                        owner_id=OTHER_USER if project == FOREIGN_PROJECT else USER,
                        title="a.md",
                    )
                    for project, artifact_id in artifacts.items()
                ]
            )
        )

        def version(version_id: UUID, project: UUID, **values: Any) -> dict[str, Any]:
            return dict(
                id=version_id,
                artifact_id=artifacts[project],
                upload_id=uuid4(),
                title="a.md",
                mime_type="text/markdown",
                byte_size=1,
                sha256="0" * 64,
                storage_key="k",
                producer="harness",
                provenance={},
                **values,
            )

        await session.execute(
            insert(ArtifactVersion).values(
                [
                    version(VERSION, PROJECT),
                    version(OTHER_PROJECT_VERSION, OTHER_PROJECT),
                    version(FOREIGN_VERSION, FOREIGN_PROJECT),
                    version(DELETED_VERSION, PROJECT, is_deleted=True),
                ]
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


async def _count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count(IntegrationHandoff.id))) or 0)


async def test_first_save_is_version_one_and_readable(db: AsyncSession) -> None:
    assert await read_latest(db, _ctx()) is None
    saved = await save(db, _ctx(), _payload())
    assert saved.version == 1 and saved.thread_id == THREAD
    assert saved.project_id == PROJECT
    assert saved.results[0].artifact_version_id == VERSION
    latest = await read_latest(db, _ctx())
    assert latest == saved
    second = await save(db, _ctx(), _payload(parent=1, goal="Next"))
    assert second.version == 2
    assert (await read_latest(db, _ctx())) == second


async def test_unbound_grant_cannot_save(db: AsyncSession) -> None:
    with pytest.raises(IntegrationAccessDenied):
        await save(db, _ctx(None), _payload())
    with pytest.raises(IntegrationAccessDenied):
        await read_latest(db, _ctx(None))
    assert await _count(db) == 0


async def test_thread_moved_out_of_project_cannot_save(db: AsyncSession) -> None:
    await db.execute(
        update(Thread).where(Thread.id == THREAD).values(source_project_id=None)
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await save(db, _ctx(), _payload())
    assert await _count(db) == 0


async def test_replay_returns_same_version(db: AsyncSession) -> None:
    payload = _payload()
    first = await save(db, _ctx(), payload)
    # A retried request carries the same handoff_id from a fresh grant.
    again = await save(db, _ctx(), payload)
    assert again == first
    assert await _count(db) == 1


async def test_replay_with_different_body_conflicts(db: AsyncSession) -> None:
    payload = _payload()
    first = await save(db, _ctx(), payload)
    changed = payload.model_copy(update={"goal": "Something else"})
    with pytest.raises(HandoffConflict) as caught:
        await save(db, _ctx(), changed)
    assert caught.value.latest == first
    assert await _count(db) == 1


async def test_same_handoff_id_in_another_thread_is_fresh(db: AsyncSession) -> None:
    payload = _payload()
    await save(db, _ctx(), payload)
    sibling = await save(db, _ctx(SIBLING_THREAD), payload)
    assert sibling.thread_id == SIBLING_THREAD and sibling.version == 1
    assert await _count(db) == 2


async def test_same_handoff_id_in_another_org_is_fresh(db: AsyncSession) -> None:
    payload = _payload()
    await save(db, _ctx(), payload)
    foreign = _ctx(
        FOREIGN_THREAD, org=OTHER_ORG, user=OTHER_USER, project=FOREIGN_PROJECT
    )
    saved = await save(
        db,
        foreign,
        _payload(
            handoff_id=payload.handoff_id,
            results=[{"artifact_version_id": FOREIGN_VERSION, "summary": "theirs"}],
        ),
    )
    assert saved.thread_id == FOREIGN_THREAD and saved.version == 1
    # Neither tenant sees the other's record.
    assert (await read_latest(db, _ctx())).thread_id == THREAD  # type: ignore[union-attr]
    assert (
        await read_latest_for_thread(db, organization_id=ORG, thread_id=FOREIGN_THREAD)
        is None
    )


@pytest.mark.parametrize("parent", [None, 1, 3])
async def test_stale_parent_conflicts(db: AsyncSession, parent: int | None) -> None:
    await save(db, _ctx(), _payload())
    latest = await save(db, _ctx(), _payload(parent=1))
    rejected = _payload(parent=parent, goal="Stale writer")
    with pytest.raises(HandoffConflict) as caught:
        await save(db, _ctx(), rejected)
    assert caught.value.latest == latest
    # Rejected content is returned to the caller, never stored.
    assert await _count(db) == 2
    assert (await read_latest(db, _ctx())) == latest


async def test_parent_named_when_none_exists_conflicts(db: AsyncSession) -> None:
    with pytest.raises(HandoffConflict) as caught:
        await save(db, _ctx(), _payload(parent=1))
    assert caught.value.latest is None
    assert await _count(db) == 0


async def test_concurrent_save_one_wins(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulated race: the in-memory SQLite test DB cannot run two writers, so
    the loser's latest-read is forced to miss the winner's commit. The real
    unique constraint then rejects its insert (IntegrityError) and the service
    must roll back, re-read, and conflict with the winner."""
    winner = await save(db, _ctx(), _payload())
    real = handoffs._latest_row
    calls = {"n": 0}

    async def stale_once(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(*args, **kwargs)

    monkeypatch.setattr(handoffs, "_latest_row", stale_once)
    with pytest.raises(HandoffConflict) as caught:
        await save(db, _ctx(), _payload(goal="Loser"))
    assert caught.value.latest == winner
    assert await _count(db) == 1


async def test_concurrent_identical_replay_returns_winner(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulated same-handoff_id race: both requests miss each other's row on
    the replay read, the loser's insert hits the unique constraint, and after
    rollback it finds the identical winner and returns it (no 409)."""
    payload = _payload()
    winner = await save(db, _ctx(), payload)
    real_replayed = handoffs._replayed
    real_latest = handoffs._latest_row
    calls = {"replay": 0, "latest": 0}

    async def miss_replay_once(*args: Any, **kwargs: Any) -> Any:
        calls["replay"] += 1
        return None if calls["replay"] == 1 else await real_replayed(*args, **kwargs)

    async def miss_latest_once(*args: Any, **kwargs: Any) -> Any:
        calls["latest"] += 1
        return None if calls["latest"] == 1 else await real_latest(*args, **kwargs)

    monkeypatch.setattr(handoffs, "_replayed", miss_replay_once)
    monkeypatch.setattr(handoffs, "_latest_row", miss_latest_once)
    again = await save(db, _ctx(), payload)
    assert again == winner
    assert calls["replay"] == 2  # re-read after the IntegrityError rollback
    assert await _count(db) == 1


@pytest.mark.parametrize(
    "version_id", [OTHER_PROJECT_VERSION, FOREIGN_VERSION, DELETED_VERSION, uuid4()]
)
async def test_foreign_version_rejected(db: AsyncSession, version_id: UUID) -> None:
    payload = _payload(
        results=[
            {"artifact_version_id": VERSION, "summary": "ok"},
            {"artifact_version_id": version_id, "summary": "bad"},
        ]
    )
    with pytest.raises(HandoffInvalid):
        await save(db, _ctx(), payload)
    assert await _count(db) == 0


async def test_browser_read_is_org_scoped(db: AsyncSession) -> None:
    saved = await save(db, _ctx(), _payload())
    assert (
        await read_latest_for_thread(db, organization_id=ORG, thread_id=THREAD)
    ) == saved
    assert (
        await read_latest_for_thread(db, organization_id=OTHER_ORG, thread_id=THREAD)
        is None
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"goal": ""},
        {"goal": "g" * 2001},
        {"decisions": ["d"] * 51},
        {"remaining": ["r" * 501]},
        {"remaining": ["r"] * 51},
        {"decisions": ["d" * 501]},
        {
            "results": [
                {"artifact_version_id": uuid4(), "summary": "s"} for _ in range(51)
            ]
        },
        {"results": [{"artifact_version_id": uuid4(), "summary": "s" * 501}]},
        {"harness_name": "h" * 65},
        {"harness_session_id": "s" * 129},
        {"expected_parent_version": 0},
        {"unexpected": True},
    ],
)
def test_payload_bounds(bad: dict[str, Any]) -> None:
    data = _payload().model_dump()
    data.update(bad)
    with pytest.raises(ValidationError):
        HandoffCreate(**data)


async def test_reattached_thread_starts_a_fresh_chain(db: AsyncSession) -> None:
    await save(db, _ctx(), _payload())
    await save(db, _ctx(), _payload(parent=1))
    await save(db, _ctx(), _payload(parent=2))
    await db.execute(
        update(Thread)
        .where(Thread.id == THREAD)
        .values(source_project_id=OTHER_PROJECT)
    )
    await db.commit()
    moved = _ctx(project=OTHER_PROJECT)
    assert await read_latest(db, moved) is None
    first = await save(
        db,
        moved,
        _payload(
            results=[{"artifact_version_id": OTHER_PROJECT_VERSION, "summary": "B"}]
        ),
    )
    assert first.version == 1 and first.project_id == OTHER_PROJECT
    # The browser shows only the chain of the thread's current project.
    assert (
        await read_latest_for_thread(db, organization_id=ORG, thread_id=THREAD)
    ) == first
    assert await _count(db) == 4


async def test_non_chain_integrity_error_is_not_a_conflict(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_commit() -> None:
        raise IntegrityError(
            "INSERT",
            {},
            Exception(
                'insert violates foreign key constraint "fk_integration_handoffs_grant_id"'
            ),
        )

    monkeypatch.setattr(db, "commit", failing_commit)
    with pytest.raises(IntegrityError):
        await save(db, _ctx(), _payload())


def test_text_is_stripped_and_blank_lines_rejected() -> None:
    payload = _payload(goal="  Goal  ", decisions=[" d "])
    assert payload.goal == "Goal" and payload.decisions == ["d"]
    blanks: list[dict[str, Any]] = [{"goal": "   "}, {"remaining": ["  "]}]
    for bad in blanks:
        data = _payload().model_dump()
        data.update(bad)
        with pytest.raises(ValidationError):
            HandoffCreate(**data)
    with pytest.raises(ValidationError):
        _payload(results=[{"artifact_version_id": VERSION, "summary": " "}])


def test_duplicate_result_versions_rejected() -> None:
    with pytest.raises(ValidationError, match="each artifact version once"):
        _payload(
            results=[
                {"artifact_version_id": VERSION, "summary": "a"},
                {"artifact_version_id": VERSION, "summary": "b"},
            ]
        )


@pytest.mark.parametrize("ancestor", ["workspace", "conversation", "thread"])
async def test_deleted_ancestor_hides_the_handoff(
    db: AsyncSession, ancestor: str
) -> None:
    await save(db, _ctx(), _payload())
    model, key = {
        "workspace": (Workspace, WORKSPACE),
        "conversation": (Conversation, CONVERSATION),
        "thread": (Thread, THREAD),
    }[ancestor]
    # Deleted after any access check the caller made.
    await db.execute(update(model).where(model.id == key).values(is_deleted=True))
    await db.commit()
    assert (
        await read_latest_for_thread(db, organization_id=ORG, thread_id=THREAD) is None
    )
    assert await read_latest(db, _ctx()) is None


async def test_browser_read_requires_the_workspace_org(db: AsyncSession) -> None:
    await save(db, _ctx(), _payload())
    # The chat's workspace is in ORG; naming another org finds nothing even
    # though the handoff row itself carries ORG.
    assert (
        await read_latest_for_thread(db, organization_id=OTHER_ORG, thread_id=THREAD)
        is None
    )
