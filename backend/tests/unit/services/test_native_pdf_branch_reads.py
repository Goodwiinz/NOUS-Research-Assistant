"""Native branch reads use canonical rows, authorization and selected ancestry."""

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.threads import threads as legacy_routes
from src.api.threads.workspace_routes import messages as message_routes
from src.api.threads.workspace_routes import threads as thread_routes
from src.api.threads.workspace_routes.presenters import _thread_to_detail_response
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.chat_message import ChatMessage
from src.models.document import Document
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.services.threads import message_service, thread_service
from src.services.threads.branch_reads import MessagePageMetadata
from src.services.threads.message_branches import BranchAccessError, select_branch
from tests.unit.services.test_alphaxiv_history import (
    A1,
    A2,
    A3,
    ACTOR,
    CHAT,
    CONVERSATION,
    ORG,
    PAPER,
    START,
    U1,
    U2,
    U3,
    WORKSPACE,
)
from tests.unit.services.test_alphaxiv_history import db as db


async def test_selected_ancestry_precedes_paging_and_exposes_canonical_variants(
    db: AsyncSession,
) -> None:
    metadata = MessagePageMetadata()
    first = await message_service.list_messages(
        db,
        CHAT,
        ACTOR,
        limit=2,
        order="desc",
        branch_metadata=metadata,
    )
    assert first is not None
    messages, total, more = first
    assert [row.id for row in messages] == [A3, U3]
    assert total == 4 and more
    assert messages[0].parent_message_id == U3
    assert messages[1].parent_message_id == A1
    assert getattr(messages[1], "sibling_user_message_ids") == [U2, U3]
    assert getattr(messages[0], "sibling_user_message_ids") == []
    assert metadata.response_fields() == {
        "message_branching": True,
        "branch_document_id": PAPER,
        "selected_message_id": A3,
    }
    older = await message_service.list_messages(
        db,
        CHAT,
        ACTOR,
        limit=2,
        order="desc",
        before_id=U3,
        selected_message_id=metadata.selected_message_id,
    )
    assert older is not None
    assert [row.id for row in older[0]] == [A1, U1]
    assert older[1:] == (2, False)


async def test_native_detail_projects_only_selected_path(db: AsyncSession) -> None:
    thread = await thread_service.get_thread_detail(db, CHAT, ACTOR)
    assert thread is not None
    response = _thread_to_detail_response(thread, trusted=True)
    assert response.message_branching
    assert response.branch_document_id == PAPER
    assert response.selected_message_id == A3
    assert [row.id for row in response.messages] == [U1, A1, U3, A3]
    assert response.messages[2].sibling_user_message_ids == [U2, U3]
    assert not db.dirty


async def test_switch_fences_old_cursor_and_returns_new_selected_path(
    db: AsyncSession,
) -> None:
    first = await message_service.list_messages(db, CHAT, ACTOR, limit=2, order="desc")
    assert first is not None and first[0][-1].id == U3
    await db.rollback()
    await select_branch(db, actor_id=ACTOR, thread_id=CHAT, message_id=U2)
    with pytest.raises(BranchAccessError, match="selected branch changed") as caught:
        await message_service.list_messages(
            db,
            CHAT,
            ACTOR,
            limit=2,
            order="desc",
            before_id=U3,
            selected_message_id=A3,
        )
    assert caught.value.status_code == 409
    fresh = await message_service.list_messages(db, CHAT, ACTOR)
    assert fresh is not None
    assert [row.id for row in fresh[0]] == [U1, A1, U2, A2]


async def test_stale_head_is_rejected_even_with_a_shared_ancestor_cursor(
    db: AsyncSession,
) -> None:
    await select_branch(db, actor_id=ACTOR, thread_id=CHAT, message_id=U2)
    with pytest.raises(BranchAccessError, match="selected branch changed"):
        await message_service.list_messages(
            db,
            CHAT,
            ACTOR,
            before_id=A1,
            selected_message_id=A3,
            order="desc",
        )


@pytest.mark.parametrize("cursor", [U2, A2])
async def test_cursor_must_belong_to_pinned_selected_ancestry(
    db: AsyncSession, cursor: UUID
) -> None:
    with pytest.raises(BranchAccessError, match="not on the selected branch") as caught:
        await message_service.list_messages(
            db,
            CHAT,
            ACTOR,
            before_id=cursor,
            selected_message_id=A3,
            order="desc",
        )
    assert caught.value.status_code == 409


@pytest.mark.parametrize(
    "continuation", [{"before_id": U3}, {"offset": 2}, {"since": START}]
)
async def test_branch_continuation_requires_selected_head_pin(
    db: AsyncSession, continuation: dict[str, Any]
) -> None:
    with pytest.raises(BranchAccessError, match="selected branch changed"):
        await message_service.list_messages(db, CHAT, ACTOR, **continuation)


