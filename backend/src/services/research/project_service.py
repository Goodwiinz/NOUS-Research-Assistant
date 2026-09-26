"""Project service for research project CRUD and ownership validation."""

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from structlog import get_logger

from src.core.config import settings
from src.models import Collection, Workspace
from src.models.project_note import ProjectNote
from src.services.agent.tool_helpers import _escape_like
from src.shared.research_schemas import ProjectCreate, ProjectUpdate

logger = get_logger(__name__)


class ProjectService:
    """Business logic for research project CRUD."""

    _ALLOWED_STATUS_TRANSITIONS = {
        "active": {"paused", "completed", "archived"},
        "paused": {"active", "completed", "archived"},
        "completed": {"archived"},
        "archived": set(),
    }

    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_projects(
        self,
        user_id: UUID,
        workspace_id: Optional[UUID] = None,
        project_status: Optional[str] = None,
        project_type: Optional[str] = None,
        tag: Optional[str] = None,
        search: Optional[str] = None,
        skip: int = 0,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """List projects owned by the user with filtering."""
        workspace_ids = await self._get_workspace_ids_for_user(user_id)
        if not workspace_ids:
            return {
                "projects": [],
                "total": 0,
                "page": 1,
                "size": limit,
                "has_next": False,
                "has_prev": False,
            }

        # Soft-deleted projects are still rows in ``collections``. Every write
        # path (_verify_project_ownership, project_skills access, the RAG node)
        # filters them out, so listing them hands callers ids that are then
        # rejected as "Project not found or access denied".
        filters = [
            Collection.workspace_id.in_(workspace_ids),
            Collection.is_deleted.is_(False),
        ]
        if workspace_id:
            filters.append(Collection.workspace_id == workspace_id)
        if project_status:
            filters.append(Collection.research_status == project_status)
        if project_type:
            filters.append(Collection.project_type == project_type)
        if tag:
            filters.append(Collection.tags.contains([tag]))
        if search:
            # search is agent- and user-supplied free text; unescaped '%'/'_'
            # would act as LIKE wildcards instead of literal characters (the
            # same invariant every other ilike() site in the agent path
            # enforces via this helper — see tool_helpers.py).
            filters.append(
                Collection.name.ilike(f"%{_escape_like(search)}%", escape="\\")
            )

        count_query = select(func.count(Collection.id)).where(and_(*filters))
        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        query = (
            select(Collection)
            .where(and_(*filters))
            .options(selectinload(Collection.documents))
            .order_by(Collection.updated_at.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await self.db.execute(query)
        projects = list(result.scalars().all())

        return {
            "projects": projects,
            "total": total,
            "page": (skip // limit) + 1 if limit > 0 else 1,
            "size": limit,
            "has_next": (skip + limit) < total,
            "has_prev": skip > 0,
        }

    async def create_project(
        self,
        user_id: UUID,
        project_data: ProjectCreate,
        *,
        commit: bool = True,
    ) -> Collection:
        """Create a project after ownership validation."""
        await self._ensure_workspace_owned(project_data.workspace_id, user_id)

        # R5-L11: collections are user-created durable rows just like
        # workspaces. Cap active projects per workspace so a caller cannot mint
        # an unbounded number of project containers; soft-deleted projects do
        # not consume the quota.
        existing_count = (
            await self.db.execute(
                select(func.count(Collection.id)).where(
                    Collection.workspace_id == project_data.workspace_id,
                    Collection.is_deleted.is_(False),
                )
            )
        ).scalar() or 0
        cap = getattr(settings, "MAX_PROJECTS_PER_WORKSPACE", 200)
        if existing_count >= cap:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Project limit reached ({cap} for this workspace)",
            )

        project = Collection(
            workspace_id=project_data.workspace_id,
            name=project_data.name,
            description=project_data.description,
            project_type=project_data.project_type.value,
            research_status=project_data.research_status.value,
            research_goals=project_data.research_goals,
            deadline=project_data.deadline,
            tags=project_data.tags or [],
            color=project_data.color,
            icon=project_data.icon,
            is_private=True,  # US4: always private
        )

        self.db.add(project)
        if commit:
            await self.db.commit()
        else:
            await self.db.flush()
        await self.db.refresh(project)
        return project

    async def create_note(
        self,
        *,
        user_id: UUID,
        project_id: UUID,
        title: str,
        content: str,
        tags: Optional[List[str]] = None,
        linked_document_ids: Optional[List[Any]] = None,
        is_pinned: bool = False,
        commit: bool = True,
    ) -> ProjectNote:
        """Persist a project note and return it (refreshed).

        Single home for ``ProjectNote`` construction shared by the REST route
        (``POST /projects/{id}/notes``) and the agent ``create_project_note``
        tool — the two callers that previously each built the model
        independently (audit finding B2).

        Ownership of *project_id* MUST be verified by the caller first (the
        route via ``_get_project_with_auth``; the tool via
        ``_verify_project_ownership``). Those two guards differ deliberately —
        the tool resolves a project *name* and excludes soft-deleted projects,
        while the route takes an already-validated path UUID — so this method
        stays persistence-only to preserve each caller's exact authorization
        semantics rather than imposing a third policy.
        """
        note = ProjectNote(
            project_id=project_id,
            user_id=user_id,
            title=title,
            content=content,
            linked_document_ids=linked_document_ids or [],
            tags=tags or [],
            is_pinned=is_pinned,
        )
        self.db.add(note)
        if commit:
            await self.db.commit()
        else:
            await self.db.flush()
        await self.db.refresh(note)
        return note

    async def get_project_for_user(self, project_id: UUID, user_id: UUID) -> Collection:
        """Fetch one project owned by the user."""
        query = (
            select(Collection)
            .options(selectinload(Collection.documents))
            .join(Workspace, Collection.workspace_id == Workspace.id)
            .where(
                and_(
                    Collection.id == project_id,
                    Workspace.owner_id == user_id,
                    Collection.is_deleted.is_(False),
                )
            )
        )
        result = await self.db.execute(query)
        project = result.scalar_one_or_none()
        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found or access denied",
            )
        return project

    async def update_project(
        self,
        user_id: UUID,
        project_id: UUID,
        project_data: ProjectUpdate,
    ) -> Collection:
        """Update project fields with state-transition validation."""
        project = await self.get_project_for_user(
            project_id=project_id, user_id=user_id
        )

        if project_data.research_status is not None:
            self._validate_status_transition(
                current_status=str(project.research_status),
                target_status=str(project_data.research_status.value),
            )

        if project_data.name is not None:
            project.name = project_data.name
        if project_data.description is not None:
            project.description = project_data.description
        if project_data.project_type is not None:
            project.project_type = project_data.project_type.value
        if project_data.research_status is not None:
            project.research_status = project_data.research_status.value
        if project_data.research_goals is not None:
            project.research_goals = project_data.research_goals
        if project_data.deadline is not None:
            project.deadline = project_data.deadline
        if project_data.tags is not None:
            project.tags = project_data.tags
        if project_data.color is not None:
            project.color = project_data.color
        if project_data.icon is not None:
            project.icon = project_data.icon

        project.is_private = True  # Enforce US4 privacy rule

        await self.db.commit()
        await self.db.refresh(project)
        return project

    async def delete_project(self, user_id: UUID, project_id: UUID) -> None:
        """Soft-delete project if owned by user.

        Not a hard ``db.delete`` (R2-H2): ``project_skills.project_id`` and
        ``agent_runtime_snapshots.project_id`` are ``ForeignKey("collections.id",
        ondelete="RESTRICT")`` with no ORM cascade, so deleting a project with
        either row raised an unhandled IntegrityError -> 500. Soft delete also
        matches the ``is_deleted`` convention every read path already filters on
        (see ``get_project_for_user`` / ``list_projects``).
        """
        project = await self.get_project_for_user(
            project_id=project_id, user_id=user_id
        )
        project.soft_delete()
        await self.db.commit()

    async def _get_workspace_ids_for_user(self, user_id: UUID) -> List[UUID]:
        query = select(Workspace.id).where(Workspace.owner_id == user_id)
        result = await self.db.execute(query)
        return [row[0] for row in result.all()]

    async def _ensure_workspace_owned(self, workspace_id: UUID, user_id: UUID) -> None:
        query = select(Workspace.id).where(
            and_(
                Workspace.id == workspace_id,
                Workspace.owner_id == user_id,
            )
        )
        result = await self.db.execute(query)
        if not result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Workspace not found or access denied",
            )

    def _validate_status_transition(
        self, current_status: str, target_status: str
    ) -> None:
        """Validate project research status transitions."""
        if current_status == target_status:
            return

        allowed_targets = self._ALLOWED_STATUS_TRANSITIONS.get(current_status, set())
        if target_status not in allowed_targets:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid status transition from '{current_status}' to "
                    f"'{target_status}'"
                ),
            )
