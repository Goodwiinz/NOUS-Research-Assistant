from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.models.user import User
from src.services.agent.tools_impl import _tool_create_draft

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_create_draft_tool_returns_completed_terminal_payload() -> None:
    project = SimpleNamespace(id=uuid4(), name="Transformers")
    user = cast(User, SimpleNamespace(id=uuid4()))
    service = MagicMock()
    service.generate_draft = AsyncMock(
        return_value={"task_id": "task-1", "status": "pending"}
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
        service_cls.wait_for_terminal_status = AsyncMock(
            return_value={
                "task_id": "task-1",
                "status": "completed",
                "draft_id": "draft-9",
                "progress": 100,
            }
        )
        result = await _tool_create_draft(
            {"project_id": str(project.id), "themes": ["attention"]},
            MagicMock(),
            user,
        )

    assert result["status"] == "completed"
    assert result["draft_id"] == "draft-9"
    assert result["task_id"] == "task-1"


async def test_create_draft_tool_returns_readable_terminal_failure() -> None:
    project = SimpleNamespace(id=uuid4(), name="Transformers")
    user = cast(User, SimpleNamespace(id=uuid4()))
    service = MagicMock()
    service.generate_draft = AsyncMock(
        return_value={"task_id": "task-2", "status": "pending"}
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
        service_cls.wait_for_terminal_status = AsyncMock(
            return_value={
                "task_id": "task-2",
                "status": "failed",
                "current_step": "Error: citation review blocked persistence",
            }
        )
        result = await _tool_create_draft(
            {"project_id": str(project.id), "themes": ["attention"]},
            MagicMock(),
            user,
        )

    assert result["status"] == "failed"
    assert result["message"] == "citation review blocked persistence"
    assert result["error"] == "citation review blocked persistence"
    service_cls.wait_for_terminal_status.assert_awaited_once_with(
        "task-2", timeout_seconds=105.0
    )
