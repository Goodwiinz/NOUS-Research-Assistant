"""Tests for ``?since=`` delta fetch on ``GET /threads/{id}/messages``.

Verifies both the service-layer filter (real Postgres + ORM rows) and
the API route's contract (accepts ``since`` query param, forwards it).

Task 9 of ``docs/plans/2026-05-13-agent-persist-perf.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.chat_message import ChatMessage, MessageRole
from src.services.threads.chat_service import ChatService

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Service-layer tests (real Postgres via db_session fixture)
# ---------------------------------------------------------------------------


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
            # Force monotonically increasing timestamps so the boundary is unambiguous.
            created_at=now + timedelta(milliseconds=i * 10),
            updated_at=now + timedelta(milliseconds=i * 10),
        )
        db_session.add(m)
        await db_session.commit()
        await db_session.refresh(m)
        db_session.info["_created"]["chat_messages"].append(m.id)
        msgs.append(m)
    return msgs


async def test_since_filter_returns_only_newer(
    db_session, thread_factory, user_factory
):
    """``since=<ts>`` excludes the row at that timestamp and returns only strictly newer rows."""
    user = await user_factory()
    thread = await thread_factory(user=user)
    msgs = await _seed_messages(db_session, thread, user, count=3)

    service = ChatService(db_session)
    cutoff = msgs[0].created_at  # strictly greater-than => first row excluded

    out, total = await service.list_messages(
        thread_id=thread.id,
        user_id=user.id,
        since=cutoff,
    )

    out_ids = {m.id for m in out}
    assert msgs[0].id not in out_ids
    assert msgs[1].id in out_ids
    assert msgs[-1].id in out_ids
    # `total` is the filtered count (semantics retained per plan).
    assert total == 2


async def test_no_since_returns_all(db_session, thread_factory, user_factory):
    """Omitting ``since`` returns every (non-deleted) message in the thread."""
    user = await user_factory()
    thread = await thread_factory(user=user)
    msgs = await _seed_messages(db_session, thread, user, count=3)

    service = ChatService(db_session)
    out, total = await service.list_messages(thread_id=thread.id, user_id=user.id)

    assert total == len(msgs)
    assert {m.id for m in out} == {m.id for m in msgs}


# ---------------------------------------------------------------------------
# API-route contract test (dependency-override; service is stubbed)
# ---------------------------------------------------------------------------


def test_route_accepts_since_query_param_and_forwards_to_service(
    test_app, test_auth_headers, monkeypatch
):
    """The route must accept ``?since=<iso>`` (no 422) and pass it through to the service."""
    received: dict = {}

    async def fake_list_messages(
        thread_id, user_id, limit=100, offset=0, before_id=None, since=None, order="asc"
    ):
        received["thread_id"] = thread_id
        received["since"] = since
        return [], 0

    fake_service = AsyncMock(spec=ChatService)
    fake_service.list_messages = AsyncMock(side_effect=fake_list_messages)

    class _MockUser:
        id = uuid4()
        email = "t@example.com"

    # The route does `service = get_chat_service(db)` — patch the function the
    # route module looked up at import time so we intercept that call.
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
            since_iso = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
            resp = client.get(
                f"/api/v2/threads/{tid}/messages",
                params={"since": since_iso},
            )
            # 200 with empty messages; 422 here would mean the param wasn't declared.
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["messages"] == []
            assert body["total"] == 0
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)

    # `since` arrived at the service as a datetime, not a string.
    assert isinstance(received["since"], datetime)
