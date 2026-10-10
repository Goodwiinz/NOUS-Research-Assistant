"""Durable, transport-neutral inputs for one agent turn.

Project skills are deliberately unavailable unless their catalog has first
been persisted in an :class:`AgentRuntimeSnapshot`.  This prevents a failed
write from accidentally making an in-memory (and therefore non-resumable)
skill version available to a streaming or queued graph.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.config import get_settings
from src.models import (
    AgentRuntimeSnapshot,
    ProjectSkill,
    ProjectSkillVersion,
    ProjectSkillVersionScan,
)
from src.services.agent.observability import record_project_skill_event
from src.services.agent.tools import TOOL_REGISTRY
from src.services.project_skills.access import (
    ProjectSkillNotFound,
    get_authorized_project,
)

logger = logging.getLogger(__name__)

MAX_ACTIVE_PROJECT_SKILLS = 32
MAX_LOADED_PROJECT_SKILLS = 3
MAX_LOADED_PROJECT_SKILL_TOKENS = 12_000


@dataclass(frozen=True)
class RuntimeSnapshot:
    """The safe state-facing projection of a persisted runtime snapshot."""

    id: str | None
    tool_registry_hash: str
    tool_registry_version: str
    tool_names: tuple[str, ...]
    project_skill_catalog: tuple[dict[str, Any], ...]
    expires_at: datetime | None


def turn_reset_fields() -> dict[str, Any]:
    """Per-turn resets shared by the streaming and queued ``initial_state``.

    Every key here overwrites the checkpoint's last value when the turn input
    is applied, so only state that must NOT carry across turns belongs here.
    Never add cross-turn state (``turn_index``, ``identity_ledger``, ...):
    seeding it clobbers the checkpoint every turn (R8-A3).
    ``preprocessing_node`` keeps its own in-graph resets for callers that
    bypass these builders (``langgraph.json``, evals).
    """
    return {
        "retrieved_contexts": [],
        "attachment_status": [],
        "tool_executions": [],
        "tool_loop_count": 0,
        "error_count": 0,
        "last_error": "",
        "pending_confirmation": {},
        "user_confirmed": False,
        "intent": "",
        "user_memories": [],
        "plan": [],
        "plan_reasoning": "",
        "reflection_count": 0,
        "compaction_count": 0,
        "intent_confidence": 0.0,
        "last_error_info": {},
    }


def runtime_state_fields(
    snapshot: RuntimeSnapshot, project_id: str | None
) -> dict[str, Any]:
    """Single initial-state projection used by streaming and queued turns."""
    return {
        "current_project_id": str(project_id or ""),
        "runtime_snapshot_id": snapshot.id or "",
        "runtime_tool_names": list(snapshot.tool_names),
        "tool_registry_hash": snapshot.tool_registry_hash,
        "tool_registry_version": snapshot.tool_registry_version,
        "runtime_projection_unavailable": False,
        "project_skill_catalog": list(snapshot.project_skill_catalog),
        "loaded_skill_versions": [],
        "capability_limitation": {},
    }


def runtime_config_fields(
    snapshot_id: str | None, project_id: str | None
) -> dict[str, str]:
    """Server-owned configurable identifiers for every tool invocation."""
    return {
        "project_id": str(project_id or ""),
        "runtime_snapshot_id": str(snapshot_id or ""),
    }


def resume_runtime_config_fields(values: dict[str, Any]) -> dict[str, str]:
    """Recover immutable runtime context from checkpoint state for HITL resume."""
    page_context = values.get("page_context") or {}
    return runtime_config_fields(
        values.get("runtime_snapshot_id"),
        values.get("current_project_id") or page_context.get("project_id"),
    )


def empty_runtime_snapshot() -> RuntimeSnapshot:
    """Return the ordinary-tools-only fallback without a durable skill catalog."""
    metadata = TOOL_REGISTRY.metadata_snapshot()
    return RuntimeSnapshot(
        id=None,
        tool_registry_hash=metadata["hash"],
        tool_registry_version=metadata["version"],
        tool_names=TOOL_REGISTRY.available_descriptor_names(),
        project_skill_catalog=(),
        expires_at=None,
    )


def _as_uuid(value: UUID | str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


async def _eligible_catalog(
    session: AsyncSession, *, project_id: UUID
) -> list[dict[str, Any]]:
    """Build a deterministic catalog from active versions with a latest passed scan.

    Scans are append-only.  Eligibility is therefore based on the latest scan
    row (ordered by created timestamp and id), rather than on any historical
    passed result that a later blocked/error scan could otherwise bypass.
    """
    skills = (
        await session.scalars(
            select(ProjectSkill)
            .where(
                ProjectSkill.project_id == project_id,
                ProjectSkill.active_version_id.is_not(None),
                ProjectSkill.is_archived.is_(False),
                ProjectSkill.is_deleted.is_(False),
            )
            .order_by(ProjectSkill.normalized_name.asc(), ProjectSkill.id.asc())
            .limit(MAX_ACTIVE_PROJECT_SKILLS)
            .options(selectinload(ProjectSkill.active_version))
        )
    ).all()

    catalog: list[dict[str, Any]] = []
    # Keep a defense-in-depth cap even when a mocked/nonstandard session
    # ignores the SQL LIMIT above.
    for skill in skills[:MAX_ACTIVE_PROJECT_SKILLS]:
        version = skill.active_version
        if version is None:
            continue
        latest_scan = await session.scalar(
            select(ProjectSkillVersionScan)
            .where(ProjectSkillVersionScan.version_id == version.id)
            .order_by(
                ProjectSkillVersionScan.created_at.desc(),
                ProjectSkillVersionScan.id.desc(),
            )
            .limit(1)
        )
        if latest_scan is None or latest_scan.scan_state != "passed":
            continue
        catalog.append(
            {
                # This is persisted only.  The state/prompt projection below
                # excludes it, so the model never receives a database id.
                "version_id": str(version.id),
                "name": skill.normalized_name,
                "description": version.description,
                "version": version.version,
                "content_hash": version.content_hash,
            }
        )
    return catalog


def _state_catalog(catalog: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Return only model-safe, compact catalog metadata in stable order."""
    return tuple(
        {
            "name": item["name"],
            "description": item["description"],
            "version": item["version"],
            "content_hash": item["content_hash"],
        }
        for item in catalog
    )


