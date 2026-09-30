"""Explicitly selected project memories for a connected device.

The browser owner picks memories per consent request; a harness holding a
`context:read` grant under that consent reads only those memories, only
while they still belong to the owner's authorized project. Nothing else from
NOUS memory, prompts or state is exposed.
"""

import json
from datetime import datetime, timezone
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, or_, select
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
# Wire budget for one read, measured as UTF-8 bytes of the JSON result.
MAX_RESULT_BYTES = 64 * 1024


class ContextNotFound(Exception):
    """Opaque: missing, foreign, revoked, expired, or without context:read."""


class ContextSelectionInvalid(Exception):
    """A selected id is not one of the owner's memories in this project."""


class ContextSelectionTooLarge(Exception):
    """The selected memories would not fit in one read."""


def _identity(user: User) -> tuple[UUID, UUID]:
    if user.organization_id is None:
        raise ContextNotFound()
    return cast(UUID, user.id), cast(UUID, user.organization_id)


def _wire_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


async def _owned_consent(
    db: AsyncSession, user_id: UUID, organization_id: UUID, request_id: UUID
) -> tuple[UUID, UUID]:
    """(consent id, project id) for the owner's live context consent."""
    consent = await db.scalar(
        select(IntegrationGrantRequest)
        .where(
            IntegrationGrantRequest.id == request_id,
            IntegrationGrantRequest.user_id == user_id,
            IntegrationGrantRequest.organization_id == organization_id,
            # Approved-but-unexchanged consents may be prepared before the
            # device exchanges them, but only until they expire.
            or_(
                IntegrationGrantRequest.status == "consumed",
                and_(
                    IntegrationGrantRequest.status == "approved",
                    IntegrationGrantRequest.expires_at > datetime.now(timezone.utc),
                ),
            ),
            IntegrationGrantRequest.consent_revoked_at.is_(None),
            IntegrationGrantRequest.is_deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if consent is None or CONTEXT_SCOPE not in (consent.scopes or []):
        raise ContextNotFound()
    return cast(UUID, consent.id), cast(UUID, consent.project_id)


async def _authorized_label(
    db: AsyncSession, user_id: UUID, organization_id: UUID, project_id: UUID
) -> str:
    try:
        project = await authorized_project(db, user_id, organization_id, project_id)
    except IntegrationAccessDenied as error:
        raise ContextNotFound() from error
    return str(project.name)


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


async def _valid_selected(
    db: AsyncSession,
    ids: list[UUID],
    project_id: UUID,
    user_id: UUID,
    organization_id: UUID,
) -> list[ProjectMemory]:
    """The still-owned memories among `ids`, in selection order."""
    if not ids:
        return []
    rows = (
        await db.scalars(
            _owned_memories(project_id, user_id, organization_id).where(
                ProjectMemory.id.in_(ids)
            )
        )
    ).all()
    by_id = {m.id: m for m in rows}
    return [by_id[i] for i in ids if i in by_id]


async def _options(
    db: AsyncSession, user_id: UUID, organization_id: UUID, request_id: UUID
) -> ContextOptions:
    consent_id, project_id = await _owned_consent(
        db, user_id, organization_id, request_id
    )
    label = await _authorized_label(db, user_id, organization_id, project_id)
    selection = await _selection(db, consent_id, user_id, organization_id)
    selected = await _valid_selected(
        db,
        [UUID(i) for i in (selection.memory_ids if selection else [])],
        project_id,
        user_id,
        organization_id,
    )
    recent = (
        await db.scalars(
            _owned_memories(project_id, user_id, organization_id)
            .order_by(ProjectMemory.created_at.desc())
            .limit(MAX_OPTIONS)
        )
    ).all()
    # Selected memories are always listed, even past the newest 100, so a
    # later save can never silently drop one the user cannot see.
    listed = {m.id: m for m in recent}
    for memory in selected:
        listed.setdefault(memory.id, memory)
    return ContextOptions(
        request_id=consent_id,
        project_id=project_id,
        project_label=label,
        memories=[
            MemoryOption(
                id=m.id,
                content=str(m.content),
                source=str(m.source),
                created_at=m.created_at,
            )
            for m in listed.values()
        ],
        selected_memory_ids=[cast(UUID, m.id) for m in selected],
    )


async def context_options(
    db: AsyncSession, user: User, request_id: UUID
) -> ContextOptions:
    user_id, organization_id = _identity(user)
    return await _options(db, user_id, organization_id, request_id)


async def _write_selection(
    db: AsyncSession,
    user_id: UUID,
    organization_id: UUID,
    request_id: UUID,
    ordered: list[UUID],
) -> None:
    consent_id, project_id = await _owned_consent(
        db, user_id, organization_id, request_id
    )
    await _authorized_label(db, user_id, organization_id, project_id)
    memories = await _valid_selected(db, ordered, project_id, user_id, organization_id)
    if len(memories) != len(ordered):
        raise ContextSelectionInvalid()
    if _wire_bytes([str(m.content) for m in memories]) > MAX_RESULT_BYTES:
        raise ContextSelectionTooLarge()
    values = [str(i) for i in ordered]
    row = await _selection(db, consent_id, user_id, organization_id)
    if row is not None:
        row.memory_ids = values
    else:
        db.add(
            IntegrationContextSelection(
                organization_id=organization_id,
                user_id=user_id,
                project_id=project_id,
                consent_id=consent_id,
                memory_ids=values,
            )
        )
    await db.commit()


async def save_selection(
    db: AsyncSession, user: User, request_id: UUID, memory_ids: list[UUID]
) -> ContextOptions:
    """Replace the shared set with exactly these memories (possibly none)."""
    user_id, organization_id = _identity(user)
    ordered = list(dict.fromkeys(memory_ids))
    if len(ordered) > MAX_SELECTED_MEMORIES:
        raise ContextSelectionInvalid()
    try:
        await _write_selection(db, user_id, organization_id, request_id, ordered)
    except IntegrityError:
        # A concurrent first save inserted the row; rerun the whole validated
        # write once, now as an update. Only plain ids survive the rollback.
        await db.rollback()
        await _write_selection(db, user_id, organization_id, request_id, ordered)
    return await _options(db, user_id, organization_id, request_id)


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
    selected = await _valid_selected(
        db,
        [UUID(i) for i in selection.memory_ids],
        context.project_id,
        context.user_id,
        context.organization_id,
    )
    # Memories can grow after they were selected; share the leading ones that
    # fit and say so, rather than nothing at all.
    memories: list[dict[str, str]] = []
    truncated = False
    for memory in selected:
        item = {"memory_id": str(memory.id), "content": str(memory.content)}
        if _wire_bytes({"memories": [*memories, item]}) > MAX_RESULT_BYTES:
            truncated = True
            break
        memories.append(item)
    return ToolResult(
        content=[{"memories": memories, **({"truncated": True} if truncated else {})}],
        is_error=False,
        source_refs=[{"memory_id": m["memory_id"]} for m in memories],
    )