@pytest.mark.parametrize(
    "revocation", ["document", "actor_org", "inactive", "organization"]
)
async def test_bound_reads_refresh_document_and_actor_authority(
    db: AsyncSession, revocation: str
) -> None:
    assert await thread_service.get_thread_detail(db, CHAT, ACTOR) is not None
    assert await message_service.get_message(db, A3, ACTOR) is not None
    if revocation == "document":
        await db.execute(
            update(Document)
            .where(Document.id == PAPER)
            .values(is_deleted=True)
            .execution_options(synchronize_session=False)
        )
    elif revocation == "actor_org":
        await db.execute(
            update(User)
            .where(User.id == ACTOR)
            .values(organization_id=uuid4())
            .execution_options(synchronize_session=False)
        )
    elif revocation == "inactive":
        await db.execute(
            update(User)
            .where(User.id == ACTOR)
            .values(is_active=False)
            .execution_options(synchronize_session=False)
        )
    else:
        await db.execute(
            update(Organization)
            .where(Organization.id == ORG)
            .values(is_active=False)
            .execution_options(synchronize_session=False)
        )
    await db.commit()
    assert await thread_service.get_thread_detail(db, CHAT, ACTOR) is None
    assert await message_service.list_messages(db, CHAT, ACTOR) is None
    assert await message_service.get_message(db, A3, ACTOR) is None


async def test_legacy_linear_page_semantics_do_not_require_branch_pin(
    db: AsyncSession,
) -> None:
    await db.execute(
        update(Thread)
        .where(Thread.id == CHAT)
        .values(
            message_branching=False, branch_document_id=None, selected_message_id=None
        )
    )
    await db.commit()
    metadata = MessagePageMetadata()
    result = await message_service.list_messages(
        db, CHAT, ACTOR, order="desc", before_id=A3, limit=2, branch_metadata=metadata
    )
    assert result is not None
    assert [row.id for row in result[0]] == [U3, A2]
    assert result[1:] == (5, True)
    assert metadata.response_fields() == {
        "message_branching": False,
        "branch_document_id": None,
        "selected_message_id": None,
    }


async def test_document_revocation_does_not_remove_whole_thread_cleanup_authority(
    db: AsyncSession,
) -> None:
    await db.execute(
        update(Document).where(Document.id == PAPER).values(is_deleted=True)
    )
    await db.commit()
    assert await thread_service.get_thread_detail(db, CHAT, ACTOR) is None
    assert await thread_service.delete_thread(db, CHAT, ACTOR) is True
    assert (await db.get(Thread, CHAT)).is_deleted is True


async def test_path_order_is_ancestry_order_not_creation_timestamp(
    db: AsyncSession,
) -> None:
    await db.execute(
        update(ChatMessage)
        .where(ChatMessage.id == A1)
        .values(created_at=START + timedelta(days=1))
    )
    result = await message_service.list_messages(db, CHAT, ACTOR)
    assert result is not None
    assert [row.id for row in result[0]] == [U1, A1, U3, A3]
    assert (
        await db.scalar(select(Thread.selected_message_id).where(Thread.id == CHAT))
    ) == A3


@pytest.mark.parametrize("family", ["flat", "nested", "legacy"])
async def test_http_contract_has_atomic_binding_and_canonical_branch_fields(
    db: AsyncSession,
    family: str,
) -> None:
    app = FastAPI()
    if family == "legacy":
        app.include_router(legacy_routes.router, prefix="/api/v2")
    else:
        app.include_router(
            message_routes.router
            if family == "nested"
            else message_routes.standalone_router
        )
        app.include_router(
            thread_routes.router
            if family == "nested"
            else thread_routes.standalone_router
        )

    async def session() -> AsyncIterator[AsyncSession]:
        yield db

    async def actor() -> User:
        user = await db.get(User, ACTOR)
        assert user is not None
        return cast(User, user)

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = actor
    base = (
        f"/api/v2/workspaces/{WORKSPACE}/conversations/{CONVERSATION}/threads/{CHAT}"
        if family == "nested"
        else f"/api/v2/threads/{CHAT}"
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(base + "/messages", params={"limit": 3})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["message_branching"] is True
        assert body["branch_document_id"] == str(PAPER)
        assert body["selected_message_id"] == str(A3)
        assert [row["id"] for row in body["messages"]] == [str(U1), str(A1), str(U3)]
        assert body["messages"][2]["parent_message_id"] == str(A1)
        assert body["messages"][2]["sibling_user_message_ids"] == [str(U2), str(U3)]
        stale = await client.get(
            base + "/messages", params={"page": 2, "selected_message_id": str(A2)}
        )
        assert stale.status_code == 409, stale.text
        detail = await client.get(base)
        assert detail.status_code == 200, detail.text
        assert detail.json()["selected_message_id"] == str(A3)
        assert [row["id"] for row in detail.json()["messages"]] == list(
            map(str, [U1, A1, U3, A3])
        )
        await db.execute(
            update(Document).where(Document.id == PAPER).values(is_deleted=True)
        )
        await db.commit()
        assert (await client.get(base)).status_code == 404
        assert (
            await client.get(base, params={"include_messages": "false"})
        ).status_code == 404