def render_project_skill_catalog(
    catalog: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> str:
    """Render only frozen, compact catalog metadata for an LLM system prompt."""
    entries = sorted(catalog or (), key=lambda item: item.get("name", ""))
    if not entries:
        return ""
    lines = ["Project skills available for this turn (load only when relevant):"]
    for item in entries[:MAX_ACTIVE_PROJECT_SKILLS]:
        description = " ".join(str(item.get("description", "")).split())[:240]
        lines.append(
            f"- {item.get('name', '')} (v{item.get('version', '')}): " f"{description}"
        )
    lines.append(
        "Call load_project_skill(skill_name) to read the exact instructions for one listed skill."
    )
    lines.append(
        "When the user explicitly names a listed skill, call "
        "load_project_skill(skill_name) before any other project tool."
    )
    return "\n".join(lines)


async def create_runtime_snapshot(
    session: AsyncSession,
    *,
    user_id: UUID | str,
    project_id: UUID | str | None,
    thread_id: UUID | str | None = None,
    job_id: str | None = None,
) -> RuntimeSnapshot:
    """Persist and return a frozen catalog, or the safe empty fallback.

    A missing/invalid/unauthorized project, disabled rollout flags, or any
    database persistence failure all degrade to ordinary tools.  In no case
    does this function return an in-memory skill catalog that was not first
    committed to the durable snapshot row.
    """
    settings = get_settings()
    skill_runtime = (
        settings.PROJECT_SKILL_CATALOG_ENABLED
        and settings.PROJECT_SKILL_RUNTIME_ENABLED
    )
    if not skill_runtime and not getattr(
        settings, "AGENT_TOOL_REGISTRY_ENFORCEMENT_ENABLED", False
    ):
        record_project_skill_event("snapshot", "skipped")
        return empty_runtime_snapshot()

    actor_id = _as_uuid(user_id)
    scoped_project_id = _as_uuid(project_id)
    if actor_id is None:
        record_project_skill_event("snapshot", "rejected")
        return empty_runtime_snapshot()

    if not skill_runtime and scoped_project_id is None:
        scoped_project_id = None
    elif scoped_project_id is None:
        record_project_skill_event("snapshot", "rejected")
        return empty_runtime_snapshot()

    try:
        if scoped_project_id is not None:
            await get_authorized_project(
                session,
                project_id=scoped_project_id,
                user_id=actor_id,
                capability="read",
            )
    except (ProjectSkillNotFound, PermissionError, ValueError):
        record_project_skill_event("snapshot", "rejected")
        return empty_runtime_snapshot()

    metadata = TOOL_REGISTRY.metadata_snapshot()
    try:
        catalog = (
            await _eligible_catalog(session, project_id=scoped_project_id)
            if skill_runtime and scoped_project_id is not None
            else []
        )
        conditions = {"project_skill_catalog"} if skill_runtime and catalog else set()
        tool_names = TOOL_REGISTRY.available_descriptor_names(conditions=conditions)
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=max(1, settings.PROJECT_SKILL_SNAPSHOT_RETENTION_DAYS)
        )
        row = AgentRuntimeSnapshot(
            project_id=scoped_project_id,
            user_id=actor_id,
            thread_id=_as_uuid(thread_id),
            job_id=job_id,
            tool_registry_hash=metadata["hash"],
            tool_registry_version=metadata["version"],
            tool_metadata={
                "descriptors": TOOL_REGISTRY.frozen_descriptor_metadata(
                    conditions=conditions
                )
            },
            skill_catalog=catalog,
            loaded_skill_versions=[],
            expires_at=expires_at,
        )
        session.add(row)
        await session.commit()
    except Exception:
        # A catalog that never reached durable storage must never be supplied
        # to the graph; queued and HITL paths would be unable to reproduce it.
        await session.rollback()
        logger.warning(
            "runtime snapshot creation failed; continuing without skills", exc_info=True
        )
        record_project_skill_event("snapshot", "failure")
        return empty_runtime_snapshot()

    record_project_skill_event("snapshot", "success")
    return RuntimeSnapshot(
        id=str(row.id),
        tool_registry_hash=metadata["hash"],
        tool_registry_version=metadata["version"],
        tool_names=tool_names,
        project_skill_catalog=_state_catalog(catalog),
        expires_at=expires_at,
    )


