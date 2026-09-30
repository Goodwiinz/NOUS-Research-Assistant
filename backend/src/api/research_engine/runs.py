"""Research Engine run endpoints."""

import asyncio
import functools
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Dict, List, Optional
from uuid import UUID

from anyio import CancelScope
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import cast, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_run import ResearchRun, RunStatus
from src.models.research_step import ResearchStep
from src.models.user import User
from src.schemas.research_engine import (
    ExportFormat,
    RunCreate,
    RunResponse,
    RunResumeRequest,
    validate_blueprint_runtime,
)
from src.services.expensive_work_admission import admit_expensive_work
from src.services.research_engine.connectors import (
    ArxivConnector,
    RagStoreConnector,
    SemanticScholarConnector,
)
from src.services.research_engine.connectors.registry import (
    build_connectors,
    safe_capability_projection,
)
from src.services.research_engine.contracts import (
    canonical_stage_output_hash,
    rehydrate_stage_outputs,
)
from src.services.research_engine.engine import WorkflowEngine
from src.services.research_engine.export_service import (
    ExportService,
    ResearchExportError,
)
from src.services.research_engine.observability import (
    research_observability,
    safely_observe,
)
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    require_blueprint,
    require_run,
    resolve_engine_project_context,
    resolve_project,
)
from src.services.research_engine.providers import (
    ClaudeProvider,
    OllamaProvider,
    OpenAIProvider,
    ProviderConfig,
)
from src.services.research_engine.review_service import ResearchReviewService
from src.services.research_engine.run_conformance import create_approved_run
from src.services.research_engine.run_conformance import (
    require_run_conformance as _require_run_conformance,
)
from src.services.research_engine.run_lifecycle import (
    ResearchRunLifecycleError,
    ResearchRunLifecycleService,
)
from src.services.research_engine.scope import (
    canonicalize_scope_confirmation,
    resolve_effective_daily_brief_parameters,
)
from src.services.research_engine.search_receipts import SearchReceiptJournal
from src.services.research_engine.source_persistence import research_source_rows
from src.services.research_engine.step_executor import StepExecutor

logger = logging.getLogger(__name__)

_PAUSE_REQUESTED_KEY = "_pause_requested"
_CONTINUATION_REQUESTED_KEY = "continuation_requested"
_CONTINUED_AFTER_FAILURE_KEY = "continued_after_failure"
_EXECUTION_ERRORS_KEY = "execution_errors"


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


def _research_step_from_event(
    run_id: UUID,
    step_def: Dict[str, Any],
    event: Dict[str, Any],
) -> ResearchStep:
    """Build the durable step row from the executor's completed-step event."""
    params = _get_step_params(step_def)
    model_id = (
        event.get("model_id") or step_def.get("model_id") or params.get("model_id")
    )
    temperature = event.get("temperature")
    if temperature is None:
        temperature = step_def.get("temperature", params.get("temperature", 0.0))
    if "seed" in event:
        seed = event["seed"]
    else:
        seed = step_def.get("seed", params.get("seed"))
    output = event.get("output")
    if output is not None and not isinstance(output, dict):
        output = {"value": output}

    return ResearchStep(
        run_id=run_id,
        step_index=int(event.get("step_index") or 0),
        step_type=str(event.get("step_type") or step_def.get("type") or "search"),
        mode=str(step_def.get("mode") or params.get("mode") or "deterministic"),
        inputs_hash=event.get("inputs_hash"),
        outputs_hash=event.get("outputs_hash"),
        full_prompt=event.get("full_prompt"),
        model_id=str(model_id) if model_id is not None else None,
        model_version=event.get("model_version"),
        temperature=float(temperature),
        seed=int(seed) if seed is not None else None,
        output=output,
        quality_marks=event.get("quality_marks") or [],
        token_count=int(event.get("token_count") or 0),
        completed_at=datetime.now(timezone.utc),
    )


async def _run_interrupted_cleanup(cleanup: Awaitable[None]) -> None:
    """Finish durable stream cleanup outside the request cancellation scope."""

    async def shielded_cleanup() -> None:
        with CancelScope(shield=True):
            await cleanup

    cleanup_task = asyncio.create_task(shielded_cleanup())
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            continue
    cleanup_task.result()


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
    action: ResearchAction = ResearchAction.VIEW,
) -> ResearchRun:
    """Resolve a run through the shared mapped-project access policy."""
    return await require_run(db, run_id, user_id, action)


