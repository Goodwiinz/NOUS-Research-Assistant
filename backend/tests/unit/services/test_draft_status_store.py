"""Draft progress must be readable by another API replica."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.services.research import draft_generation_service as draft_module

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_draft_status_survives_process_local_state_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: dict[str, str] = {}

    class FakeRedis:
        async def setex(self, key: str, ttl: int, value: str) -> None:
            assert ttl > 0
            stored[key] = value

        async def get(self, key: str) -> str | None:
            return stored.get(key)

    monkeypatch.setattr(draft_module, "get_redis", AsyncMock(return_value=FakeRedis()))
    task_id = "draft-cross-replica"
    project_id = uuid4()
    user_id = uuid4()
    draft_module._generation_status[task_id] = {
        "status": draft_module.DraftGenerationStatus.PENDING,
        "progress": 0,
        "current_step": "Initializing",
        "project_id": str(project_id),
        "user_id": str(user_id),
    }
    service = draft_module.DraftGenerationService(MagicMock())

    try:
        await service._set_status(
            task_id,
            draft_module.DraftGenerationStatus.GENERATING,
            30,
            "Generating content",
        )
        draft_module._generation_status.pop(task_id)

        snapshot = await service.get_status_shared(task_id)
        assert snapshot is not None
        assert snapshot["status"] == "generating"
        assert snapshot["progress"] == 30
        assert snapshot["task_id"] == task_id
        assert snapshot["project_id"] == str(project_id)
        assert snapshot["user_id"] == str(user_id)

        # Import the route directly: the research package initializer loads
        # unrelated optional services in the lean unit test environment.
        route_path = (
            Path(draft_module.__file__).resolve().parents[2] / "api/research/drafts.py"
        )
        route_spec = importlib.util.spec_from_file_location(
            "draft_status_route", route_path
        )
        assert route_spec is not None and route_spec.loader is not None
        route = importlib.util.module_from_spec(route_spec)
        route_spec.loader.exec_module(route)

        with patch.object(route, "_validate_project_ownership", new=AsyncMock()):
            response = await route.get_generation_status(
                project_id=project_id,
                task_id=task_id,
                current_user=SimpleNamespace(id=user_id),
                db=MagicMock(),
            )
            assert response["status"] == "generating"

            for wrong_project, wrong_user in (
                (uuid4(), user_id),
                (project_id, uuid4()),
            ):
                with pytest.raises(HTTPException) as denied:
                    await route.get_generation_status(
                        project_id=wrong_project,
                        task_id=task_id,
                        current_user=SimpleNamespace(id=wrong_user),
                        db=MagicMock(),
                    )
                assert denied.value.status_code == 404

            draft_module._generation_status[task_id] = {
                key: value for key, value in snapshot.items() if key != "task_id"
            }
            await route.cancel_generation(
                project_id=project_id,
                task_id=task_id,
                current_user=SimpleNamespace(id=user_id),
                db=MagicMock(),
            )
            draft_module._generation_status.pop(task_id)
            cancelled = await service.get_status_shared(task_id)
            assert cancelled is not None
            assert cancelled["status"] == "cancelled"

        with patch.object(
            route,
            "_validate_project_ownership",
            new=AsyncMock(side_effect=HTTPException(status_code=404)),
        ):
            with pytest.raises(HTTPException) as deleted:
                await route.get_generation_status(
                    project_id=project_id,
                    task_id=task_id,
                    current_user=SimpleNamespace(id=user_id),
                    db=MagicMock(),
                )
            assert deleted.value.status_code == 404
    finally:
        draft_module._generation_status.pop(task_id, None)


@pytest.mark.asyncio
async def test_stale_active_snapshot_does_not_claim_work_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = f"draft-{uuid4().hex[:12]}"
    stale = {
        "task_id": task_id,
        "status": draft_module.DraftGenerationStatus.GENERATING,
        "progress": 30,
        "current_step": "Generating content",
        "updated_at": (datetime.utcnow() - timedelta(minutes=10)).isoformat(),
    }
    redis = SimpleNamespace(get=AsyncMock(return_value=json.dumps(stale)))
    monkeypatch.setattr(draft_module, "get_redis", AsyncMock(return_value=redis))

    assert await draft_module.DraftGenerationService.get_status_shared(task_id) is None


@pytest.mark.asyncio
async def test_completed_snapshot_remains_available_after_two_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = f"draft-{uuid4().hex[:12]}"
    now = 0
    stored: dict[str, tuple[int, str]] = {}

    class ExpiringRedis:
        async def setex(self, key: str, ttl: int, value: str) -> None:
            stored[key] = (now + ttl, value)

        async def get(self, key: str) -> str | None:
            expires_at, value = stored[key]
            return value if now < expires_at else None

    monkeypatch.setattr(
        draft_module, "get_redis", AsyncMock(return_value=ExpiringRedis())
    )
    draft_module._generation_status[task_id] = {
        "status": draft_module.DraftGenerationStatus.COMPLETED,
        "progress": 100,
        "current_step": "Draft completed",
        "draft_id": str(uuid4()),
        "updated_at": datetime.utcnow().isoformat(),
    }
    try:
        await draft_module.DraftGenerationService.publish_status(task_id)
        draft_module._generation_status.pop(task_id)
        now = 2 * 3600

        status = await draft_module.DraftGenerationService.get_status_shared(task_id)
        assert status is not None
        assert status["status"] == draft_module.DraftGenerationStatus.COMPLETED
    finally:
        draft_module._generation_status.pop(task_id, None)


@pytest.mark.asyncio
async def test_heartbeat_refreshes_active_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = f"draft-{uuid4().hex[:12]}"
    stale_time = (datetime.utcnow() - timedelta(minutes=10)).isoformat()
    snapshots: list[dict[str, object]] = []

    class FakeRedis:
        async def setex(self, key: str, ttl: int, value: str) -> None:
            snapshots.append(json.loads(value))

    monkeypatch.setattr(draft_module, "get_redis", AsyncMock(return_value=FakeRedis()))
    monkeypatch.setattr(
        draft_module.asyncio,
        "sleep",
        AsyncMock(side_effect=[None, asyncio.CancelledError()]),
    )
    draft_module._generation_status[task_id] = {
        "status": draft_module.DraftGenerationStatus.GENERATING,
        "progress": 30,
        "current_step": "Generating content",
        "updated_at": stale_time,
    }
    try:
        with pytest.raises(asyncio.CancelledError):
            await draft_module.DraftGenerationService._heartbeat_status(task_id)
        assert len(snapshots) == 1
        assert snapshots[0]["status"] == draft_module.DraftGenerationStatus.GENERATING
        assert datetime.fromisoformat(
            str(snapshots[0]["updated_at"])
        ) > datetime.fromisoformat(stale_time)
    finally:
        draft_module._generation_status.pop(task_id, None)
