"""Explicitly selected project memories for a connected device.

The browser owner picks memories per consent request; a harness holding a
`context:read` grant under that consent reads only those memories, only
while they still belong to the owner's authorized project. Nothing else from
NOUS memory, prompts or state is exposed.
"""

import json
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.integration_context_selection import IntegrationContextSelection
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.project_memory import ProjectMemory
from src.models.user import User
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_selected_context import (
    MAX_SELECTED_MEMORIES,
    ContextOptions,
    MemoryOption,
)
from src.schemas.integration_tools import ToolResult
from src.services.integrations.context import (
    IntegrationAccessDenied,
    authorized_project,
)

CONTEXT_SCOPE = "context:read"
MAX_OPTIONS = 100
MAX_RESULT_BYTES = 64 * 1024


class ContextNotFound(Exception):
    """Opaque: missing, foreign, revoked, or without context:read."""


class ContextSelectionInvalid(Exception):
    """A selected id is not one of the owner's memories in this project."""


def _identity(user: User) -> tuple[UUID, UUID]:
    if user.organization_id is None:
        raise ContextNotFound()
    return cast(UUID, user.id), cast(UUID, user.organization_id)


async def _owned_consent(
    db: AsyncSession, user_id: UUID, organization_id: UUID, request_id: UUID
) -> IntegrationGrantRequest:
    consent = await db.scalar(
        select(IntegrationGrantRequest)
        .where(
            IntegrationGrantRequest.id == request_id,
            IntegrationGrantRequest.user_id == user_id,
            IntegrationGrantRequest.organization_id == organization_id,
            IntegrationGrantRequest.status.in_(("approved", "consumed")),
            IntegrationGrantRequest.consent_revoked_at.is_(None),
            IntegrationGrantRequest.is_deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if consent is None or CONTEXT_SCOPE not in (consent.scopes or []):
        raise ContextNotFound()
    return cast(IntegrationGrantRequest, consent)


async def _selection(
    db: AsyncSession, consent_id: UUID, user_id: UUID, organization_id: UUID
) -> IntegrationContextSelection | None:
    row = await db.scalar(
        select(IntegrationContextSelection)
        .where(
            IntegrationContextSelection.consent_id == consent_id,
            IntegrationContextSelection.user_id == user_id,
            IntegrationContextSelection.organization_id == organization_id,
            IntegrationContextSelection.is_deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    return cast(IntegrationContextSelection | None, row)


def _owned_memories(project_id: UUID, user_id: UUID, organization_id: UUID) -> Any:
    return (
        select(ProjectMemory)
        .join(User, User.id == ProjectMemory.user_id)
        .where(
            ProjectMemory.project_id == project_id,
            ProjectMemory.user_id == user_id,
            ProjectMemory.is_deleted.is_(False),
            User.organization_id == organization_id,
        )
    )


async def context_options(
    db: AsyncSession, user: User, request_id: UUID
) -> ContextOptions:
    user_id, organization_id = _identity(user)
    consent = await _owned_consent(db, user_id, organization_id, request_id)
    try:
        project = await authorized_project(
            db, user_id, organization_id, consent.project_id
        )
    except IntegrationAccessDenied as error:
        raise ContextNotFound() from error
    memories = (
        await db.scalars(
            _owned_memories(consent.project_id, user_id, organization_id)
            .order_by(ProjectMemory.created_at.desc())
            .limit(MAX_OPTIONS)
        )
    ).all()
    selection = await _selection(db, consent.id, user_id, organization_id)
    return ContextOptions(
        request_id=consent.id,
        project_id=consent.project_id,
        project_label=str(project.name),
        memories=[
            MemoryOption(
                id=m.id,
                content=str(m.content),
                source=str(m.source),
                created_at=m.created_at,
            )
            for m in memories
        ],
        selected_memory_ids=[
            UUID(i) for i in (selection.memory_ids if selection else [])
        ],
    )


async def save_selection(
    db: AsyncSession, user: User, request_id: UUID, memory_ids: list[UUID]
) -> ContextOptions:
    """Replace the shared set with exactly these memories (possibly none)."""
    user_id, organization_id = _identity(user)
    ordered = list(dict.fromkeys(memory_ids))
    if len(ordered) > MAX_SELECTED_MEMORIES:
        raise ContextSelectionInvalid()
    consent = await _owned_consent(db, user_id, organization_id, request_id)
    try:
        await authorized_project(db, user_id, organization_id, consent.project_id)
    except IntegrationAccessDenied as error:
        raise ContextNotFound() from error
    if ordered:
        found = set(
            (
                await db.scalars(
                    _owned_memories(consent.project_id, user_id, organization_id)
                    .with_only_columns(ProjectMemory.id)
                    .where(ProjectMemory.id.in_(ordered))
                )
            ).all()
        )
        if found != set(ordered):
            raise ContextSelectionInvalid()
    values = [str(i) for i in ordered]
    row = await _selection(db, consent.id, user_id, organization_id)
    if row is not None:
        row.memory_ids = values
    else:
        db.add(
            IntegrationContextSelection(
                organization_id=organization_id,
                user_id=user_id,
                project_id=consent.project_id,
                consent_id=consent.id,
                memory_ids=values,
            )
        )
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent first save won; apply this one on top of it.
        await db.rollback()
        row = await _selection(db, consent.id, user_id, organization_id)
        if row is None:
            raise
        row.memory_ids = values
        await db.commit()
    return await context_options(db, user, request_id)


def _unavailable(code: str) -> ToolResult:
    return ToolResult(content=[{"error": code}], is_error=True, source_refs=[])


async def read_selected_context(
    db: AsyncSession, context: IntegrationContext
) -> ToolResult:
    """Only the selected memories that still belong to the authorized project."""
    grant = await db.get(IntegrationGrant, context.grant_id)
    if grant is None or grant.request_id is None:
        # Internal grants have no browser consent to select context under.
        return _unavailable("no_context_selection")
    try:
        await authorized_project(
            db, context.user_id, context.organization_id, context.project_id
        )
    except IntegrationAccessDenied:
        return _unavailable("context_unavailable")
    selection = await _selection(
        db, grant.request_id, context.user_id, context.organization_id
    )
    if selection is None or selection.project_id != context.project_id:
        return ToolResult(content=[{"memories": []}], is_error=False, source_refs=[])
    wanted = [UUID(i) for i in selection.memory_ids]
    rows = (
        (
            await db.scalars(
                _owned_memories(
                    context.project_id, context.user_id, context.organization_id
                ).where(ProjectMemory.id.in_(wanted))
            )
        ).all()
        if wanted
        else []
    )
    by_id = {m.id: m for m in rows}
    memories = [
        {"memory_id": str(i), "content": str(by_id[i].content)}
        for i in wanted
        if i in by_id
    ]
    result = ToolResult(
        content=[{"memories": memories}],
        is_error=False,
        source_refs=[{"memory_id": m["memory_id"]} for m in memories],
    )
    if len(json.dumps(result.model_dump(mode="json")).encode()) > MAX_RESULT_BYTES:
        return _unavailable("result_too_large")
    return result
