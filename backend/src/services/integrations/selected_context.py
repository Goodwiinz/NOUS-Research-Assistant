"""Explicitly selected project memories for a connected device.

The browser owner picks memories per consent request; a harness holding a
`context:read` grant under that consent reads only those memories, only
while they still belong to the owner's authorized project. Nothing else from
NOUS memory, prompts or state is exposed.
"""

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.agent_runtime_snapshot import AgentRuntimeSnapshot
from src.models.integration_context_selection import IntegrationContextSelection
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.project_memory import ProjectMemory
from src.models.project_skill import (
    ProjectSkill,
    ProjectSkillVersion,
    ProjectSkillVersionScan,
)
from src.models.user import User
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_selected_context import (
    MAX_SELECTED_MEMORIES,
    ContextOptions,
    MemoryOption,
    SkillOption,
)
from src.schemas.integration_tools import ToolResult
from src.services.integrations.context import (
    IntegrationAccessDenied,
    _validate_grant,
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
    db: AsyncSession,
    user_id: UUID,
    organization_id: UUID,
    request_id: UUID,
    *,
    lock: bool = False,
) -> tuple[UUID, UUID]:
    """(consent id, project id) for the owner's live context consent."""
    query = (
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
    if lock:
        query = query.with_for_update()
    consent = await db.scalar(query)
    # A workspace consent has no project to pick memories from. check_scopes
    # keeps context:read off it; a row that got in another way is refused here.
    if (
        consent is None
        or consent.project_id is None
        or CONTEXT_SCOPE not in (consent.scopes or [])
    ):
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


async def _skill_catalog(db: AsyncSession, project_id: UUID) -> list[dict[str, Any]]:
    if not (
        settings.PROJECT_SKILL_CATALOG_ENABLED
        and settings.PROJECT_SKILL_RUNTIME_ENABLED
    ):
        return []
    from src.services.agent.runtime_snapshot import _eligible_catalog

    return await _eligible_catalog(db, project_id=project_id)


def _skill_option(item: dict[str, Any]) -> SkillOption:
    return SkillOption(
        version_id=UUID(item["version_id"]),
        name=item["name"],
        description=str(item["description"])[:240],
        version=item["version"],
        content_hash=item["content_hash"],
    )


async def _skill_snapshot(
    db: AsyncSession,
    selection: IntegrationContextSelection | None,
    *,
    include_expired: bool = False,
) -> Any:
    from src.services.agent.runtime_snapshot import _snapshot_has_frozen_loader

    if (
        selection is None
        or not selection.skill_version_ids
        or not selection.runtime_snapshot_id
    ):
        return None
    snapshot: Any = await db.get(
        AgentRuntimeSnapshot, selection.runtime_snapshot_id, populate_existing=True
    )
    if (
        snapshot is None
        or snapshot.is_deleted
        or snapshot.user_id != selection.user_id
        or snapshot.project_id != selection.project_id
        or (
            not include_expired
            and snapshot.expires_at.replace(tzinfo=timezone.utc)
            <= datetime.now(timezone.utc)
        )
        or not isinstance(snapshot.skill_catalog, list)
        or len(snapshot.skill_catalog) > 32
        or not all(isinstance(v, dict) for v in snapshot.skill_catalog)
        or {v.get("version_id") for v in snapshot.skill_catalog}
        != set(selection.skill_version_ids)
        or (not include_expired and not settings.PROJECT_SKILL_RUNTIME_ENABLED)
        or (not include_expired and not _snapshot_has_frozen_loader(snapshot))
    ):
        return None
    try:
        for item in snapshot.skill_catalog:
            _skill_option(item)
    except (KeyError, ValueError, TypeError):
        return None
    if not include_expired:
        # A frozen catalog preserves version choice, not authority to read a
        # deleted version/skill. Historical metadata remains usable only for
        # the browser's explicit reselection path below.
        live_ids = (
            await db.scalars(
                select(ProjectSkillVersion.id)
                .join(ProjectSkill, ProjectSkill.id == ProjectSkillVersion.skill_id)
                .where(
                    ProjectSkillVersion.id.in_(
                        [UUID(value) for value in selection.skill_version_ids]
                    ),
                    ProjectSkillVersion.is_deleted.is_(False),
                    ProjectSkill.is_deleted.is_(False),
                    ProjectSkill.project_id == selection.project_id,
                )
            )
        ).all()
        if {str(value) for value in live_ids} != set(selection.skill_version_ids):
            return None
    return snapshot


async def _retained_skill_catalog(
    db: AsyncSession, row: IntegrationContextSelection, chosen: list[str]
) -> list[dict[str, Any]]:
    """Retain frozen choices only while their immutable versions still pass."""
    previous = await _skill_snapshot(db, row, include_expired=True)
    if previous is None:
        return []
    retained: list[dict[str, Any]] = []
    for item in previous.skill_catalog:
        if item["version_id"] not in chosen:
            continue
        version: Any = await db.get(
            ProjectSkillVersion, UUID(item["version_id"]), populate_existing=True
        )
        skill: Any = (
            await db.get(ProjectSkill, version.skill_id, populate_existing=True)
            if version
            else None
        )
        scan: Any = await db.scalar(
            select(ProjectSkillVersionScan)
            .where(ProjectSkillVersionScan.version_id == UUID(item["version_id"]))
            .order_by(
                ProjectSkillVersionScan.created_at.desc(),
                ProjectSkillVersionScan.id.desc(),
            )
            .limit(1)
        )
        if (
            version is None
            or version.is_deleted
            or skill is None
            or skill.is_deleted
            or skill.project_id != row.project_id
            or skill.normalized_name != item["name"]
            or version.parsed_name != item["name"]
            or version.version != item["version"]
            or version.content_hash != item["content_hash"]
            or sha256(version.instructions.encode("utf-8")).hexdigest()
            != item["content_hash"]
            or scan is None
            or scan.scan_state != "passed"
        ):
            raise ContextSelectionInvalid()
        retained.append(item)
    return retained


async def _select_skills(
    db: AsyncSession,
    row: IntegrationContextSelection,
    ids: list[UUID] | None,
    refresh: bool,
) -> None:
    if ids is None:
        if refresh:
            raise ContextSelectionInvalid()
        return
    chosen = list(dict.fromkeys(str(value) for value in ids))
    if len(chosen) > 32:
        raise ContextSelectionInvalid()
    if set(chosen) == set(row.skill_version_ids) and not refresh:
        return
    if not chosen:
        row.skill_version_ids = []
        row.runtime_snapshot_id = None
        return
    if (
        refresh
        and set(chosen) == set(row.skill_version_ids)
        and await _skill_snapshot(db, row) is not None
    ):
        raise ContextSelectionInvalid()
    from src.services.agent.runtime_snapshot import create_runtime_snapshot

    try:
        snapshot = await create_runtime_snapshot(
            db,
            user_id=row.user_id,
            project_id=row.project_id,
            selected_skill_version_ids=[UUID(value) for value in chosen],
            retained_skill_catalog=await _retained_skill_catalog(db, row, chosen),
            commit=False,
        )
    except ValueError as error:
        raise ContextSelectionInvalid() from error
    if snapshot.id is None:
        raise ContextSelectionInvalid()
    row.skill_version_ids = chosen
    row.runtime_snapshot_id = UUID(snapshot.id)


async def _context_selection(
    db: AsyncSession, context: IntegrationContext, *, lock: bool = False
) -> IntegrationContextSelection | None:
    grant = await db.get(IntegrationGrant, context.grant_id, populate_existing=True)
    try:
        grant = await _validate_grant(db, grant)
        if (
            grant.request_id is None
            or context.project_id is None
            or grant.user_id != context.user_id
            or grant.organization_id != context.organization_id
            or grant.project_id != context.project_id
            or CONTEXT_SCOPE not in grant.scopes
        ):
            raise IntegrationAccessDenied()
        consent_id, project_id = await _owned_consent(
            db, context.user_id, context.organization_id, grant.request_id, lock=lock
        )
        if project_id != context.project_id:
            raise IntegrationAccessDenied()
        await authorized_project(
            db, context.user_id, context.organization_id, project_id
        )
        return await _selection(
            db, consent_id, context.user_id, context.organization_id
        )
    except (IntegrationAccessDenied, ContextNotFound) as error:
        raise ContextNotFound() from error


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
    catalog = await _skill_catalog(db, project_id)
    snapshot = await _skill_snapshot(db, selection)
    frozen = await _skill_snapshot(db, selection, include_expired=True)
    # Preserve frozen choices in the browser even after live activation changes.
    listed_skills = {item["version_id"]: item for item in catalog}
    if frozen is not None:
        for item in frozen.skill_catalog:
            listed_skills.setdefault(item["version_id"], item)
    for value in selection.skill_version_ids if selection else []:
        listed_skills.setdefault(
            value,
            {
                "version_id": value,
                "name": "Unavailable selected skill",
                "description": "Select an available version to refresh.",
                "version": 0,
                "content_hash": "",
            },
        )
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
        skills=[_skill_option(item) for item in listed_skills.values()],
        selected_skill_version_ids=[
            UUID(value) for value in (selection.skill_version_ids if selection else [])
        ],
        skill_snapshot_status=(
            "ready"
            if snapshot is not None
            else "unavailable" if selection and selection.skill_version_ids else "none"
        ),
        skill_snapshot_expires_at=frozen.expires_at if frozen else None,
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
    skill_version_ids: list[UUID] | None,
    refresh_skills: bool,
) -> None:
    consent_id, project_id = await _owned_consent(
        db, user_id, organization_id, request_id, lock=True
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
        row = IntegrationContextSelection(
            organization_id=organization_id,
            user_id=user_id,
            project_id=project_id,
            consent_id=consent_id,
            memory_ids=values,
            skill_version_ids=[],
        )
        db.add(row)
    await _select_skills(db, row, skill_version_ids, refresh_skills)
    await db.commit()


async def save_selection(
    db: AsyncSession,
    user: User,
    request_id: UUID,
    memory_ids: list[UUID],
    *,
    skill_version_ids: list[UUID] | None = None,
    refresh_skills: bool = False,
) -> ContextOptions:
    """Replace the shared set with exactly these memories (possibly none)."""
    user_id, organization_id = _identity(user)
    ordered = list(dict.fromkeys(memory_ids))
    if len(ordered) > MAX_SELECTED_MEMORIES:
        raise ContextSelectionInvalid()
    try:
        await _write_selection(
            db,
            user_id,
            organization_id,
            request_id,
            ordered,
            skill_version_ids,
            refresh_skills,
        )
    except IntegrityError:
        # A concurrent first save inserted the row; rerun the whole validated
        # write once, now as an update. Only plain ids survive the rollback.
        await db.rollback()
        await _write_selection(
            db,
            user_id,
            organization_id,
            request_id,
            ordered,
            skill_version_ids,
            refresh_skills,
        )
    except Exception:
        await db.rollback()
        raise
    return await _options(db, user_id, organization_id, request_id)


def _unavailable(code: str) -> ToolResult:
    return ToolResult(content=[{"error": code}], is_error=True, source_refs=[])


async def read_selected_context(
    db: AsyncSession, context: IntegrationContext
) -> ToolResult:
    """Only the selected memories that still belong to the authorized project."""
    # The project comes from the grant and never from the caller. A workspace
    # grant has none (and cannot hold context:read), so it reads nothing, and
    # the answer does not depend on which grant row it names.
    project_id = context.project_id
    if project_id is None:
        return _unavailable("context_unavailable")
    grant = await db.get(IntegrationGrant, context.grant_id)
    if grant is None or grant.request_id is None:
        # Internal grants have no browser consent to select context under.
        return _unavailable("no_context_selection")
    try:
        await authorized_project(
            db, context.user_id, context.organization_id, project_id
        )
    except IntegrationAccessDenied:
        return _unavailable("context_unavailable")
    selection = await _selection(
        db, grant.request_id, context.user_id, context.organization_id
    )
    if selection is None or selection.project_id != project_id:
        return ToolResult(content=[{"memories": []}], is_error=False, source_refs=[])
    try:
        selection = await _context_selection(db, context)
    except ContextNotFound:
        return _unavailable("context_unavailable")
    if selection is None:
        return _unavailable("context_unavailable")
    snapshot = await _skill_snapshot(db, selection)
    if selection.skill_version_ids and snapshot is None:
        return _unavailable("skill_snapshot_unavailable")
    skills = (
        [_skill_option(item).model_dump(mode="json") for item in snapshot.skill_catalog]
        if snapshot is not None
        else []
    )
    selected = await _valid_selected(
        db,
        [UUID(i) for i in selection.memory_ids],
        project_id,
        context.user_id,
        context.organization_id,
    )
    # Memories can grow after they were selected; share the leading ones that
    # fit and say so, rather than nothing at all.
    memories: list[dict[str, str]] = []
    truncated = False
    for memory in selected:
        item = {"memory_id": str(memory.id), "content": str(memory.content)}
        if (
            _wire_bytes(
                {
                    "memories": [*memories, item],
                    **({"skills": skills} if skills else {}),
                }
            )
            > MAX_RESULT_BYTES
        ):
            truncated = True
            break
        memories.append(item)

    def outcome() -> ToolResult:
        return ToolResult(
            content=[
                {
                    "memories": memories,
                    **({"skills": skills} if skills else {}),
                    **({"truncated": True} if truncated else {}),
                }
            ],
            is_error=False,
            source_refs=[{"memory_id": m["memory_id"]} for m in memories]
            + [
                {"version_id": item["version_id"], "content_hash": item["content_hash"]}
                for item in skills
            ],
        )

    result = outcome()
    while _wire_bytes(result.model_dump(mode="json")) > MAX_RESULT_BYTES:
        if not memories:
            return _unavailable("context_result_too_large")
        memories.pop()
        truncated = True
        result = outcome()
    return result


async def load_selected_skill(
    db: AsyncSession, context: IntegrationContext, skill_name: str
) -> ToolResult:
    """Load only a browser-frozen version, recording a consent-wide bounded receipt."""
    try:
        selection = await _context_selection(db, context, lock=True)
        snapshot = await _skill_snapshot(db, selection)
        if snapshot is None:
            await db.rollback()
            return _unavailable("skill_snapshot_unavailable")
        from src.services.agent.runtime_snapshot import load_project_skill_from_snapshot

        result = await load_project_skill_from_snapshot(
            db,
            snapshot_id=str(snapshot.id),
            user_id=context.user_id,
            project_id=context.project_id,
            skill_name=skill_name,
            commit=False,
        )
        if "error_type" in result:
            await db.rollback()
            return _unavailable(result["error_type"])
        receipt = result["loaded_skill_version"]
        content = {
            key: result[key]
            for key in ("name", "version", "content_hash", "instructions")
        }
        content["version_id"] = receipt["version_id"]
        outcome = ToolResult(
            content=[content],
            is_error=False,
            source_refs=[
                {
                    "version_id": receipt["version_id"],
                    "content_hash": receipt["content_hash"],
                }
            ],
        )
        if _wire_bytes(outcome.model_dump(mode="json")) > MAX_RESULT_BYTES:
            await db.rollback()
            return _unavailable("skill_result_too_large")
        await db.commit()
        return outcome
    except ContextNotFound:
        await db.rollback()
        return _unavailable("context_unavailable")
    except Exception:
        await db.rollback()
        raise
