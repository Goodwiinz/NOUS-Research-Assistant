"""Explicitly selected project memories: owner picks, harness reads only those."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.collection import Collection
from src.models.integration_context_selection import IntegrationContextSelection
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.project_memory import ProjectMemory
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.integration_context import IntegrationContext
from src.services.integrations import selected_context
from src.services.integrations.selected_context import (
    ContextNotFound,
    ContextSelectionInvalid,
    ContextSelectionTooLarge,
    context_options,
    read_selected_context,
    save_selection,
)

pytestmark = pytest.mark.unit
USER, OTHER_USER, ORG, PROJECT, OTHER_PROJECT, WORKSPACE = (uuid4() for _ in range(6))
CONSENT, CONSENT_NO_SCOPE, GRANT, RENEWED, INTERNAL = (uuid4() for _ in range(5))
KEEP, SECRET, THIRD, FOREIGN_USER_MEMORY, OTHER_PROJECT_MEMORY = (
    uuid4() for _ in range(5)
)
SOON = datetime.now(timezone.utc) + timedelta(hours=1)


async def _seed(session: AsyncSession) -> None:
    await session.execute(
        insert(Organization).values(id=ORG, name="Org", storage_limit_bytes=1000000)
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
    for project_id, name in ((PROJECT, "Thesis"), (OTHER_PROJECT, "Other")):
        await session.execute(
            insert(Collection).values(id=project_id, name=name, workspace_id=WORKSPACE)
        )
    for memory_id, project_id, user_id, content in (
        (KEEP, PROJECT, USER, "Cite in APA"),
        (SECRET, PROJECT, USER, "Client is St. Mary's hospital"),
        (THIRD, PROJECT, USER, "Focus on post-2020 work"),
        (FOREIGN_USER_MEMORY, PROJECT, OTHER_USER, "someone else's"),
        (OTHER_PROJECT_MEMORY, OTHER_PROJECT, USER, "other project"),
    ):
        await session.execute(
            insert(ProjectMemory).values(
                id=memory_id, project_id=project_id, user_id=user_id, content=content
            )
        )
    for consent_id, scopes in (
        (CONSENT, ["tools:read", "context:read"]),
        (CONSENT_NO_SCOPE, ["tools:read"]),
    ):
        await session.execute(
            insert(IntegrationGrantRequest).values(
                id=consent_id,
                user_id=USER,
                organization_id=ORG,
                project_id=PROJECT,
                device_id=uuid4(),
                scopes=scopes,
                status="consumed",
                expires_at=SOON,
            )
        )
    for grant_id, request_id in (
        (GRANT, CONSENT),
        (RENEWED, CONSENT),
        (INTERNAL, None),
    ):
        await session.execute(
            insert(IntegrationGrant).values(
                id=grant_id,
                user_id=USER,
                organization_id=ORG,
                project_id=PROJECT,
                request_id=request_id,
                scopes=["context:read"],
                token_hash=str(grant_id).replace("-", "") * 2,
                expires_at=SOON,
                consented_at=datetime.now(timezone.utc),
            )
        )
    await session.commit()


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'context.db'}")
    tables: list[Any] = [
        Organization,
        User,
        Workspace,
        WorkspaceMember,
        Collection,
        ProjectMemory,
        IntegrationGrantRequest,
        IntegrationGrant,
        IntegrationContextSelection,
    ]
    async with engine.begin() as conn:
        for model in tables:
            await conn.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await _seed(session)
        yield session
    await engine.dispose()


async def _user(db: AsyncSession, user_id: UUID = USER) -> User:
    user = await db.get(User, user_id)
    assert user is not None
    return cast(User, user)


def _grant_context(grant_id: UUID = GRANT) -> IntegrationContext:
    return IntegrationContext(
        user_id=USER, organization_id=ORG, project_id=PROJECT, grant_id=grant_id
    )


def _memory_ids(result: Any) -> list[str]:
    return [m["memory_id"] for m in result.content[0]["memories"]]


async def test_options_list_only_the_owners_memories_in_the_consent_project(
    db: AsyncSession,
) -> None:
    options = await context_options(db, await _user(db), CONSENT)
    assert options.project_id == PROJECT and options.project_label == "Thesis"
    assert {m.id for m in options.memories} == {KEEP, SECRET, THIRD}
    assert options.selected_memory_ids == []  # nothing shared by default


@pytest.mark.parametrize(
    "case",
    ["other-user", "no-scope", "revoked", "pending", "approved-expired", "unknown"],
)
async def test_options_are_refused_outside_an_owned_context_consent(
    db: AsyncSession, case: str
) -> None:
    user, request_id = await _user(db), CONSENT
    if case == "other-user":
        user = await _user(db, OTHER_USER)
    elif case == "no-scope":
        request_id = CONSENT_NO_SCOPE
    elif case == "revoked":
        await db.execute(
            update(IntegrationGrantRequest).values(
                consent_revoked_at=datetime.now(timezone.utc)
            )
        )
        await db.commit()
    elif case == "pending":
        await db.execute(update(IntegrationGrantRequest).values(status="pending"))
        await db.commit()
    elif case == "approved-expired":
        await db.execute(
            update(IntegrationGrantRequest).values(
                status="approved",
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        await db.commit()
    else:
        request_id = uuid4()
    with pytest.raises(ContextNotFound):
        await context_options(db, user, request_id)


async def test_harness_reads_only_the_selected_memories_in_order(
    db: AsyncSession,
) -> None:
    saved = await save_selection(db, await _user(db), CONSENT, [THIRD, KEEP, THIRD])
    assert saved.selected_memory_ids == [THIRD, KEEP]
    result = await read_selected_context(db, _grant_context())
    assert result.is_error is False
    assert _memory_ids(result) == [str(THIRD), str(KEEP)]
    assert result.source_refs == [{"memory_id": str(THIRD)}, {"memory_id": str(KEEP)}]
    assert "St. Mary" not in json.dumps(result.model_dump(mode="json"))
    # A renewed grant under the same consent sees the same selection.
    renewed = await read_selected_context(db, _grant_context(RENEWED))
    assert _memory_ids(renewed) == [str(THIRD), str(KEEP)]


async def test_default_selection_shares_nothing(db: AsyncSession) -> None:
    result = await read_selected_context(db, _grant_context())
    assert result.is_error is False
    assert _memory_ids(result) == []
    assert result.source_refs == []


@pytest.mark.parametrize(
    "memory_id",
    [FOREIGN_USER_MEMORY, OTHER_PROJECT_MEMORY, uuid4()],
    ids=["other-users-memory", "other-project-memory", "unknown"],
)
async def test_selecting_a_memory_outside_the_project_is_rejected(
    db: AsyncSession, memory_id: UUID
) -> None:
    with pytest.raises(ContextSelectionInvalid):
        await save_selection(db, await _user(db), CONSENT, [KEEP, memory_id])
    assert (
        await context_options(db, await _user(db), CONSENT)
    ).selected_memory_ids == []


async def test_selection_is_capped_and_can_be_cleared(db: AsyncSession) -> None:
    with pytest.raises(ContextSelectionInvalid):
        await save_selection(db, await _user(db), CONSENT, [uuid4() for _ in range(26)])
    await save_selection(db, await _user(db), CONSENT, [KEEP])
    cleared = await save_selection(db, await _user(db), CONSENT, [])
    assert cleared.selected_memory_ids == []
    assert _memory_ids(await read_selected_context(db, _grant_context())) == []


async def test_a_deleted_or_moved_memory_disappears_from_the_read(
    db: AsyncSession,
) -> None:
    await save_selection(db, await _user(db), CONSENT, [KEEP, THIRD])
    await db.execute(
        update(ProjectMemory).where(ProjectMemory.id == KEEP).values(is_deleted=True)
    )
    await db.execute(
        update(ProjectMemory)
        .where(ProjectMemory.id == THIRD)
        .values(project_id=OTHER_PROJECT)
    )
    await db.commit()
    assert _memory_ids(await read_selected_context(db, _grant_context())) == []


@pytest.mark.parametrize(
    ("table", "values"),
    [
        ("workspace", {"is_deleted": True}),
        ("project", {"is_deleted": True}),
        ("user", {"is_active": False}),
    ],
    ids=["workspace-deleted", "project-deleted", "user-inactive"],
)
async def test_lost_project_access_fails_closed(
    db: AsyncSession, table: str, values: dict[str, Any]
) -> None:
    await save_selection(db, await _user(db), CONSENT, [KEEP])
    model: Any = {"workspace": Workspace, "project": Collection, "user": User}[table]
    target = {"workspace": WORKSPACE, "project": PROJECT, "user": USER}[table]
    await db.execute(update(model).where(model.id == target).values(**values))
    await db.commit()
    result = await read_selected_context(db, _grant_context())
    assert result.is_error is True
    assert result.content == [{"error": "context_unavailable"}]
    assert "Cite in APA" not in json.dumps(result.model_dump(mode="json"))


async def test_a_grant_without_consent_has_no_selection(db: AsyncSession) -> None:
    result = await read_selected_context(db, _grant_context(INTERNAL))
    assert result.is_error is True
    assert result.content == [{"error": "no_context_selection"}]


@pytest.mark.parametrize(
    "grant_id",
    [GRANT, uuid4()],
    ids=["grant-of-a-project-with-a-selection", "unknown-grant"],
)
async def test_a_workspace_bound_context_never_reads_a_selection(
    db: AsyncSession, grant_id: UUID
) -> None:
    # A selection is stored per project consent and the read takes its project
    # from the grant. A workspace grant has no such project (and cannot hold
    # context:read), so a context that reached the service anyway gets the same
    # stable error whatever the grant row says, and nothing is read.
    await save_selection(db, await _user(db), CONSENT, [KEEP])
    workspace_context = IntegrationContext(
        user_id=USER, organization_id=ORG, workspace_id=WORKSPACE, grant_id=grant_id
    )
    result = await read_selected_context(db, workspace_context)
    assert result.is_error is True
    assert result.content == [{"error": "context_unavailable"}]
    assert result.source_refs == []
    assert "Cite in APA" not in json.dumps(result.model_dump(mode="json"))


async def test_a_workspace_consent_cannot_hold_a_selection(db: AsyncSession) -> None:
    # check_scopes refuses context:read for a workspace consent. A row that got
    # in another way still has no project to pick memories from, so the owner
    # can neither list options for it nor save a selection under it.
    consent = uuid4()
    await db.execute(
        insert(IntegrationGrantRequest).values(
            id=consent,
            user_id=USER,
            organization_id=ORG,
            project_id=None,
            workspace_id=WORKSPACE,
            device_id=uuid4(),
            scopes=["tools:read", "context:read"],
            status="consumed",
            expires_at=SOON,
        )
    )
    await db.commit()
    with pytest.raises(ContextNotFound):
        await context_options(db, await _user(db), consent)
    with pytest.raises(ContextNotFound):
        await save_selection(db, await _user(db), consent, [KEEP])
    assert await db.scalar(select(func.count(IntegrationContextSelection.id))) == 0


async def test_concurrent_first_save_applies_instead_of_failing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await save_selection(db, await _user(db), CONSENT, [KEEP])
    # The loser of a first-save race does not see the winner's row yet.
    real = selected_context._selection
    calls = {"n": 0}

    async def stale_once(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(*args, **kwargs)

    monkeypatch.setattr(selected_context, "_selection", stale_once)
    saved = await save_selection(db, await _user(db), CONSENT, [THIRD])
    assert saved.selected_memory_ids == [THIRD]
    monkeypatch.setattr(selected_context, "_selection", real)
    assert _memory_ids(await read_selected_context(db, _grant_context())) == [
        str(THIRD)
    ]


async def test_selected_memories_stay_listed_past_the_newest_hundred(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await save_selection(db, await _user(db), CONSENT, [KEEP])
    monkeypatch.setattr(selected_context, "MAX_OPTIONS", 1)
    options = await context_options(db, await _user(db), CONSENT)
    assert KEEP in {m.id for m in options.memories}
    assert options.selected_memory_ids == [KEEP]


async def test_deleted_memories_leave_the_saved_selection(db: AsyncSession) -> None:
    await save_selection(db, await _user(db), CONSENT, [KEEP, THIRD])
    await db.execute(
        update(ProjectMemory).where(ProjectMemory.id == KEEP).values(is_deleted=True)
    )
    await db.commit()
    options = await context_options(db, await _user(db), CONSENT)
    assert options.selected_memory_ids == [THIRD]


async def test_oversized_selection_is_refused_and_growth_is_truncated(
    db: AsyncSession,
) -> None:
    # Multibyte text is measured in UTF-8 bytes, not escaped ASCII.
    big = "\u6f22" * 12_000  # 36,000 bytes each in UTF-8
    for memory_id in (KEEP, THIRD):
        await db.execute(
            update(ProjectMemory)
            .where(ProjectMemory.id == memory_id)
            .values(content=big)
        )
    await db.commit()
    with pytest.raises(ContextSelectionTooLarge):
        await save_selection(db, await _user(db), CONSENT, [KEEP, THIRD])
    await save_selection(db, await _user(db), CONSENT, [KEEP])
    # THIRD is small again when saved, then grows past the budget.
    await db.execute(
        update(ProjectMemory).where(ProjectMemory.id == THIRD).values(content="short")
    )
    await db.commit()
    await save_selection(db, await _user(db), CONSENT, [KEEP, THIRD])
    await db.execute(
        update(ProjectMemory).where(ProjectMemory.id == THIRD).values(content=big)
    )
    await db.commit()
    result = await read_selected_context(db, _grant_context())
    assert result.is_error is False
    assert _memory_ids(result) == [str(KEEP)]
    assert result.content[0]["truncated"] is True