async def hydrate_runtime_state_from_snapshot(
    session: AsyncSession,
    values: dict[str, Any],
    *,
    user_id: UUID | str,
    thread_id: UUID | str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Hydrate a pre-projection checkpoint from its authorized durable row.

    Only state that predates ``runtime_tool_names`` uses this path. Snapshot
    metadata is compared with the current code-owned registry; no live project
    skill pointers or caller-provided tool names are consulted.
    """
    snapshot_id = str(values.get("runtime_snapshot_id") or "")
    if not snapshot_id or (
        isinstance(values.get("runtime_tool_names"), list)
        and values.get("tool_registry_hash")
        and values.get("tool_registry_version")
    ):
        return {}

    actor_id = _as_uuid(user_id)
    snapshot_uuid = _as_uuid(snapshot_id)
    current_project_id = _as_uuid(
        values.get("current_project_id")
        or (values.get("page_context") or {}).get("project_id")
    )
    expected_thread_id = _as_uuid(thread_id or values.get("thread_id"))
    expected_job_id = str(job_id or values.get("job_id") or "")
    failure = {
        "runtime_tool_names": [],
        "runtime_projection_unavailable": True,
        "project_skill_catalog": [],
        "capability_limitation": {
            "branch": "runtime",
            "unavailable_tools": [],
            "reason": "The saved tool runtime for this confirmation is unavailable.",
        },
    }
    if actor_id is None or snapshot_uuid is None:
        return failure

    snapshot = await session.get(AgentRuntimeSnapshot, snapshot_uuid)
    now = datetime.now(timezone.utc)
    if (
        snapshot is None
        or snapshot.user_id != actor_id
        or (
            snapshot.expires_at is not None
            and snapshot.expires_at.replace(tzinfo=timezone.utc) <= now
        )
        or snapshot.project_id != current_project_id
    ):
        return failure

    row_thread_id = _as_uuid(snapshot.thread_id)
    row_job_id = str(snapshot.job_id or "")
    identity_matches = False
    if expected_thread_id is not None and row_thread_id is not None:
        if row_thread_id != expected_thread_id:
            return failure
        identity_matches = True
    if expected_job_id and row_job_id:
        if row_job_id != expected_job_id:
            return failure
        identity_matches = True
    if (
        not identity_matches
        and row_thread_id is None
        and not row_job_id
        and values.get("thread_persistence") == "ephemeral"
        and not values.get("thread_id")
        and expected_thread_id is None
        and not expected_job_id
    ):
        identity_matches = True
    # A threadless durable run must be anchored by its exact job id. A
    # thread-bound snapshot must match the thread if no matching job is given.
    if not identity_matches:
        return failure

    metadata = TOOL_REGISTRY.metadata_snapshot()
    tool_metadata = snapshot.tool_metadata
    descriptors = (
        tool_metadata.get("descriptors") if isinstance(tool_metadata, dict) else None
    )
    catalog = (
        snapshot.skill_catalog if isinstance(snapshot.skill_catalog, list) else None
    )
    expected_conditions = {"project_skill_catalog"} if catalog else set()
    expected_descriptors = TOOL_REGISTRY.frozen_descriptor_metadata(
        conditions=expected_conditions
    )
    if (
        snapshot.tool_registry_hash != metadata["hash"]
        or snapshot.tool_registry_version != metadata["version"]
        or not isinstance(descriptors, list)
        or descriptors != expected_descriptors
        or catalog is None
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("version_id"), str)
            or not isinstance(item.get("content_hash"), str)
            for item in catalog
        )
    ):
        return failure

    frozen_names = [item["name"] for item in descriptors if isinstance(item, dict)]
    if len(frozen_names) != len(set(frozen_names)):
        return failure
    return {
        "runtime_tool_names": frozen_names,
        "tool_registry_hash": metadata["hash"],
        "tool_registry_version": metadata["version"],
        "runtime_projection_unavailable": False,
        "project_skill_catalog": _state_catalog(catalog),
    }


def _snapshot_error(error_type: str, error: str) -> dict[str, str]:
    """Keep loader failures structured for the model and tool audit trail."""
    record_project_skill_event("loader", "rejected")
    return {"error_type": error_type, "error": error}


def _estimated_instruction_tokens(instructions: str) -> int:
    """Match the conservative catalog scanner approximation without model I/O."""
    return (len(instructions) + 3) // 4


def _snapshot_has_frozen_loader(snapshot: AgentRuntimeSnapshot) -> bool:
    """Check the durable snapshot itself authorizes conditional skill loading."""
    catalog = snapshot.skill_catalog
    if not isinstance(catalog, list) or not catalog:
        return False
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("name"), str)
        or not isinstance(item.get("version_id"), str)
        or not isinstance(item.get("content_hash"), str)
        for item in catalog
    ):
        return False

    registry_metadata = TOOL_REGISTRY.metadata_snapshot()
    conditions = {"project_skill_catalog"}
    descriptors = (
        snapshot.tool_metadata.get("descriptors")
        if isinstance(snapshot.tool_metadata, dict)
        else None
    )
    expected_descriptors = TOOL_REGISTRY.frozen_descriptor_metadata(
        conditions=conditions
    )
    return (
        snapshot.tool_registry_hash == registry_metadata["hash"]
        and snapshot.tool_registry_version == registry_metadata["version"]
        and isinstance(descriptors, list)
        and descriptors == expected_descriptors
        and any(
            isinstance(item, dict) and item.get("name") == "load_project_skill"
            for item in descriptors
        )
    )


async def load_project_skill_from_snapshot(
    session: AsyncSession,
    *,
    snapshot_id: str | None,
    user_id: UUID | str | None,
    project_id: UUID | str | None,
    skill_name: str,
) -> dict[str, Any]:
    """Load one exact, frozen instruction document from a durable snapshot.

    The caller supplies all context from server-owned config.  ``skill_name``
    is intentionally the only model-controlled value; it must match an entry
    already persisted in the snapshot catalog.
    """
    if not get_settings().PROJECT_SKILL_RUNTIME_ENABLED:
        return _snapshot_error(
            "project_skill_runtime_disabled", "Project skill runtime is disabled."
        )
    snapshot_uuid = _as_uuid(snapshot_id)
    actor_id = _as_uuid(user_id)
    expected_project_id = _as_uuid(project_id)
    if snapshot_uuid is None or actor_id is None or expected_project_id is None:
        return _snapshot_error(
            "runtime_snapshot_required",
            "Project skill loading requires server runtime snapshot context.",
        )

    snapshot = await session.get(
        AgentRuntimeSnapshot, snapshot_uuid, with_for_update=True
    )
    now = datetime.now(timezone.utc)
    if (
        snapshot is None
        or snapshot.user_id != actor_id
        or snapshot.project_id != expected_project_id
        or (
            snapshot.expires_at is not None
            and snapshot.expires_at.replace(tzinfo=timezone.utc) <= now
        )
    ):
        return _snapshot_error(
            "runtime_snapshot_unavailable",
            "The project skill snapshot is unavailable for this run.",
        )

    try:
        from src.services.project_skills.skill_document import normalize_skill_name

        normalized_name = normalize_skill_name(skill_name)
    except ValueError:
        return _snapshot_error(
            "invalid_skill_name", "skill_name must be lowercase kebab-case."
        )

    entry = next(
        (
            item
            for item in snapshot.skill_catalog or []
            if item.get("name") == normalized_name
        ),
        None,
    )
    if entry is None:
        return _snapshot_error(
            "skill_not_in_snapshot",
            "That skill is not available in this run's snapshot.",
        )

    if not _snapshot_has_frozen_loader(snapshot):
        return _snapshot_error(
            "tool_not_in_snapshot",
            "Project skill loading was not enabled in this run's frozen tool snapshot.",
        )

    prior_loads = list(snapshot.loaded_skill_versions or [])
    prior = next(
        (item for item in prior_loads if item.get("name") == normalized_name), None
    )
    if (
        prior is None
        and len({item.get("name") for item in prior_loads}) >= MAX_LOADED_PROJECT_SKILLS
    ):
        return _snapshot_error(
            "project_skill_load_limit",
            "At most three project skills may be loaded per turn.",
        )

    version_id = _as_uuid(entry.get("version_id"))
    version = await session.get(ProjectSkillVersion, version_id) if version_id else None
    if (
        version is None
        or str(version.id) != str(entry.get("version_id"))
        or version.parsed_name != normalized_name
        or version.version != entry.get("version")
        or version.content_hash != entry.get("content_hash")
        or sha256(version.instructions.encode("utf-8")).hexdigest()
        != version.content_hash
    ):
        return _snapshot_error(
            "skill_version_unavailable",
            "The frozen project skill version is unavailable.",
        )
    version_project_id = await session.scalar(
        select(ProjectSkill.project_id)
        .join(ProjectSkillVersion, ProjectSkill.id == ProjectSkillVersion.skill_id)
        .where(ProjectSkillVersion.id == version.id)
    )
    if version_project_id != snapshot.project_id:
        return _snapshot_error(
            "skill_version_unavailable",
            "The frozen project skill version is unavailable.",
        )

    token_count = _estimated_instruction_tokens(version.instructions)
    loaded_tokens = sum(int(item.get("token_count", 0) or 0) for item in prior_loads)
    if prior is None and loaded_tokens + token_count > MAX_LOADED_PROJECT_SKILL_TOKENS:
        return _snapshot_error(
            "project_skill_token_limit",
            "Loading this skill would exceed the per-turn project skill token limit.",
        )

    record = prior or {
        "version_id": str(version.id),
        "name": normalized_name,
        "content_hash": version.content_hash,
        "token_count": token_count,
    }
    if prior is None:
        snapshot.loaded_skill_versions = [*prior_loads, record]
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            logger.warning("failed to record loaded project skill", exc_info=True)
            return _snapshot_error(
                "runtime_snapshot_unavailable",
                "The project skill snapshot could not be updated.",
            )

    record_project_skill_event(
        "loader",
        "success",
        loaded_skill_count=1 if prior is None else 0,
        loaded_skill_tokens=token_count if prior is None else 0,
    )
    return {
        "name": normalized_name,
        "version": version.version,
        "content_hash": version.content_hash,
        "instructions": version.instructions,
        "loaded_skill_version": record,
    }
