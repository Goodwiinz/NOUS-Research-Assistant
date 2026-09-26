from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.models.user import User
from src.services.agent.tools_impl import _tool_create_draft

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _request_session() -> tuple[MagicMock, Callable[[], bool]]:
    request_transaction_open = True

    async def commit() -> None:
        nonlocal request_transaction_open
        request_transaction_open = False

    db = MagicMock()
    db.commit = AsyncMock(side_effect=commit)
    return db, lambda: request_transaction_open


async def test_create_draft_tool_returns_completed_terminal_payload() -> None:
    project = SimpleNamespace(id=uuid4(), name="Transformers")
    user = cast(User, SimpleNamespace(id=uuid4()))
    db, request_transaction_open = _request_session()
    service = MagicMock()

    async def generate_draft(**_: object) -> dict[str, str]:
        # Source validation owns the first commit; invalid selections must
        # roll back before the worker can be published.
        assert request_transaction_open()
        await db.commit()
        return {"task_id": "task-1", "status": "pending"}

    service.generate_draft = AsyncMock(side_effect=generate_draft)

    async def wait_for_terminal_status(
        task_id: str, *, timeout_seconds: float
    ) -> dict[str, str | int]:
        assert not request_transaction_open()
        assert task_id == "task-1"
        assert timeout_seconds == 105.0
        return {
            "task_id": "task-1",
            "status": "completed",
            "draft_id": "draft-9",
            "progress": 100,
        }

    with (
        patch(
            "src.services.agent.tools_impl._verify_project_ownership",
            new=AsyncMock(return_value=project),
        ),
        patch(
            "src.services.research.draft_generation_service.DraftGenerationService",
        ) as service_cls,
    ):
        service_cls.return_value = service
        service_cls.wait_for_terminal_status = AsyncMock(
            side_effect=wait_for_terminal_status
        )
        result = await _tool_create_draft(
            {"project_id": str(project.id), "themes": ["attention"]},
            db,
            user,
        )

    db.commit.assert_awaited_once_with()
    assert result["status"] == "completed"
    assert result["draft_id"] == "draft-9"
    assert result["task_id"] == "task-1"


async def test_create_draft_tool_returns_readable_terminal_failure() -> None:
    project = SimpleNamespace(id=uuid4(), name="Transformers")
    user = cast(User, SimpleNamespace(id=uuid4()))
    db, request_transaction_open = _request_session()
    service = MagicMock()

    async def generate_draft(**_: object) -> dict[str, str]:
        assert request_transaction_open()
        await db.commit()
        return {"task_id": "task-2", "status": "pending"}

    service.generate_draft = AsyncMock(side_effect=generate_draft)

    async def wait_for_terminal_status(
        task_id: str, *, timeout_seconds: float
    ) -> dict[str, str]:
        assert not request_transaction_open()
        assert task_id == "task-2"
        assert timeout_seconds == 105.0
        return {
            "task_id": "task-2",
            "status": "failed",
            "current_step": "Error: citation review blocked persistence",
        }

    with (
        patch(
            "src.services.agent.tools_impl._verify_project_ownership",
            new=AsyncMock(return_value=project),
        ),
        patch(
            "src.services.research.draft_generation_service.DraftGenerationService",
        ) as service_cls,
    ):
        service_cls.return_value = service
        service_cls.wait_for_terminal_status = AsyncMock(
            side_effect=wait_for_terminal_status
        )
        result = await _tool_create_draft(
            {"project_id": str(project.id), "themes": ["attention"]},
            db,
            user,
        )

    db.commit.assert_awaited_once_with()
    assert result["status"] == "failed"
    assert result["message"] == "citation review blocked persistence"
    assert result["error"] == "citation review blocked persistence"
    service_cls.wait_for_terminal_status.assert_awaited_once_with(
        "task-2", timeout_seconds=105.0
    )


async def test_create_draft_tool_preserves_generation_conflict_without_waiting() -> (
    None
):
    project = SimpleNamespace(id=uuid4(), name="Transformers")
    user = cast(User, SimpleNamespace(id=uuid4()))
    db, _request_transaction_open = _request_session()
    service = MagicMock()
    service.generate_draft = AsyncMock(
        return_value={
            "error": "A different draft generation is already in progress for this project.",
            "error_category": "draft_generation_conflict",
        }
    )

    with (
        patch(
            "src.services.agent.tools_impl._verify_project_ownership",
            new=AsyncMock(return_value=project),
        ),
        patch(
            "src.services.research.draft_generation_service.DraftGenerationService",
        ) as service_cls,
    ):
        service_cls.return_value = service
        service_cls.wait_for_terminal_status = AsyncMock()
        result = await _tool_create_draft(
            {"project_id": str(project.id), "themes": ["attention"]}, db, user
        )

    assert result["error_category"] == "draft_generation_conflict"
    assert "task_id" not in result
    service_cls.wait_for_terminal_status.assert_not_awaited()
