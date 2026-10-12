"""``get_thread_lean``: ``get_thread``'s access answer in one SELECT (HO-3).

Every open chat header polls its handoff. That read needs only the chat's
workspace organization, so deciding access must not load every member row of
the workspace the way ``get_thread`` does.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, insert, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.services.threads import workspace_access

pytestmark = pytest.mark.unit
ORG = uuid4()
OWNER, MEMBER, FORMER_MEMBER, STRANGER = (uuid4() for _ in range(4))
WORKSPACE, CONVERSATION, THREAD = uuid4(), uuid4(), uuid4()
_USER_SQL = (
    "INSERT INTO users (id, organization_id, email, password_hash, first_name, "
    "last_name, role, is_active, login_count, created_at, updated_at, is_deleted) "
    "VALUES (:id, :org, :email, 'unused', 'T', 'U', 'USER', 1, 0, "
    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
)
_ROWS: dict[str, tuple[Any, UUID]] = {
    "thread": (Thread, THREAD),
    "conversation": (Conversation, CONVERSATION),
    "workspace": (Workspace, WORKSPACE),
}
# (case id, viewer, row changes, readable)
_CASES: list[tuple[str, UUID, dict[str, dict[str, Any]], bool]] = [
    ("owner", OWNER, {}, True),
    ("member", MEMBER, {}, True),
    ("former-member", FORMER_MEMBER, {}, False),
    ("stranger", STRANGER, {}, False),
    ("stranger-public-workspace", STRANGER, {"workspace": {"is_public": True}}, True),
    ("deleted-thread", OWNER, {"thread": {"is_deleted": True}}, False),
    ("deleted-conversation", OWNER, {"conversation": {"is_deleted": True}}, False),
    ("deleted-workspace", OWNER, {"workspace": {"is_deleted": True}}, False),
    (
        "deleted-public-workspace",
        STRANGER,
        {"workspace": {"is_public": True, "is_deleted": True}},
        False,
    ),
]


def _session(engine: AsyncEngine) -> AsyncSession:
    return async_sessionmaker(engine, expire_on_commit=False)()


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lean.db'}")
    tables: list[Any] = [
        Organization,
        User,
        Workspace,
        WorkspaceMember,
        Collection,
        Conversation,
        Thread,
    ]
    async with engine.begin() as conn:
        for model in tables:
            await conn.run_sync(model.__table__.create)
    async with _session(engine) as db:
        await db.execute(
            insert(Organization).values(id=ORG, name="Org", storage_limit_bytes=10**6)
        )
        for user_id in (OWNER, MEMBER, FORMER_MEMBER, STRANGER):
            await db.execute(
                text(_USER_SQL),
                {"id": str(user_id), "org": str(ORG), "email": f"{user_id.hex}@e.test"},
            )
        await db.execute(
            insert(Workspace).values(
                id=WORKSPACE, name="W", owner_id=OWNER, organization_id=ORG
            )
        )
        for user_id, deleted in ((MEMBER, False), (FORMER_MEMBER, True)):
            await db.execute(
                insert(WorkspaceMember).values(
                    workspace_id=WORKSPACE,
                    user_id=user_id,
                    role=WorkspaceRole.VIEWER,
                    is_deleted=deleted,
                )
            )
        await db.execute(
            insert(Conversation).values(
                id=CONVERSATION, workspace_id=WORKSPACE, title="C", created_by_id=OWNER
            )
        )
        await db.execute(
            insert(Thread).values(
                id=THREAD, conversation_id=CONVERSATION, created_by_id=OWNER
            )
        )
        await db.commit()
    yield engine
    await engine.dispose()


@pytest.mark.parametrize(
    ("viewer", "changes", "readable"),
    [case[1:] for case in _CASES],
    ids=[case[0] for case in _CASES],
)
async def test_lean_read_gives_get_threads_answer(
    engine: AsyncEngine,
    viewer: UUID,
    changes: dict[str, dict[str, Any]],
    readable: bool,
) -> None:
    async with _session(engine) as db:
        for name, values in changes.items():
            model, row_id = _ROWS[name]
            await db.execute(update(model).where(model.id == row_id).values(**values))
        await db.commit()
    async with _session(engine) as db:
        lean = await workspace_access.get_thread_lean(db, THREAD, viewer)
        if lean is not None:
            assert lean.conversation.workspace.organization_id == ORG
    async with _session(engine) as db:
        full = await workspace_access.get_thread(
            db, THREAD, viewer, include_messages=False
        )
    assert (lean is not None) is readable
    assert (full is not None) is readable


async def test_lean_read_is_one_select_without_member_rows(
    engine: AsyncEngine,
) -> None:
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with _session(engine) as db:
            thread = await workspace_access.get_thread_lean(db, THREAD, MEMBER)
            assert thread is not None
            organization_id = thread.conversation.workspace.organization_id
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert organization_id == ORG
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1
