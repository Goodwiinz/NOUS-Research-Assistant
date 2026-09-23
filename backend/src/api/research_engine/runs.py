"""Research Engine run endpoints."""

import asyncio
import functools
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_run import ResearchRun, RunStatus
from src.models.research_step import ResearchStep
from src.models.user import User
from src.schemas.research_engine import (
    RunCreate,
    RunResponse,
    validate_blueprint_runtime,
)
from src.services.expensive_work_admission import admit_expensive_work
from src.services.research_engine.connectors import (
    ArxivConnector,
    RagStoreConnector,
    SemanticScholarConnector,
)
from src.services.research_engine.connectors.registry import build_connectors
from src.services.research_engine.engine import WorkflowEngine
from src.services.research_engine.providers import (
    ClaudeProvider,
    OllamaProvider,
    OpenAIProvider,
    ProviderConfig,
)
from src.services.research_engine.source_persistence import research_source_rows
from src.services.research_engine.step_executor import StepExecutor

logger = logging.getLogger(__name__)

_PAUSE_REQUESTED_KEY = "_pause_requested"


def _pause_requested(run: ResearchRun) -> bool:
    manifest = run.reproducibility_manifest or {}
    return bool(manifest.get(_PAUSE_REQUESTED_KEY))


def _set_pause_requested(run: ResearchRun, requested: bool) -> None:
    manifest = dict(run.reproducibility_manifest or {})
    if requested:
        manifest[_PAUSE_REQUESTED_KEY] = True
    else:
        manifest.pop(_PAUSE_REQUESTED_KEY, None)
    run.reproducibility_manifest = manifest or None


def _get_verified_organization_id(current_user: Any) -> Any:
    """Return the authenticated user's server-side organization ID."""
    organization_id = getattr(current_user, "organization_id", None)
    if not organization_id:
        organization = getattr(current_user, "organization", None)
        organization_id = getattr(organization, "id", None)
    if not organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No organization associated with this account",
        )
    return organization_id


async def _get_owned_run(
    run_id: UUID,
    user_id: UUID,
    db: AsyncSession,
) -> ResearchRun:
    """Fetch a run in a single query, verifying the user owns it via JOIN.

    Raises 404 if the run doesn't exist or the user doesn't own it.
    """
    query = (
        select(ResearchRun)
        .join(ResearchBlueprint, ResearchBlueprint.id == ResearchRun.blueprint_id)
        .join(ResearchProject, ResearchProject.id == ResearchBlueprint.project_id)
        .where(
            ResearchRun.id == run_id,
            ResearchProject.owner_id == user_id,
        )
    )
    result = await db.execute(query)
    run = result.scalars().first()
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run not found",
        )
    return run


def _get_step_params(step_def: Dict[str, Any]) -> Dict[str, Any]:
    return step_def.get("params") or step_def.get("parameters") or {}


def _create_provider(model_id: str):
    normalized = (model_id or "").strip()
    if not normalized:
        return None

    from src.core.config import settings

    lower = normalized.lower()
    if lower.startswith("claude"):
        if not settings.ANTHROPIC_API_KEY:
            return None
        return ClaudeProvider(
            ProviderConfig(
                provider_type="claude",
                model_id=normalized,
                api_key=settings.ANTHROPIC_API_KEY,
            )
        )

    if lower.startswith("gpt") or lower.startswith("o1") or lower.startswith("o3"):
        if not settings.OPENAI_API_KEY:
            return None
        return OpenAIProvider(
            ProviderConfig(
                provider_type="openai",
                model_id=normalized,
                api_key=settings.OPENAI_API_KEY,
            )
        )

    if (
        lower.startswith("ollama/")
        or lower.startswith("llama")
        or lower.startswith("mistral")
    ):
        ollama_model = (
            normalized.split("/", 1)[1] if lower.startswith("ollama/") else normalized
        )
        return OllamaProvider(
            ProviderConfig(
                provider_type="ollama",
                model_id=ollama_model,
                base_url=os.getenv("OLLAMA_BASE_URL"),
            )
        )

    return None


