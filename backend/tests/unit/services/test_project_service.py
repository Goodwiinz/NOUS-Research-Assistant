"""Unit tests for ProjectService."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.services.research.project_service import ProjectService
from src.services.research_engine.project_access import ResearchAction
from src.shared.research_schemas import ProjectCreate, ProjectUpdate, ResearchStatus


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock(spec=AsyncSession)
    db.execute = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.delete = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_list_projects_returns_filtered_paginated_result(
    mock_db: AsyncMock,
) -> None:
    service = ProjectService(mock_db)
    workspace_id = uuid4()
    user_id = uuid4()

    count_result = MagicMock()
    count_result.scalar.return_value = 2

    project_a = MagicMock()
    project_b = MagicMock()
    projects_result = MagicMock()
    projects_result.scalars.return_value.all.return_value = [project_a, project_b]
    mappings_result = MagicMock()
    mappings_result.all.return_value = []

    mock_db.execute.side_effect = [count_result, projects_result, mappings_result]

    with patch(
        "src.services.research.project_service.accessible_research_workspace_ids",
        new=AsyncMock(return_value=[workspace_id]),
    ) as accessible:
        result = await service.list_projects(
            user_id=user_id,
            project_status="active",
            skip=0,
            limit=10,
        )

    assert result["total"] == 2
    assert result["page"] == 1
    assert result["size"] == 10
    assert len(result["projects"]) == 2
    assert result["has_next"] is False
    assert result["has_prev"] is False
    accessible.assert_awaited_once_with(mock_db, user_id)


@pytest.mark.asyncio
async def test_create_project_enforces_private_true(mock_db: AsyncMock) -> None:
    service = ProjectService(mock_db)
    workspace_id = uuid4()
    user_id = uuid4()

    count_result = MagicMock()
    count_result.scalar.return_value = 0
    mock_db.execute.return_value = count_result

    payload = ProjectCreate(
        workspace_id=workspace_id,
        name="ML Healthcare",
    )

    with patch(
        "src.services.research.project_service.require_research_workspace",
        new=AsyncMock(),
    ) as require_workspace:
        await service.create_project(user_id=user_id, project_data=payload)

    created_project = mock_db.add.call_args[0][0]
    assert created_project.name == "ML Healthcare"
    assert created_project.workspace_id == workspace_id
    assert created_project.is_private is True
    assert created_project.project_type == "research"
    assert created_project.research_status == "active"
    mock_db.commit.assert_awaited_once()
    mock_db.refresh.assert_awaited_once()
    require_workspace.assert_awaited_once_with(
        mock_db, workspace_id, user_id, ResearchAction.EDIT
    )


@pytest.mark.asyncio
async def test_create_project_rejects_workspace_quota(mock_db: AsyncMock) -> None:
    service = ProjectService(mock_db)
    workspace_id = uuid4()
    count_result = MagicMock()
    count_result.scalar.return_value = 200
    mock_db.execute.return_value = count_result

    with patch(
        "src.services.research.project_service.require_research_workspace",
        new=AsyncMock(),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await service.create_project(
                user_id=uuid4(),
                project_data=ProjectCreate(workspace_id=workspace_id, name="Too many"),
            )

    assert exc_info.value.status_code == 402
    mock_db.add.assert_not_called()
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_project_rejects_invalid_status_transition(
    mock_db: AsyncMock,
) -> None:
    service = ProjectService(mock_db)
    user_id = uuid4()
    project_id = uuid4()

    project = MagicMock()
    project.research_status = "completed"

    workspace = MagicMock(is_archived=False)
    workspace.can_user_edit.return_value = True
    workspace.can_user_admin.return_value = False
    context = SimpleNamespace(collection=project, engine=None, workspace=workspace)
    with patch(
        "src.services.research.project_service.resolve_project",
        new=AsyncMock(return_value=context),
    ) as resolve:
        with pytest.raises(HTTPException) as exc_info:
            await service.update_project(
                user_id=user_id,
                project_id=project_id,
                project_data=ProjectUpdate(research_status=ResearchStatus.ACTIVE),
            )

    assert exc_info.value.status_code == 400
    assert "Invalid status transition" in exc_info.value.detail
    mock_db.commit.assert_not_awaited()
    resolve.assert_awaited_once_with(mock_db, project_id, user_id, ResearchAction.EDIT)


@pytest.mark.asyncio
async def test_update_project_allows_valid_status_transition(
    mock_db: AsyncMock,
) -> None:
    service = ProjectService(mock_db)
    user_id = uuid4()
    project_id = uuid4()

    project = MagicMock()
    project.research_status = "active"

    workspace = MagicMock(is_archived=False)
    workspace.can_user_edit.return_value = True
    workspace.can_user_admin.return_value = False
    context = SimpleNamespace(collection=project, engine=None, workspace=workspace)
    with patch(
        "src.services.research.project_service.resolve_project",
        new=AsyncMock(return_value=context),
    ) as resolve:
        updated_project = await service.update_project(
            user_id=user_id,
            project_id=project_id,
            project_data=ProjectUpdate(research_status=ResearchStatus.PAUSED),
        )

    assert updated_project is project
    assert project.research_status == "paused"
    assert project.is_private is True
    mock_db.commit.assert_awaited_once()
    assert mock_db.refresh.await_count == 2
    assert mock_db.refresh.await_args_list[0].args == (project,)
    assert mock_db.refresh.await_args_list[0].kwargs == {
        "attribute_names": ["documents"]
    }
    assert mock_db.refresh.await_args_list[1].args == (project,)
    resolve.assert_awaited_once_with(mock_db, project_id, user_id, ResearchAction.EDIT)


@pytest.mark.asyncio
async def test_delete_project_soft_deletes_instead_of_hard_delete(
    mock_db: AsyncMock,
) -> None:
    """R2-H2: hard ``db.delete`` hits RESTRICT FKs from project_skills /

    agent_runtime_snapshots (no ORM cascade) and raises an unhandled
    IntegrityError. delete_project must flip ``is_deleted`` instead of
    calling ``db.delete`` -- which also means a project with skill/snapshot
    rows can never trip that FK, since the row is never actually removed.
    """
    from src.models.collection import Collection

    service = ProjectService(mock_db)
    user_id = uuid4()
    project_id = uuid4()

    project = Collection()
    project.is_deleted = False

    workspace = MagicMock(is_archived=False)
    workspace.can_user_edit.return_value = True
    workspace.can_user_admin.return_value = True
    context = SimpleNamespace(collection=project, engine=None, workspace=workspace)
    with patch(
        "src.services.research.project_service.resolve_project",
        new=AsyncMock(return_value=context),
    ) as resolve:
        await service.delete_project(user_id=user_id, project_id=project_id)

    assert project.is_deleted is True
    assert project.deleted_at is not None
    mock_db.delete.assert_not_awaited()
    mock_db.delete.assert_not_called()
    mock_db.commit.assert_awaited_once()
    resolve.assert_awaited_once_with(
        mock_db, project_id, user_id, ResearchAction.MANAGE
    )
