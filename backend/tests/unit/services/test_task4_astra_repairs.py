"""Regression coverage for Astra's bounded Task 4 review findings."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.models.user import User
from src.services.research.draft_generation_service import DraftGenerationService


class _UnscheduledTask:
    def add_done_callback(self, _callback: Any) -> None:
        return None

    def cancel(self) -> None:
        return None


def _snapshot_db(count: int) -> tuple[MagicMock, list[SimpleNamespace]]:
    rows = [SimpleNamespace(id=uuid4()) for _ in range(count)]
    query_result = MagicMock()
    query_result.scalars.return_value.all.return_value = rows
    db = MagicMock()
    db.execute = AsyncMock(return_value=query_result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db, rows


def _make_service_observable(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], AsyncMock]:
    from src.services.research import draft_generation_service as service_module

    monkeypatch.setattr(service_module, "_generation_status", {})
    scheduled: list[str] = []

    def capture_and_close(coro: Any) -> _UnscheduledTask:
        name = getattr(coro, "__name__", "")
        scheduled.append(name)
        coro.close()
        return _UnscheduledTask()

    monkeypatch.setattr(
        DraftGenerationService, "_fire_and_forget", staticmethod(capture_and_close)
    )
    published = AsyncMock()
    monkeypatch.setattr(DraftGenerationService, "publish_status", published)
    return scheduled, published


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("source_count", [50, 51])
async def test_project_snapshot_limit_is_checked_before_service_publication(
    monkeypatch: pytest.MonkeyPatch, source_count: int
) -> None:
    from src.services.research import draft_generation_service as service_module

    db, rows = _snapshot_db(source_count)
    scheduled, published = _make_service_observable(monkeypatch)
    monkeypatch.setattr(
        DraftGenerationService,
        "_init_openai_client",
        staticmethod(lambda: (None, "")),
    )
    service = DraftGenerationService(db)

    if source_count == 51:
        with pytest.raises(ValueError, match="document selection"):
            await service.generate_draft(
                project_id=uuid4(), user_id=uuid4(), themes=["topic"]
            )

        assert awaitable_not_called(db.commit)
        db.rollback.assert_awaited_once()
        assert scheduled == []
        published.assert_not_awaited()
        assert service_module._generation_status == {}
        return

    result = await service.generate_draft(
        project_id=uuid4(), user_id=uuid4(), themes=["topic"]
    )

    assert result["selection_mode"] == "project_snapshot"
    assert result["document_ids"] == sorted(str(row.id) for row in rows)
    assert len(result["document_ids"]) == 50
    db.commit.assert_awaited_once()
    assert scheduled == ["_generate_draft_async", "_heartbeat_status"]
    published.assert_awaited_once()
    assert result["task_id"] in service_module._generation_status


def awaitable_not_called(mock: AsyncMock) -> bool:
    """Assert a coroutine-backed mock stayed untouched without leaking a coro."""
    mock.assert_not_awaited()
    return True


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("source_count", [50, 51])
async def test_create_draft_tool_observes_project_snapshot_limit(
    monkeypatch: pytest.MonkeyPatch, source_count: int
) -> None:
    from src.services.agent import tools_impl
    from src.services.research import draft_generation_service as service_module

    project_id, user_id = uuid4(), uuid4()
    db, rows = _snapshot_db(source_count)
    scheduled, published = _make_service_observable(monkeypatch)
    monkeypatch.setattr(
        DraftGenerationService,
        "_init_openai_client",
        staticmethod(lambda: (None, "")),
    )
    monkeypatch.setattr(
        tools_impl,
        "_verify_project_ownership",
        AsyncMock(return_value=SimpleNamespace(id=project_id, name="Bound project")),
    )
    monkeypatch.setattr(
        DraftGenerationService,
        "wait_for_terminal_status",
        AsyncMock(return_value={"status": "completed", "draft_id": str(uuid4())}),
    )
    user = cast(User, SimpleNamespace(id=user_id, organization_id=uuid4()))

    result = await tools_impl._tool_create_draft(
        {"project_id": str(project_id), "themes": ["topic"]}, db, user
    )

    if source_count == 51:
        assert result.get("error")
        assert awaitable_not_called(db.commit)
        db.rollback.assert_awaited_once()
        assert scheduled == []
        published.assert_not_awaited()
        assert service_module._generation_status == {}
        return

    assert result["status"] == "completed"
    assert result["project_id"] == str(project_id)
    assert result["selection_mode"] == "project_snapshot"
    assert result["document_ids"] == sorted(str(row.id) for row in rows)
    assert len(result["document_ids"]) == 50
    db.commit.assert_awaited_once()
    assert scheduled == ["_generate_draft_async", "_heartbeat_status"]
    published.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("source_count", [50, 51])
async def test_draft_http_route_observes_project_snapshot_limit(
    monkeypatch: pytest.MonkeyPatch, source_count: int
) -> None:
    from fastapi import HTTPException

    from src.api.research import drafts as draft_api
    from src.services.research import draft_generation_service as service_module

    project_id, user_id = uuid4(), uuid4()
    db, rows = _snapshot_db(source_count)
    scheduled, published = _make_service_observable(monkeypatch)
    monkeypatch.setattr(
        DraftGenerationService,
        "_init_openai_client",
        staticmethod(lambda: (None, "")),
    )
    monkeypatch.setattr(
        draft_api, "_validate_project_ownership", AsyncMock(return_value=object())
    )
    user = cast(User, SimpleNamespace(id=user_id))

    if source_count == 51:
        with pytest.raises(HTTPException) as raised:
            await draft_api.generate_draft(
                project_id=project_id,
                themes=["topic"],
                document_ids=None,
                style="academic",
                max_sections=5,
                include_abstract=True,
                current_user=user,
                db=db,
            )

        assert raised.value.status_code == 400
        assert raised.value.detail == "Invalid draft generation request."
        assert awaitable_not_called(db.commit)
        db.rollback.assert_awaited_once()
        assert scheduled == []
        published.assert_not_awaited()
        assert service_module._generation_status == {}
        return

    result = await draft_api.generate_draft(
        project_id=project_id,
        themes=["topic"],
        document_ids=None,
        style="academic",
        max_sections=5,
        include_abstract=True,
        current_user=user,
        db=db,
    )

    assert result["selection_mode"] == "project_snapshot"
    assert result["document_ids"] == sorted(str(row.id) for row in rows)
    assert len(result["document_ids"]) == 50
    db.commit.assert_awaited_once()
    assert scheduled == ["_generate_draft_async", "_heartbeat_status"]
    published.assert_awaited_once()
