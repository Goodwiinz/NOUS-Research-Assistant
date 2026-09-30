"""Bind execution to the exact plan reviewed in an approved protocol version."""

from copy import deepcopy
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.research_blueprint import ResearchBlueprint
from src.models.research_protocol import ResearchProtocol, ResearchProtocolVersion
from src.models.research_run import ResearchRun
from src.schemas.research_engine import RunCreate, validate_blueprint_runtime
from src.services.research_engine.project_access import ProjectContext
from src.services.research_engine.protocol_service import (
    blueprint_execution_plan,
    canonical_hash,
    protocol_content,
)


def effective_plan_hash(version: ResearchProtocolVersion) -> str:
    """Bind the immutable execution plan to its exact methodological snapshot."""
    return canonical_hash(
        {
            "protocol_content_hash": version.content_hash,
            "execution_plan": version.execution_plan,
        }
    )


def _verify_plan(
    version: ResearchProtocolVersion, blueprint: ResearchBlueprint
) -> dict[str, Any]:
    if blueprint.is_deleted:
        raise HTTPException(status_code=404, detail="Blueprint not found")
    content = protocol_content(
        version.question_version_id,
        version.blueprint_id,
        version.snapshot,
        version.execution_plan,
    )
    if canonical_hash(content) != version.content_hash:
        raise HTTPException(status_code=409, detail="Protocol content hash mismatch")
    plan = cast(dict[str, Any], version.execution_plan)
    if canonical_hash(blueprint_execution_plan(blueprint)) != canonical_hash(plan):
        raise HTTPException(
            status_code=409, detail="Blueprint changes require a protocol amendment"
        )
    try:
        validate_blueprint_runtime(plan)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Approved plan exceeds a server-owned execution limit",
        ) from None
    return deepcopy(plan)


async def _bound_version(
    db: AsyncSession,
    context: ProjectContext,
    version_id: UUID,
    blueprint_id: UUID,
    *,
    new_run: bool,
) -> ResearchProtocolVersion:
    query = (
        select(ResearchProtocolVersion)
        .join(
            ResearchProtocol, ResearchProtocol.id == ResearchProtocolVersion.protocol_id
        )
        .where(
            ResearchProtocolVersion.id == version_id,
            ResearchProtocolVersion.blueprint_id == blueprint_id,
            ResearchProtocol.collection_id == context.collection.id,
            ResearchProtocol.is_deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if new_run:
        query = query.where(
            ResearchProtocolVersion.status == "approved",
            ResearchProtocol.current_approved_version_id == ResearchProtocolVersion.id,
        )
    else:
        # Supersession does not rewrite the original plan of an existing run.
        query = query.where(
            ResearchProtocolVersion.status.in_(["approved", "superseded"])
        )
    version = cast(
        ResearchProtocolVersion | None,
        (await db.execute(query)).scalars().one_or_none(),
    )
    if version is None:
        raise HTTPException(
            status_code=409, detail="Approved protocol version is required"
        )
    return version


async def create_approved_run(
    db: AsyncSession,
    context: ProjectContext,
    blueprint: ResearchBlueprint,
    body: RunCreate,
    *,
    manifest_metadata: dict[str, Any] | None = None,
) -> ResearchRun:
    """Commit one pending run after its caller obtains project EDIT authority."""
    if body.protocol_version_id is None:
        raise HTTPException(status_code=409, detail="approved_protocol_required")
    # The engine currently has no supported operational override contract.
    # Accepting an arbitrary dict would make exact-plan approval meaningless.
    if body.parameters_override:
        raise HTTPException(
            status_code=409, detail="Method override requires a protocol amendment"
        )
    # The route can preload this object before waiting for the project lock.
    await db.refresh(blueprint)
    version = await _bound_version(
        db, context, body.protocol_version_id, blueprint.id, new_run=True
    )
    _verify_plan(version, blueprint)
    blueprint.is_immutable = True
    metadata = deepcopy(manifest_metadata or {})
    if "parameters_override" in metadata:
        raise ValueError("manifest metadata cannot replace parameters_override")
    run = ResearchRun(
        blueprint_id=blueprint.id,
        blueprint_version=blueprint.version,
        protocol_version_id=version.id,
        effective_plan_hash=effective_plan_hash(version),
        conformance_status="plan_verified",
        status="pending",
        reproducibility_manifest={"parameters_override": {}, **metadata},
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def require_run_conformance(
    db: AsyncSession,
    run: ResearchRun,
    blueprint: ResearchBlueprint,
    context: ProjectContext,
) -> dict[str, Any]:
    """Return the frozen plan before stream/resume can claim paid execution."""
    # Re-read identities loaded before the shared project mutation lock.
    await db.refresh(run)
    await db.refresh(blueprint)
    if run.is_deleted:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.protocol_version_id is None:
        raise HTTPException(
            status_code=409, detail="Run is not bound to an approved protocol"
        )
    version = await _bound_version(
        db, context, run.protocol_version_id, blueprint.id, new_run=False
    )
    plan = _verify_plan(version, blueprint)
    manifest = run.reproducibility_manifest or {}
    overrides = manifest.get("parameters_override", {})
    if (
        run.effective_plan_hash != effective_plan_hash(version)
        or run.blueprint_version != plan["blueprint_version"]
        or run.conformance_status not in {"plan_verified", "conformant"}
        or not isinstance(overrides, dict)
        or bool(overrides)
    ):
        raise HTTPException(
            status_code=409,
            detail="Run execution plan does not match its approved protocol",
        )
    return plan