def _run_response(run: ResearchRun, context: ProjectContext) -> RunResponse:
    assert context.engine is not None
    return RunResponse.model_validate(
        {
            **run.__dict__,
            "project_id": context.collection.id,
            "research_engine_project_id": context.engine.id,
        }
    )


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
    body: Optional[RunCreate] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunResponse:
    """Start a new research run from a blueprint."""
    body = body or RunCreate()
    blueprint = await require_blueprint(
        db, blueprint_id, current_user.id, ResearchAction.EDIT
    )
    if (
        blueprint.template_source == "daily_research_brief"
        and not settings.DAILY_RESEARCH_BRIEF_ENABLED
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Blueprint not found",
        )

    manifest_metadata: Dict[str, Any] = {}
    if blueprint.template_source == "daily_research_brief":
        if body.scope_confirmation is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Daily Brief scope confirmation is required",
            )
        try:
            effective = resolve_effective_daily_brief_parameters(
                blueprint.parameters or {}, {}
            )
            manifest_metadata["scope_confirmation"] = canonicalize_scope_confirmation(
                effective=effective,
                submitted=body.scope_confirmation,
                actor_id=current_user.id,
                confirmed_at=datetime.now(timezone.utc),
            )
            selected = set(manifest_metadata["scope_confirmation"]["providers"])
            manifest_metadata["provider_manifest"] = [
                capability
                for capability in safe_capability_projection()
                if capability["id"] in selected
            ]
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Scope confirmation does not match effective Daily Brief parameters"
                ),
            ) from exc

    context = await resolve_engine_project_context(
        db, blueprint.project_id, current_user.id, ResearchAction.EDIT
    )
    run = await create_approved_run(
        db,
        context,
        blueprint,
        body,
        manifest_metadata=manifest_metadata,
    )
    return _run_response(run, context)


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
    run = await _get_owned_run(run_id, current_user.id, db, ResearchAction.VIEW)
    blueprint = await db.get(ResearchBlueprint, run.blueprint_id)
    assert blueprint is not None
    context = await resolve_engine_project_context(
        db, blueprint.project_id, current_user.id, ResearchAction.VIEW
    )
    return _run_response(run, context)


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
    run = await _get_owned_run(run_id, current_user.id, db, ResearchAction.EDIT)
    if run.status != RunStatus.RUNNING.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Run is not currently running",
        )
    # Keep the run claimed while the active stream reaches a safe boundary.
    # Publishing PAUSED here would let a second stream reclaim and replay the
    # in-flight paid step before the first stream can persist its result.
    lifecycle = ResearchRunLifecycleService(db)
    pause_descriptor = await lifecycle.current_user_pause_descriptor(run=run)
    pause_patch = {
        _PAUSE_REQUESTED_KEY: True,
        "user_pause": lifecycle.user_pause_manifest_value(pause_descriptor),
    }
    merged_manifest = func.coalesce(
        ResearchRun.reproducibility_manifest,
        cast({}, JSONB),
    ).op("||")(cast(pause_patch, JSONB))
    pause_update = await db.execute(
        update(ResearchRun)
        .where(
            ResearchRun.id == run_id,
            ResearchRun.status == RunStatus.RUNNING.value,
        )
        .values(reproducibility_manifest=merged_manifest)
        .execution_options(synchronize_session=False)
    )
    if pause_update.rowcount == 0:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Run finished before the pause request was recorded",
        )
    await db.commit()
    await db.refresh(run)
    blueprint = await db.get(ResearchBlueprint, run.blueprint_id)
    assert blueprint is not None
    context = await resolve_engine_project_context(
        db, blueprint.project_id, current_user.id, ResearchAction.EDIT
    )
    return _run_response(run, context)


@router.post(
    "/runs/{run_id}/resume",
    response_model=RunResponse,
)
async def resume_run(
    run_id: UUID,
    body: RunResumeRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunResponse:
    """Authorize a paused run to be claimed exactly once by its stream."""
    if body is None:
        body = RunResumeRequest()
    run = await _get_owned_run(run_id, current_user.id, db, ResearchAction.EDIT)
    blueprint = await db.get(ResearchBlueprint, run.blueprint_id)
    assert blueprint is not None
    context = await resolve_engine_project_context(
        db, blueprint.project_id, current_user.id, ResearchAction.EDIT
    )
    await _require_run_conformance(db, run, blueprint, context)
    lifecycle = ResearchRunLifecycleService(db)
    try:
        await lifecycle.authorize_resume(
            run=run,
            actor_id=current_user.id,
            request=body,
        )
    except ResearchRunLifecycleError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail(),
        ) from exc
    return _run_response(run, context)


