"""Tests for ``?order=`` sort direction on ``GET /threads/{id}/messages``.

Verifies both the service-layer ordering (real Postgres + ORM rows) and the
API route contract (accepts ``order`` query param, forwards it; defaults to
``asc`` for backward compatibility).

Supports newest-first initial load for the web client's paginated message list.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.chat_message import ChatMessage, MessageRole
from src.services.threads.chat_service import ChatService

pytestmark = pytest.mark.integration


async def _seed_messages(db_session, thread, user, count: int = 3):
    """Insert ``count`` user-role messages with distinct ``created_at`` values."""
    now = datetime.utcnow()
    msgs = []
    for i in range(count):
        m = ChatMessage(
            thread_id=thread.id,
            user_id=user.id,
            role=MessageRole.USER,
            content=f"msg-{i}",
            created_at=now + timedelta(milliseconds=i * 10),
            updated_at=now + timedelta(milliseconds=i * 10),
        )
        db_session.add(m)
        await db_session.commit()
        await db_session.refresh(m)
        db_session.info["_created"]["chat_messages"].append(m.id)
        msgs.append(m)
    return msgs


async def test_order_desc_returns_newest_first(
    db_session, thread_factory, user_factory
):
    """``order='desc'`` returns messages ordered newest -> oldest."""
    user = await user_factory()
    thread = await thread_factory(user=user)
    msgs = await _seed_messages(db_session, thread, user, count=3)

    service = ChatService(db_session)
    out, total = await service.list_messages(
        thread_id=thread.id,
        user_id=user.id,
        order="desc",
    )

    assert total == 3
    assert [m.id for m in out] == [m.id for m in reversed(msgs)]


async def test_order_default_is_asc(db_session, thread_factory, user_factory):
    """Omitting ``order`` preserves the legacy oldest -> newest ordering."""
    user = await user_factory()
    thread = await thread_factory(user=user)
    msgs = await _seed_messages(db_session, thread, user, count=3)

    service = ChatService(db_session)
    out, _ = await service.list_messages(thread_id=thread.id, user_id=user.id)

    assert [m.id for m in out] == [m.id for m in msgs]


async def test_order_desc_with_before_id_paginates_older(
    db_session, thread_factory, user_factory
):
    """``order='desc'`` + ``before_id`` returns rows older than the boundary,
    newest -> oldest (the newest-first older-page contract)."""
    user = await user_factory()
    thread = await thread_factory(user=user)
    msgs = await _seed_messages(db_session, thread, user, count=4)

    service = ChatService(db_session)
    # Page older than msgs[2]: expect msgs[1], msgs[0] (newest-first).
    out, _ = await service.list_messages(
        thread_id=thread.id,
        user_id=user.id,
        before_id=msgs[2].id,
        order="desc",
    )

    assert [m.id for m in out] == [msgs[1].id, msgs[0].id]


async def test_before_id_does_not_skip_equal_timestamp_rows(
    db_session, thread_factory, user_factory
):
    """The UUID tie-breaker keeps rows sharing a timestamp pageable."""
    user = await user_factory()
    thread = await thread_factory(user=user)
    timestamp = datetime.utcnow()

    for value in (1, 2, 3):
        message = ChatMessage(
            id=UUID(int=value),
            thread_id=thread.id,
            user_id=user.id,
            role=MessageRole.USER,
            content=f"msg-{value}",
            created_at=timestamp,
            updated_at=timestamp,
        )
        db_session.add(message)
        db_session.info["_created"]["chat_messages"].append(message.id)
    await db_session.commit()

    service = ChatService(db_session)
    first, _ = await service.list_messages(
        thread_id=thread.id,
        user_id=user.id,
        limit=2,
        order="desc",
    )
    second, _ = await service.list_messages(
        thread_id=thread.id,
        user_id=user.id,
        limit=2,
        before_id=first[-1].id,
        order="desc",
    )

    assert [row.id for row in first + second] == [
        UUID(int=3),
        UUID(int=2),
        UUID(int=1),
    ]


def test_route_accepts_order_query_param_and_forwards(
    test_app, test_auth_headers, monkeypatch
):
    """The route accepts ``?order=desc`` (no 422) and forwards it to the service."""
    received: dict = {}

    async def fake_list_messages(
        thread_id, user_id, limit=100, offset=0, before_id=None, since=None, order="asc"
    ):
        received["order"] = order
        return [], 0

    fake_service = AsyncMock(spec=ChatService)
    fake_service.list_messages = AsyncMock(side_effect=fake_list_messages)

    class _MockUser:
        id = uuid4()
        email = "t@example.com"

    import src.api.threads.threads as threads_module

    monkeypatch.setattr(threads_module, "get_chat_service", lambda _db: fake_service)

    test_app.dependency_overrides[get_current_user] = lambda: _MockUser()
    test_app.dependency_overrides[get_db] = lambda: AsyncMock()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _no_lifespan(_app):
        yield

    original_lifespan = test_app.router.lifespan_context
    test_app.router.lifespan_context = _no_lifespan
    try:
        with TestClient(test_app, headers=test_auth_headers) as client:
            tid = uuid4()
            resp = client.get(
                f"/api/v2/threads/{tid}/messages",
                params={"order": "desc"},
            )
            assert resp.status_code == 200, resp.text
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)

    assert received["order"] == "desc"


def test_route_defaults_order_to_asc(test_app, test_auth_headers, monkeypatch):
    """Omitting ``order`` forwards ``asc`` to the service (backward compatible)."""
    received: dict = {}

    async def fake_list_messages(
        thread_id, user_id, limit=100, offset=0, before_id=None, since=None, order="asc"
    ):
        received["order"] = order
        return [], 0

    fake_service = AsyncMock(spec=ChatService)
    fake_service.list_messages = AsyncMock(side_effect=fake_list_messages)

    class _MockUser:
        id = uuid4()
        email = "t@example.com"

    import src.api.threads.threads as threads_module

    monkeypatch.setattr(threads_module, "get_chat_service", lambda _db: fake_service)

    test_app.dependency_overrides[get_current_user] = lambda: _MockUser()
    test_app.dependency_overrides[get_db] = lambda: AsyncMock()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _no_lifespan(_app):
        yield

    original_lifespan = test_app.router.lifespan_context
    test_app.router.lifespan_context = _no_lifespan
    try:
        with TestClient(test_app, headers=test_auth_headers) as client:
            tid = uuid4()
            resp = client.get(f"/api/v2/threads/{tid}/messages")
            assert resp.status_code == 200, resp.text
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)

    assert received["order"] == "asc"