def _build_providers(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    providers: Dict[str, Any] = {}
    for step_def in steps:
        params = _get_step_params(step_def)
        model_id = step_def.get("model_id") or params.get("model_id")
        if not model_id or model_id in providers:
            continue
        provider = _create_provider(str(model_id))
        if provider is not None:
            providers[str(model_id)] = provider
    return providers


async def _search_rag_store(
    query: str, max_results: int = 50, *, organization_id: Optional[str] = None
) -> Dict[str, Any]:
    """Search the existing hybrid RAG index for local-store style connector output.

    MUST be scoped to the run owner's ``organization_id`` — without it
    ``hybrid_search_service.search`` runs org-unfiltered and a research run
    would surface (and copy ``full_text`` from) every tenant's documents.
    """
    if not organization_id:
        return {"results": []}

    from src.models.search_schemas import SearchQuery
    from src.services.search.hybrid_search_service import hybrid_search_service

    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(
        None,
        lambda: hybrid_search_service.search(
            SearchQuery(query=query, limit=max_results, search_type="hybrid"),
            organization_id=organization_id,
        ),
    )

    results = []
    for item in response.results:
        metadata = getattr(item, "metadata", {}) or {}
        content = (
            metadata.get("full_text")
            or metadata.get("text")
            or getattr(item, "content_preview", None)
            or getattr(item, "content_snippet", None)
            or ""
        )
        results.append(
            {
                "id": str(getattr(item, "document_id", "")),
                "title": getattr(item, "title", ""),
                "content": content,
                "metadata": metadata,
            }
        )
    return {"results": results}


def _build_connectors(organization_id: Optional[str] = None) -> Dict[str, Any]:
    """Create connector instances for workflow execution.

    ``organization_id`` (the run owner's) is bound into the rag_store search so
    the local-index connector only ever returns this tenant's documents.
    """
    rag_search = functools.partial(_search_rag_store, organization_id=organization_id)
    return build_connectors(rag_search)


def _get_effective_parameters(
    blueprint: ResearchBlueprint, run: ResearchRun
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    base = dict(blueprint.parameters or {})
    overrides: Dict[str, Any] = {}
    manifest = run.reproducibility_manifest or {}
    if isinstance(manifest, dict) and isinstance(
        manifest.get("parameters_override"), dict
    ):
        overrides = manifest["parameters_override"]
    base.update(overrides)
    return base, overrides


router = APIRouter(
    prefix="/research-engine",
    tags=["research-engine"],
)


@router.post(
    "/blueprints/{blueprint_id}/runs",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_run(
    blueprint_id: UUID,
    body: RunCreate = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunResponse:
    """Start a new research run from a blueprint."""
    if body is None:
        body = RunCreate()

    # Look up the blueprint with ownership verification in one query
    query = (
        select(ResearchBlueprint)
        .join(ResearchProject, ResearchProject.id == ResearchBlueprint.project_id)
        .where(
            ResearchBlueprint.id == blueprint_id,
            ResearchProject.owner_id == current_user.id,
        )
    )
    result = await db.execute(query)
    blueprint = result.scalars().first()
    if not blueprint:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Blueprint not found",
        )

    # Mark blueprint as immutable
    blueprint.is_immutable = True

    # Create the run; execution starts when /runs/{id}/stream is opened.
    run = ResearchRun(
        blueprint_id=blueprint_id,
        blueprint_version=blueprint.version,
        status=RunStatus.PENDING.value,
        reproducibility_manifest={
            "parameters_override": body.parameters_override or {},
        },
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return RunResponse.model_validate(run)


@router.get(
    "/runs/{run_id}",
    response_model=RunResponse,
)
async def get_run(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunResponse:
    """Get run status."""
    run = await _get_owned_run(run_id, current_user.id, db)
    return RunResponse.model_validate(run)


@router.post(
    "/runs/{run_id}/pause",
    response_model=RunResponse,
)
async def pause_run(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunResponse:
    """Pause a running run."""
    run = await _get_owned_run(run_id, current_user.id, db)
    if run.status != RunStatus.RUNNING.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Run is not currently running",
        )
    # Keep the run claimed while the active stream reaches a safe boundary.
    # Publishing PAUSED here would let a second stream reclaim and replay the
    # in-flight paid step before the first stream can persist its result.
    _set_pause_requested(run, True)
    await db.commit()
    await db.refresh(run)
    return RunResponse.model_validate(run)


@router.post(
    "/runs/{run_id}/resume",
    response_model=RunResponse,
)
async def resume_run(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunResponse:
    """Resume a paused run."""
    run = await _get_owned_run(run_id, current_user.id, db)
    if run.status != RunStatus.PAUSED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Run is not currently paused",
        )
    run.status = RunStatus.RUNNING.value
    await db.commit()
    await db.refresh(run)
    return RunResponse.model_validate(run)


@router.get(
    "/runs/{run_id}/manifest",
)
async def get_manifest(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get reproducibility manifest for a completed run."""
    run = await _get_owned_run(run_id, current_user.id, db)
    if run.status != RunStatus.COMPLETED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Run is not completed",
        )
    return run.reproducibility_manifest or {}


@router.get(
    "/runs/{run_id}/stream",
)
async def stream_run(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stream research run execution via SSE.

    Accepts runs in PENDING or PAUSED status. Returns 404 for missing runs,
    409 for runs in non-streamable states (completed, failed, running).
    """
    run = await _get_owned_run(run_id, current_user.id, db)

    # Only pending or paused runs can be streamed
    streamable = {RunStatus.PENDING.value, RunStatus.PAUSED.value}
    if run.status not in streamable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run is in '{run.status}' state and cannot be streamed",
        )
    was_paused = run.status == RunStatus.PAUSED.value

    # Look up the blueprint
    bp_query = select(ResearchBlueprint).where(
        ResearchBlueprint.id == run.blueprint_id,
    )
    bp_result = await db.execute(bp_query)
    blueprint = bp_result.scalars().first()
    if not blueprint:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Blueprint not found",
        )

    # Determine resume offset from persisted steps for both paused and resumed runs.
    start_from = 0
    step_query = (
        select(ResearchStep)
        .where(ResearchStep.run_id == run_id)
        .order_by(ResearchStep.step_index.desc())
    )
    step_result = await db.execute(step_query)
    last_step = step_result.scalars().first()
    if last_step is not None:
        start_from = last_step.step_index + 1

    required_models = sorted(
        {
            str(step.get("model_id") or _get_step_params(step).get("model_id"))
            for step in blueprint.steps or []
            if step.get("model_id") or _get_step_params(step).get("model_id")
        }
    )
    providers = _build_providers(blueprint.steps or [])
    missing_models = [
        model_id for model_id in required_models if model_id not in providers
    ]
    if missing_models:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "No configured LLM provider is available for: "
                + ", ".join(missing_models)
            ),
        )

    effective_parameters, parameter_overrides = _get_effective_parameters(
        blueprint, run
    )
    try:
        validate_blueprint_runtime(
            {"steps": blueprint.steps or [], "parameters": effective_parameters}
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Blueprint exceeds a server-owned execution limit",
        )
    blueprint_dict = {
        "steps": blueprint.steps or [],
        "parameters": effective_parameters,
    }
    total_tokens = run.total_tokens or 0

    # R5-M18: claim the transition atomically — the old read-check-write let
    # two concurrent SSE streams both pass the PENDING check and double-execute
    # a paid run.
    from sqlalchemy import update as _sa_update

    organization_id = _get_verified_organization_id(current_user)
    connectors = _build_connectors(organization_id=str(organization_id))

    claim = await db.execute(
        _sa_update(ResearchRun)
        .where(
            ResearchRun.id == run_id,
            ResearchRun.status.in_(["pending", "paused"]),
        )
        .values(status=RunStatus.RUNNING.value)
    )
    if claim.rowcount == 0:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Run was just claimed by another stream",
        )
    await db.commit()

    # Admit only after this request has won the atomic claim. Otherwise every
    # concurrent loser consumes a shared paid-work slot before receiving 409.
    if not await admit_expensive_work(
        user_id=current_user.id,
        organization_id=organization_id,
    ):
        release = await db.execute(
            _sa_update(ResearchRun)
            .where(
                ResearchRun.id == run_id,
                ResearchRun.status == RunStatus.RUNNING.value,
            )
            .values(
                status=RunStatus.PAUSED.value if was_paused else RunStatus.PENDING.value
            )
        )
        await db.commit()
        if release.rowcount == 0:
            logger.warning(
                "Research run %s could not be released after admission denial",
                run_id,
            )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many expensive research runs; retry later",
        )

    await db.refresh(run)
    run.started_at = run.started_at or datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(run)

    async def event_generator():
        """Yield SSE-formatted events from the workflow engine."""
        nonlocal total_tokens
        executor = StepExecutor(providers=providers, connectors=connectors)
        # R5-M19: rehydrate accumulated step outputs so resumed/synthesize
        # steps see everything earlier steps produced instead of starting
        # from bare blueprint parameters.
        prior_outputs: dict = {}
        history = (
            (
                await db.execute(
                    select(ResearchStep)
                    .where(ResearchStep.run_id == run_id)
                    .order_by(ResearchStep.step_index.asc())
                )
            )
            .scalars()
            .all()
        )
        for done_step in history:
            if isinstance(done_step.output, dict):
                prior_outputs.update(done_step.output)

        engine = WorkflowEngine(step_executor=executor)

        try:
            async for event in engine.run(
                blueprint=blueprint_dict,
                run_id=run_id,
                start_from_step=start_from,
                initial_context=prior_outputs,
                initial_total_tokens=total_tokens,
                started_at=(
                    None
                    if was_paused
                    else (
                        run.started_at.timestamp()
                        if run.started_at is not None
                        else None
                    )
                ),
            ):
                event_type = event.get("event")
                await db.refresh(run)

                pause_requested = _pause_requested(run) and event_type != "run_paused"

                # An unfinished event can stop immediately. A completed paid step
                # must be persisted and charged before the pause takes effect.
                if pause_requested and event_type != "step_complete":
                    _set_pause_requested(run, False)
                    run.status = RunStatus.PAUSED.value
                    run.total_tokens = total_tokens
                    await db.commit()
                    paused_event = {
                        "event": "run_paused",
                        "run_id": str(run.id),
                        "reason": "Paused by user",
                    }
                    data = json.dumps(paused_event)
                    yield f"event: run_paused\ndata: {data}\n\n"
                    break

                if event_type == "step_complete":
                    step_index = int(event.get("step_index") or 0)
                    step_def = {}
                    if 0 <= step_index < len(blueprint_dict["steps"]):
                        step_def = blueprint_dict["steps"][step_index] or {}

                    params = _get_step_params(step_def)
                    model_id = step_def.get("model_id") or params.get("model_id")
                    mode = step_def.get("mode") or params.get("mode") or "deterministic"
                    temperature = params.get("temperature", 0.0)
                    seed = params.get("seed")
                    output = event.get("output")
                    if output is not None and not isinstance(output, dict):
                        output = {"value": output}

                    if step_def.get("type") == "search" and output:
                        for source_row in research_source_rows(run_id, output):
                            db.add(source_row)

                    db.add(
                        ResearchStep(
                            run_id=run_id,
                            step_index=step_index,
                            step_type=str(
                                event.get("step_type")
                                or step_def.get("type")
                                or "search"
                            ),
                            mode=str(mode),
                            model_id=str(model_id) if model_id is not None else None,
                            temperature=float(temperature),
                            seed=int(seed) if seed is not None else None,
                            output=output,
                            quality_marks=event.get("quality_marks") or [],
                            token_count=int(event.get("token_count") or 0),
                            completed_at=datetime.now(timezone.utc),
                        )
                    )
                    total_tokens += int(event.get("token_count") or 0)
                    run.total_tokens = total_tokens
                    if pause_requested:
                        _set_pause_requested(run, False)
                        run.status = RunStatus.PAUSED.value
                    await db.commit()
                elif event_type == "run_paused":
                    _set_pause_requested(run, False)
                    run.status = RunStatus.PAUSED.value
                    run.total_tokens = total_tokens
                    await db.commit()
                elif event_type == "run_failed":
                    _set_pause_requested(run, False)
                    run.status = RunStatus.FAILED.value
                    run.total_tokens = total_tokens
                    run.completed_at = datetime.now(timezone.utc)
                    await db.commit()
                elif event_type == "run_complete":
                    run.status = RunStatus.COMPLETED.value
                    run.total_tokens = total_tokens
                    run.completed_at = datetime.now(timezone.utc)
                    run.reproducibility_manifest = {
                        "run_id": str(run.id),
                        "blueprint_id": str(blueprint.id),
                        "blueprint_version": blueprint.version,
                        "total_tokens": total_tokens,
                        "completed_at": run.completed_at.isoformat(),
                        "parameters_override": parameter_overrides,
                        "parameters": effective_parameters,
                    }
                    await db.commit()

                event_type = event.get("event", "message")
                data = json.dumps(event)
                yield f"event: {event_type}\ndata: {data}\n\n"

                if pause_requested:
                    paused_event = {
                        "event": "run_paused",
                        "run_id": str(run.id),
                        "reason": "Paused by user",
                    }
                    data = json.dumps(paused_event)
                    yield f"event: run_paused\ndata: {data}\n\n"
                    break
        except asyncio.CancelledError:
            # Client disconnected; persist paused state so run can resume later.
            _set_pause_requested(run, False)
            run.status = RunStatus.PAUSED.value
            run.total_tokens = total_tokens
            await db.commit()
            raise
        except Exception as exc:
            # If streaming fails unexpectedly, mark run as failed
            await db.rollback()
            logger.error(f"Stream error for run {run_id}: {exc}")
            _set_pause_requested(run, False)
            run.status = RunStatus.FAILED.value
            run.total_tokens = total_tokens
            run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            error_event = json.dumps({"event": "run_failed", "error": str(exc)})
            yield f"event: run_failed\ndata: {error_event}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