@router.get(
    "/runs/{run_id}/manifest",
)
async def get_manifest(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the current manifest, including durable in-flight search receipts."""
    run = await _get_owned_run(run_id, current_user.id, db, ResearchAction.VIEW)
    manifest = dict(run.reproducibility_manifest or {})
    manifest.pop(_PAUSE_REQUESTED_KEY, None)
    return {
        **manifest,
        "run_status": run.status,
    }


@router.get("/runs/{run_id}/export")
async def export_run(
    run_id: UUID,
    format: ExportFormat = ExportFormat.MARKDOWN,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Download an owner-scoped artifact for a completed research run."""
    try:
        artifact = await ExportService().export(
            run_id,
            UUID(str(current_user.id)),
            format.value,
            db,
        )
    except ResearchExportError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail(),
        ) from exc
    return Response(
        content=artifact.content,
        media_type=artifact.media_type,
        headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"'},
    )


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
    run = await _get_owned_run(run_id, current_user.id, db, ResearchAction.EDIT)

    # Only pending or paused runs can be streamed
    streamable = {RunStatus.PENDING.value, RunStatus.PAUSED.value}
    if run.status not in streamable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run is in '{run.status}' state and cannot be streamed",
        )
    was_paused = run.status == RunStatus.PAUSED.value
    actor_user_id = current_user.id

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
    lifecycle_context = await resolve_engine_project_context(
        db, blueprint.project_id, current_user.id, ResearchAction.EDIT
    )
    canonical_project_id = lifecycle_context.collection.id
    approved_plan = await _require_run_conformance(
        db, run, blueprint, lifecycle_context
    )

    # Determine resume offset from persisted steps for both paused and resumed runs.
    start_from = 0
    step_query = (
        select(ResearchStep)
        .where(
            ResearchStep.run_id == run_id,
            ResearchStep.is_deleted.is_(False),
        )
        .order_by(ResearchStep.step_index.desc())
    )
    step_result = await db.execute(step_query)
    last_step = step_result.scalars().first()
    if last_step is not None:
        start_from = last_step.step_index + 1

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
    try:
        prior_outputs = rehydrate_stage_outputs(history)
    except ValueError:
        safely_observe(
            research_observability,
            "record_rehydration_error",
            run_id=run_id,
            organization_id=(
                current_user.organization_id
                if isinstance(getattr(current_user, "organization_id", None), UUID)
                else None
            ),
            error_kind="invalid_history",
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "run_reconstruction_failed",
                "message": "Persisted run stages cannot be resumed",
            },
        ) from None

    manifest = dict(run.reproducibility_manifest or {})
    scope_confirmation = manifest.get("scope_confirmation")
    provider_manifest = manifest.get("provider_manifest")
    if isinstance(scope_confirmation, dict):
        prior_outputs["scope_confirmation"] = scope_confirmation
    if isinstance(provider_manifest, list):
        prior_outputs["provider_manifest"] = provider_manifest
    if manifest.get("review_history"):
        try:
            prior_outputs = await ResearchReviewService(db).apply_approved_overlays(
                run_id=run_id,
                owner_id=current_user.id,
                context=prior_outputs,
            )
        except ValueError:
            safely_observe(
                research_observability,
                "record_rehydration_error",
                run_id=run_id,
                organization_id=(
                    current_user.organization_id
                    if isinstance(getattr(current_user, "organization_id", None), UUID)
                    else None
                ),
                error_kind="review_overlay",
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "review_overlay_reconstruction_failed",
                    "message": "Approved review overlays cannot be resumed",
                },
            ) from None

    required_models = sorted(
        {
            str(step.get("model_id") or _get_step_params(step).get("model_id"))
            for step in approved_plan["steps"]
            if step.get("model_id") or _get_step_params(step).get("model_id")
        }
    )
    providers = _build_providers(approved_plan["steps"])
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

    effective_parameters = approved_plan["parameters"]
    parameter_overrides: Dict[str, Any] = {}
    if blueprint.template_source == "daily_research_brief":
        generated_at = (
            manifest.get("generated_at")
            or manifest.get("completed_at")
            or (
                run.started_at.isoformat()
                if isinstance(run.started_at, datetime)
                else None
            )
            or (
                run.created_at.isoformat()
                if isinstance(run.created_at, datetime)
                else None
            )
        )
        prior_outputs["artifact_provenance"] = {
            "run_id": str(run.id),
            "blueprint_id": str(blueprint.id),
            "blueprint_version": blueprint.version,
            "template_source": blueprint.template_source,
            "template_contract_version": effective_parameters.get("contract_version"),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": None,
            "generated_at": generated_at,
            "stage_hashes": {
                str(step.step_index): step.outputs_hash
                for step in history
                if isinstance(step.outputs_hash, str)
            },
            "models": [
                {
                    "step_index": step_index,
                    "model_id": str(
                        step.get("model_id") or _get_step_params(step).get("model_id")
                    ),
                }
                for step_index, step in enumerate(approved_plan["steps"])
                if step.get("model_id") or _get_step_params(step).get("model_id")
            ],
            "review_history": manifest.get("review_history") or [],
            "scope_confirmation": scope_confirmation or {},
            "provider_manifest": provider_manifest or [],
            "limitations": ["Bounded provider search; results are not exhaustive."],
        }
    if effective_parameters.get("contract_version") == 1 and any(
        not isinstance(step.output, dict) or step.output.get("contract_version") != 1
        for step in history
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This legacy run cannot resume under the version-1 evidence contract; "
                "start a new run from the version-1 template."
            ),
        )
    try:
        validate_blueprint_runtime(
            {"steps": approved_plan["steps"], "parameters": effective_parameters}
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Blueprint exceeds a server-owned execution limit",
        )
    blueprint_dict = {
        "steps": approved_plan["steps"],
        "parameters": effective_parameters,
    }
    total_tokens = run.total_tokens or 0

    # Resolve all fallible, non-paid execution dependencies before changing
    # durable claim state. A setup exception must leave the run retryable.
    organization_id = _get_verified_organization_id(current_user)
    connectors = _build_connectors(organization_id=str(organization_id))

    lifecycle = ResearchRunLifecycleService(db)
    try:
        stream_claim = await lifecycle.claim_stream(run=run)
    except ResearchRunLifecycleError as exc:
        detail: Any = exc.detail()
        if exc.code == "run_already_claimed":
            detail = exc.message
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc

    # Admit only after this request has won the atomic claim. Otherwise every
    # concurrent loser consumes a shared paid-work slot before receiving 409.
    try:
        if stream_claim.authorization_kind == "continue_unverified":
            prior_outputs["continued_after_failure"] = True
            verification = prior_outputs.get("verification")
            if isinstance(verification, dict):
                verification["passed"] = False
                verification["continued_after_failure"] = True
        blueprint_dict = {
            "steps": approved_plan["steps"],
            "parameters": effective_parameters,
        }
        total_tokens = int(run.total_tokens or 0)
        admitted = await admit_expensive_work(
            user_id=current_user.id,
            organization_id=organization_id,
        )
    except BaseException:
        await _run_interrupted_cleanup(
            lifecycle.release_stream_claim(run=run, claim=stream_claim)
        )
        raise
    if not admitted:
        try:
            await lifecycle.release_stream_claim(run=run, claim=stream_claim)
        except ResearchRunLifecycleError:
            logger.warning(
                "Research run %s could not be released after admission denial",
                run_id,
            )
            raise
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many expensive research runs; retry later",
        )

    try:
        receipt_journal = SearchReceiptJournal(db, run_id, current_user.id)
        executor = StepExecutor(
            providers=providers,
            connectors=connectors,
            strategy_context={
                "run_id": str(run.id),
                "canonical_project_id": str(canonical_project_id),
                "protocol_version_id": (
                    str(run.protocol_version_id) if run.protocol_version_id else None
                ),
                "effective_plan_hash": run.effective_plan_hash,
                "blueprint_id": approved_plan["blueprint_id"],
                "blueprint_version": approved_plan["blueprint_version"],
            },
            on_search_page=receipt_journal.persist_page,
        )
        engine = WorkflowEngine(step_executor=executor)
        await db.refresh(run)
    except BaseException:
        await _run_interrupted_cleanup(
            lifecycle.release_stream_claim(run=run, claim=stream_claim)
        )
        raise

    async def event_generator():
        """Yield SSE-formatted events from the workflow engine."""
        nonlocal total_tokens

        pause_anchor_index = (
            int(last_step.step_index) if last_step is not None else None
        )
        pause_anchor_hash = (
            canonical_stage_output_hash(last_step.output)
            if last_step is not None and isinstance(last_step.output, dict)
            else None
        )

        async def recover_run(status_value: str, *, completed: bool = False) -> None:
            # Rollback also expires ORM state, so refresh before inspecting the
            # manifest or starting the recovery transaction.
            await db.rollback()
            await db.refresh(run)
            if status_value == RunStatus.PAUSED.value:
                await lifecycle.recover_stream_cancellation(
                    run=run,
                    step_index=pause_anchor_index,
                    output_hash=pause_anchor_hash,
                    total_tokens=total_tokens,
                )
                return
            _set_pause_requested(run, False)
            manifest = dict(run.reproducibility_manifest or {})
            manifest.pop("user_pause", None)
            setattr(run, "reproducibility_manifest", manifest or None)
            run.status = status_value
            run.total_tokens = total_tokens
            if completed:
                run.completed_at = datetime.now(timezone.utc)
            await db.commit()

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
                organization_id=organization_id,
                record_run_started=not was_paused,
            ):
                event_type = event.get("event")
                await db.refresh(run)
                committed_followup: dict[str, Any] | None = None

                pause_requested = _pause_requested(run) and event_type != "run_paused"

                # An unfinished event can stop immediately. A completed paid step
                # must be persisted and charged before the pause takes effect.
                if pause_requested and event_type not in {
                    "step_complete",
                    "step_error",
                }:
                    descriptor = await lifecycle.persist_user_pause(
                        run=run,
                        step_index=pause_anchor_index,
                        output_hash=pause_anchor_hash,
                        total_tokens=total_tokens,
                    )
                    paused_event = {
                        "event": "run_paused",
                        "run_id": str(run.id),
                        **descriptor.to_dict(),
                    }
                    data = json.dumps(paused_event)
                    yield f"event: run_paused\ndata: {data}\n\n"
                    return

                if event_type == "step_complete":
                    # Paid work may finish after an administrator archives or
                    # deletes the project. Re-read the lifecycle before any
                    # new output/source row is persisted or published.
                    db.expire_all()
                    await resolve_project(
                        db,
                        canonical_project_id,
                        actor_user_id,
                        ResearchAction.EDIT,
                        require_engine=True,
                    )
                    # ``expire_all`` above deliberately invalidates the identity
                    # map before the authorization recheck.  Refresh the run
                    # explicitly before handing it to lifecycle persistence so
                    # synchronous attribute access cannot trigger async I/O.
                    await db.refresh(run)
                    step_index = int(event.get("step_index") or 0)
                    step_def = {}
                    if 0 <= step_index < len(blueprint_dict["steps"]):
                        step_def = blueprint_dict["steps"][step_index] or {}
                    output = event.get("output")
                    if output is not None and not isinstance(output, dict):
                        output = {"value": output}
                    coverage = output.get("coverage") if output else None
                    if (
                        step_def.get("type") == "search"
                        and isinstance(coverage, dict)
                        and isinstance(coverage.get("search_strategy"), dict)
                    ):
                        # Attach imported IDs and attempt history to the durable
                        # journal; the lifecycle commit below persists both.
                        await receipt_journal.finalize_search_step(
                            step_id=str(event.get("step_id") or step_def.get("id")),
                            strategy=coverage["search_strategy"],
                            output=output,
                        )
                    source_rows = (
                        research_source_rows(run_id, output)
                        if step_def.get("type") == "search" and output
                        else []
                    )
                    transition = await lifecycle.persist_step_completion(
                        run=run,
                        event={**event, "output": output},
                        step_definition=step_def,
                        source_rows=source_rows,
                        collection_id=canonical_project_id,
                    )
                    total_tokens = int(run.total_tokens or 0)
                    pause_anchor_index = int(transition.step.step_index)
                    pause_anchor_hash = str(transition.step.outputs_hash)
                    if transition.pause is not None:
                        committed_followup = {
                            "event": "run_paused",
                            "run_id": str(run.id),
                            **transition.pause.to_dict(),
                        }
                    elif transition.terminal_status == "no_evidence":
                        committed_followup = {
                            "event": "run_complete",
                            "run_id": str(run.id),
                            "final_status": "no_evidence",
                        }
                elif event_type == "run_paused":
                    supplied_index = event.get("step_index")
                    supplied_hash = event.get("output_hash")
                    descriptor = await lifecycle.persist_user_pause(
                        run=run,
                        step_index=(
                            supplied_index
                            if type(supplied_index) is int
                            else pause_anchor_index
                        ),
                        output_hash=(
                            str(supplied_hash)
                            if isinstance(supplied_hash, str)
                            else pause_anchor_hash
                        ),
                        total_tokens=total_tokens,
                    )
                    event = {
                        "event": "run_paused",
                        "run_id": str(run.id),
                        **descriptor.to_dict(),
                    }
                elif event_type == "run_failed":
                    _set_pause_requested(run, False)
                    manifest = dict(run.reproducibility_manifest or {})
                    manifest.pop("user_pause", None)
                    run.reproducibility_manifest = manifest or None
                    run.status = RunStatus.FAILED.value
                    run.total_tokens = total_tokens
                    run.completed_at = datetime.now(timezone.utc)
                    await db.commit()
                elif event_type == "step_error":
                    consumed_tokens = max(0, int(event.get("consumed_tokens") or 0))
                    total_tokens += consumed_tokens
                    manifest = dict(run.reproducibility_manifest or {})
                    errors = list(manifest.get(_EXECUTION_ERRORS_KEY) or [])
                    error_key = (event.get("step_index"), event.get("step_id"))
                    if not any(
                        (item.get("step_index"), item.get("step_id")) == error_key
                        for item in errors
                        if isinstance(item, dict)
                    ):
                        errors.append(
                            {
                                "step_index": event.get("step_index"),
                                "step_id": event.get("step_id"),
                                "model_calls": max(
                                    0, int(event.get("model_calls") or 0)
                                ),
                                "consumed_tokens": consumed_tokens,
                                "batch_metadata": event.get("batch_metadata") or [],
                            }
                        )
                    manifest[_EXECUTION_ERRORS_KEY] = errors
                    run.reproducibility_manifest = manifest
                    run.total_tokens = total_tokens
                    await db.commit()
                elif event_type == "run_complete":
                    # Serialize completion with a concurrently recorded deviation.
                    await db.refresh(run, with_for_update=True)
                    run.status = RunStatus.COMPLETED.value
                    run.total_tokens = total_tokens
                    run.completed_at = datetime.now(timezone.utc)
                    previous_manifest = dict(run.reproducibility_manifest or {})
                    previous_manifest.pop(_CONTINUATION_REQUESTED_KEY, None)
                    previous_manifest.pop("resume_authorization", None)
                    previous_manifest.pop(_PAUSE_REQUESTED_KEY, None)
                    previous_manifest.pop("user_pause", None)
                    if isinstance(previous_manifest.get("verification_override"), dict):
                        previous_manifest["final_status"] = "unverified"
                    if run.conformance_status == "plan_verified":
                        run.conformance_status = "conformant"
                    run.reproducibility_manifest = {
                        **previous_manifest,
                        "run_id": str(run.id),
                        "protocol_version_id": str(run.protocol_version_id),
                        "effective_plan_hash": run.effective_plan_hash,
                        "blueprint_id": approved_plan["blueprint_id"],
                        "blueprint_version": approved_plan["blueprint_version"],
                        "total_tokens": total_tokens,
                        "completed_at": run.completed_at.isoformat(),
                        "parameters_override": parameter_overrides,
                        "parameters": effective_parameters,
                    }
                    await db.commit()

                event_type = event.get("event", "message")
                data = json.dumps(event)
                yield f"event: {event_type}\ndata: {data}\n\n"

                if event_type == "step_error" and pause_requested:
                    descriptor = await lifecycle.persist_user_pause(
                        run=run,
                        step_index=pause_anchor_index,
                        output_hash=pause_anchor_hash,
                        total_tokens=total_tokens,
                    )
                    committed_followup = {
                        "event": "run_paused",
                        "run_id": str(run.id),
                        **descriptor.to_dict(),
                    }
                if committed_followup is not None:
                    followup_type = committed_followup["event"]
                    data = json.dumps(committed_followup)
                    yield f"event: {followup_type}\ndata: {data}\n\n"
                    return
        except asyncio.CancelledError:
            # Client disconnected; persist paused state so run can resume later.
            await _run_interrupted_cleanup(recover_run(RunStatus.PAUSED.value))
            raise
        except Exception:
            # If streaming fails unexpectedly, mark run as failed
            logger.exception("Research stream failed for run %s", run_id)
            safely_observe(
                research_observability,
                "record_sse_error",
                run_id=run_id,
                organization_id=organization_id,
                error_kind="stream_failure",
            )
            try:
                await recover_run(RunStatus.FAILED.value, completed=True)
            except Exception:
                logger.error(
                    "Failed to persist terminal state for research run %s",
                    run_id,
                )
            error_event = json.dumps(
                {
                    "event": "run_failed",
                    "error": "Research stream failed",
                    "error_category": "stream_failure",
                }
            )
            yield f"event: run_failed\ndata: {error_event}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
