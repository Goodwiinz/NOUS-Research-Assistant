"""Agent execution endpoint.

FastAPI route definitions for the agent API. Everything without HTTP
concerns lives in the service layer (audit B1/B5):

- src/services/agent/tools_impl.py    — _tool_* functions, execute_tool, AGENT_TOOLS
- src/services/agent/tool_helpers.py  — _resolve_document_id, _verify_project_ownership, etc.
- src/services/agent/agent_execution_service.py — job store access,
  _run_agent_graph/_resume_agent_graph, thread resolution, message persistence
- src/services/agent/schemas.py       — execute/response wire models (re-exported here)
- streaming.py (sibling)              — SSE event generators for /stream and /stream/confirm
"""

import logging
import time
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
)
from fastapi.responses import Response, StreamingResponse
from langgraph.errors import GraphInterrupt  # noqa: F401  re-export for backward compat
from pydantic import BaseModel, Field
from sqlalchemy import cast, desc, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.database import get_db
from src.core.dependencies import get_current_user, require_admin
from src.core.rate_limit import create_rate_limiter
from src.models.chat_message import ChatMessage, MessageRole
from src.models.conversation import Conversation
from src.models.document import Document
from src.models.thread import Thread, ThreadStatus
from src.models.user import User
from src.models.workspace import Workspace
from src.services.agent import stream_buffer as _stream_buffer
from src.services.agent._pii_redact import redact_tool_executions
from src.services.agent.agent_execution_service import (  # noqa: F401
    MAX_JOBS,
    AgentThreadResolutionError,
    _actor_fields,
    _cleanup_jobs,
    _clear_stale_pending_confirmation,
    _get_job,
    _get_latest_user_content,
    _jobs,
    _jobs_lock,
    _page_context_to_dict,
    _resolve_thread,
    _resume_agent_graph,
    _run_agent_graph,
    _set_job,
)
from src.services.agent.agent_run_service import (
    claim_awaiting_run_for_confirmation,
    get_active_run_for_thread,
    get_run,
    release_confirmation_claim,
)
from src.services.agent.agent_submission_service import (
    abandon_awaiting_submission,
    request_run_cancellation,
)

# Wire models moved to the service layer (audit B5) so the graph runner can
# build them without importing src.api. Re-exported here so every existing
# `from src.api.agent.execute import <schema>` keeps resolving.
from src.services.agent.schemas import (  # noqa: F401
    SUPPORTED_MODELS,
    AgentExecuteRequest,
    AgentExecuteResponse,
    AgentMessage,
    PageContextRequest,
    RetrievedContextResponse,
    StrictUUIDString,
    ToolExecutionResponse,
)
from src.services.agent.tool_helpers import (  # noqa: F401
    _resolve_document_id,
    _resolve_project_id,
    _sanitize_metadata,
    _verify_project_ownership,
)

# Re-export from the canonical service modules so existing imports keep
# working. Every `from src.api.agent.execute import <name>` must resolve.
from src.services.agent.tools_impl import (  # noqa: F401
    AGENT_TOOLS,
    _tool_add_document_to_project,
    _tool_compare_documents,
    _tool_create_draft,
    _tool_create_project,
    _tool_create_project_note,
    _tool_do_kb_retrieve,
    _tool_execute_code,
    _tool_explore_entity_neighborhood,
    _tool_export_bibliography,
    _tool_extract_entities,
    _tool_find_entity_paths,
    _tool_get_graph_stats,
    _tool_ingest_arxiv,
    _tool_list_external_databases,
    _tool_list_project_documents,
    _tool_list_projects,
    _tool_search_arxiv,
    _tool_search_documents,
    _tool_search_external_database,
    _tool_search_knowledge_graph,
    _tool_summarize_document,
    execute_tool,
)
from src.shared.enums import AgentStreamEvent, JobStatus

from .streaming import (  # noqa: F401
    _SSE_HEADERS,
    format_stream_envelope_frame,
    replay_buffered_stream,
    stream_confirm_event_generator,
    stream_event_generator,
)

# Per-user rate limiter for agent execute/stream endpoints.
# 30 requests per minute — adjust MAX_AGENT_RPM / AGENT_RATE_WINDOW_MINUTES via
# env/config if operational needs change.  Uses Redis when available, falls back
# to InMemoryRateLimiter (not suitable for multi-worker prod without Redis).
# NOTE: this is a minimal in-process guard; a proper solution should wire into
# the AnalyticsRateLimitMiddleware or a dedicated Redis-backed dependency that
# survives worker restarts and load-balanced deployments.
_AGENT_RATE_LIMIT_RPM = 30
_AGENT_RATE_WINDOW_MINUTES = 1
_agent_rate_limiter = create_rate_limiter(
    max_attempts=_AGENT_RATE_LIMIT_RPM,
    window_minutes=_AGENT_RATE_WINDOW_MINUTES,
)

# R7-M6: turn creation is ONE per-user budget. /execute, /stream and both
# confirm/resume-a-turn endpoints previously had a bucket each, so a caller
# could start 4x30 turns a minute by rotating endpoints. Reads (job polling,
# SSE resume, cancel) are cheap and poll-heavy, so they get their own, larger
# bucket rather than sharing the turn budget.
_AGENT_TURN_PREFIX = "agent_turn"
_AGENT_READ_PREFIX = "agent_read"
_AGENT_READ_RPM = 120
_agent_read_rate_limiter = create_rate_limiter(
    max_attempts=_AGENT_READ_RPM,
    window_minutes=_AGENT_RATE_WINDOW_MINUTES,
)

logger = logging.getLogger(__name__)


async def _enforce_rate_limit(limiter: Any, user_id: str, prefix: str) -> None:
    """429 when *user_id* is over *prefix*'s budget, else record the attempt."""
    allowed, retry_after = await limiter.check_rate_limit(user_id, prefix=prefix)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Retry after {retry_after}s.",
        )
    await limiter.record_attempt(user_id, prefix=prefix)


router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


def _validate_confirmable_job(job: dict, current_user: User) -> None:
    """Raise the public API error for jobs the caller cannot confirm."""
    job_user_id = job.get("user_id")
    if not job_user_id or job_user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") != JobStatus.AWAITING_CONFIRMATION:
        raise HTTPException(status_code=409, detail="Job is not awaiting confirmation")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
#
# AgentMessage / PageContextRequest / SUPPORTED_MODELS / AgentExecuteRequest /
# RetrievedContextResponse / ToolExecutionResponse / AgentExecuteResponse
# moved to src/services/agent/schemas.py (re-exported above). Only the
# router-local schemas remain here.


class JobStartResponse(BaseModel):
    job_id: str


class JobStatusResponse(BaseModel):
    # Typed wire contract (audit C7): every status the backend can return is a
    # JobStatus member; the legacy "error" alias is normalized to FAILED
    # before this model is built (see get_job_status).
    status: JobStatus
    result: Optional[dict] = None
    tool_executions: Optional[List[dict]] = None
    error: Optional[str] = None
    confirmation: Optional[dict] = None
    thread_id: Optional[str] = None


class HTTPErrorResponse(BaseModel):
    detail: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

AGENT_THREAD_MARKER = {"source": "agent"}


class ConfirmationRequest(BaseModel):
    confirmed: bool = Field(..., description="Whether the user confirms the action")


class StreamConfirmRequest(BaseModel):
    thread_id: StrictUUIDString
    confirmed: bool


class StreamCancelRequest(BaseModel):
    expected_run_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="The active run the caller intends to stop",
    )


_SSE_RESPONSE = {
    200: {
        "description": "Server-Sent Events stream",
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
    }
}


# ---------------------------------------------------------------------------
# Dispatch backends (audit P1.3, X1 dispatch half)
# ---------------------------------------------------------------------------

_DISPATCH_BACKENDS = frozenset({"background", "celery"})


def _resolve_dispatch_backend() -> str:
    """Read AGENT_DISPATCH_BACKEND per-call (values-flippable, no restart).

    Unknown values degrade to ``"background"`` with a warning instead of
    failing the request — a typo in a values file must not take down /execute.
    """
    from src.core.config import get_settings

    raw = (get_settings().AGENT_DISPATCH_BACKEND or "background").strip().lower()
    if raw not in _DISPATCH_BACKENDS:
        logger.warning(
            "Unknown AGENT_DISPATCH_BACKEND %r — falling back to 'background'", raw
        )
        return "background"
    return raw


def _client_idempotency_key(request: "AgentExecuteRequest", current_user: User):
    """Idempotency key for a dispatch, derived from the newest user turn.

    Scoped by user id so the globally-unique partial index on
    ``agent_runs.idempotency_key`` can never collide across tenants. ``None``
    when the client sent no ``client_message_id`` (legacy clients) — those
    requests dispatch unconditionally, exactly like today.
    """
    last = next((m for m in reversed(request.messages) if m.role == "user"), None)
    cmid = getattr(last, "client_message_id", None) if last is not None else None
    if cmid is None:
        return None
    return f"agent-execute:{current_user.id}:{cmid}"


async def _celery_dispatch(
    job_id: str,
    job_payload: dict,
    request: "AgentExecuteRequest",
    current_user: User,
) -> tuple[str, str]:
    """Dispatch the turn to the Celery ``agent_runs`` queue.

    Returns ``(outcome, job_id_for_client)`` with outcome one of:

    - ``"dispatched"`` — row committed, job record written, task enqueued.
    - ``"dedup"``     — a run for this idempotency key already exists; the
      existing job_id is returned and NOTHING new is enqueued (retry of the
      same turn resolves to the original run).
    - ``"conflict"``  — another non-terminal run owns the thread.
    - ``"unavailable"`` — the writer slot could not be checked for a
      thread-scoped request, so execution fails closed.
    - ``"failed"``    — the enqueue itself failed AFTER the durable writes;
      the job is marked failed in both stores so the poller stops cleanly.
      We deliberately do NOT fall back to in-process execution here: the
      broker exception is ambiguous (the message may have been published),
      and running the turn in-process next to a possibly-delivered task
      would double-execute it.
    - ``"fallback"``  — the durable row could not be written, so nothing was
      enqueued and the caller may safely run the turn in-process instead.

    Ordering is the whole point (repo orphan-state lesson: flush-before-
    external): the ``agent_runs`` row commits FIRST, then the Redis job
    record, and the broker publish happens strictly last. A crash between
    the commit and the publish leaves a row the sweeper reaps — never a
    running task without a row (which would be unclaimable and unsweepable).
    """
    from src.core.database import AsyncSessionLocal
    from src.services.agent import agent_run_service

    org = getattr(current_user, "organization_id", None)
    idem_key = _client_idempotency_key(request, current_user)

    # 1. Durable agent_runs row (+ idempotency key) FIRST.
    try:
        async with AsyncSessionLocal() as run_db:
            run = await agent_run_service.upsert_run(
                run_db,
                job_id=job_id,
                status=JobStatus.QUEUED,
                organization_id=org,
                user_id=current_user.id,
                thread_id=request.thread_id,
                idempotency_key=idem_key,
            )
            if run is None and idem_key is not None:
                existing = await agent_run_service.get_run_by_idempotency_key(
                    run_db,
                    idem_key,
                    organization_id=org,
                    user_id=current_user.id,
                )
                if existing is not None:
                    logger.info(
                        "celery dispatch: idempotency key already dispatched — "
                        "returning existing job %s (requested %s)",
                        existing.job_id,
                        job_id,
                    )
                    return "dedup", existing.job_id
            if run is None:
                logger.warning(
                    "celery dispatch: agent_runs row not created for job %s; "
                    "falling back to in-process dispatch",
                    job_id,
                )
                return "fallback", job_id
    except agent_run_service.ActiveRunConflict:
        return "conflict", job_id
    except Exception:
        logger.warning(
            "celery dispatch: agent_runs row write failed for job %s; "
            "cannot establish the thread writer slot",
            job_id,
            exc_info=True,
        )
        return ("unavailable" if request.thread_id else "fallback"), job_id

    # 2. Job record for pollers (L1 + Redis + projection).
    _set_job(
        job_id,
        {**job_payload, "status": JobStatus.QUEUED},
        project=False,
    )

    # 3. Enqueue LAST — the external call happens only after all state is
    #    durable, so the worker's execution claim always finds its row.
    try:
        from src.tasks.agent_run_tasks import run_agent_job

        run_agent_job.delay(
            job_id=job_id,
            request_payload=request.model_dump(mode="json"),
            user_id=str(current_user.id),
        )
    except Exception:
        logger.exception(
            "celery dispatch: enqueue failed for job %s — marking failed", job_id
        )
        error = "Agent dispatch failed (task queue unavailable). Please retry."
        _set_job(
            job_id,
            {
                "status": JobStatus.FAILED,
                "error": error,
                "tool_executions": [],
                **_actor_fields(current_user),
            },
        )
        # Durable projection write (await — the fire-and-forget projection
        # scheduled by _set_job is best-effort; this one must land so the
        # sweeper never resurrects the orphan as "stale running").
        await agent_run_service.record_job_status(
            job_id,
            {"status": JobStatus.FAILED, "error": error, **_actor_fields(current_user)},
        )
        return "failed", job_id

    return "dispatched", job_id


@router.post("/execute", response_model=JobStartResponse)
async def execute_agent(
    request: AgentExecuteRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Execute an agent chat completion via LangGraph.

    Returns a job ID immediately.  Poll ``GET /jobs/{job_id}`` for the result.

    Both dispatch modes commit a durable ``agent_runs`` row before execution.
    ``background`` runs the graph on this pod via FastAPI BackgroundTasks;
    ``celery`` enqueues it to the dedicated agent_runs queue.
    """
    await _enforce_rate_limit(
        _agent_rate_limiter, str(current_user.id), _AGENT_TURN_PREFIX
    )
    logger.info(
        "Agent execute request",
        extra={
            "user_id": str(current_user.id),
            "page_context": request.page_context.type,
            "message_count": len(request.messages),
            "use_rag": request.use_rag,
            "dispatch_backend": _resolve_dispatch_backend(),
        },
    )

    try:
        thread, _conversation_id = await _resolve_thread(db, current_user, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid thread ID") from exc
    except AgentThreadResolutionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if thread is not None:
        request.thread_id = str(thread.id) if thread is not None else None

    job_id = str(_uuid.uuid4())
    job_payload = {
        "status": JobStatus.RUNNING,
        "tool_executions": [],
        **_actor_fields(current_user),
        "request": request.model_dump(mode="json"),
    }

    if _resolve_dispatch_backend() == "celery":
        outcome, dispatched_job_id = await _celery_dispatch(
            job_id, job_payload, request, current_user
        )
        if outcome == "conflict":
            raise HTTPException(
                status_code=409,
                detail="A response is already in progress for this thread.",
            )
        if outcome == "unavailable":
            raise HTTPException(
                status_code=503,
                detail="Unable to reserve this thread. Please retry.",
            )
        if outcome != "fallback":
            return JobStartResponse(job_id=dispatched_job_id)
        # "fallback": nothing was enqueued and no job record written — safe
        # to run in-process below, exactly as if the flag were "background".

    from src.services.agent import agent_run_service

    idem_key = _client_idempotency_key(request, current_user)
    persistence_error = (
        "Unable to reserve this thread. Please retry."
        if request.thread_id
        else "Unable to persist this run. Please retry."
    )
    try:
        run = await agent_run_service.upsert_run(
            db,
            job_id=job_id,
            status=JobStatus.QUEUED,
            organization_id=getattr(current_user, "organization_id", None),
            user_id=current_user.id,
            thread_id=request.thread_id,
            idempotency_key=idem_key,
        )
        if run is None and idem_key is not None:
            existing = await agent_run_service.get_run_by_idempotency_key(
                db,
                idem_key,
                organization_id=getattr(current_user, "organization_id", None),
                user_id=current_user.id,
            )
            if existing is not None:
                return JobStartResponse(job_id=existing.job_id)
    except agent_run_service.ActiveRunConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="A response is already in progress for this thread.",
        ) from exc
    except Exception as exc:
        try:
            await db.rollback()
        except Exception:
            logger.warning("Failed to roll back agent run reservation", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail=persistence_error,
        ) from exc
    if run is None:
        raise HTTPException(
            status_code=503,
            detail=persistence_error,
        )

    _set_job(job_id, job_payload)

    background_tasks.add_task(
        _run_agent_graph,
        job_id,
        request,
        current_user,
    )
    return JobStartResponse(job_id=job_id)


def _normalized_job_status(raw: object) -> JobStatus:
    """Coerce a stored job status to the typed wire contract.

    Maps the legacy ``"error"`` alias to FAILED (one-release transition) and
    degrades an unknown/corrupted value to FAILED with a log instead of a
    response-validation 500 — pollers must always be able to stop.
    """
    try:
        return JobStatus(raw)
    except ValueError:
        logger.warning("Unknown job status %r in stored record", raw)
        return JobStatus.FAILED


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    job_id: str = Path(pattern=r"^[0-9a-fA-F-]{36}$"),
    current_user: User = Depends(get_current_user),
):
    """Poll for agent job status — L1 cache, then Redis, then Postgres.

    The Postgres ``agent_runs`` projection is the failover path: when Redis
    lost the record (failover/TTL) the poller previously got a hard 404 and
    the run became untrackable (audit X1/D7). The projection carries only
    status + error — result payloads still require the Redis record.
    """
    await _enforce_rate_limit(
        _agent_read_rate_limiter, str(current_user.id), _AGENT_READ_PREFIX
    )
    # L1 is only trustworthy for terminal records (immutable). A non-terminal
    # L1 entry may be a stale seed while another PROCESS owns the run's writes
    # (Celery dispatch mode, multi-replica API) — without the fresh read the
    # poller would see "running" until the 1h TTL. get_job_fresh degrades to
    # the L1 read when Redis is unavailable, so single-process behavior (and
    # Redis-less tests) are unchanged.
    job = _get_job(job_id)
    if job is None or not _normalized_job_status(job.get("status")).is_terminal:
        from src.services.agent.job_store import get_job_fresh as _get_job_fresh

        job = (await _get_job_fresh(job_id)) or job
    if not job:
        # Redis miss: fall back to the durable projection (tenancy-filtered —
        # org + user must both match; a miss 404s without confirming existence).
        from src.services.agent import agent_run_service

        run = await agent_run_service.get_run_fallback(
            job_id,
            organization_id=getattr(current_user, "organization_id", None),
            user_id=current_user.id,
        )
        if run is None:
            raise HTTPException(status_code=404, detail="Job not found")
        run_thread_id = getattr(run, "thread_id", None)
        return JobStatusResponse(
            status=_normalized_job_status(run.status),
            error=run.error,
            thread_id=str(run_thread_id) if run_thread_id else None,
        )
    # Fail closed: a job record without an owner must not be readable. Every
    # write path stamps user_id; its absence means a corrupted/legacy record,
    # not a public one.
    if job.get("user_id") != str(current_user.id):
        raise HTTPException(status_code=404, detail="Job not found")
    request = job.get("request")
    thread_id = job.get("thread_id") or (
        request.get("thread_id") if isinstance(request, dict) else None
    )
    return JobStatusResponse(
        **{
            **job,
            "status": _normalized_job_status(job.get("status")),
            "thread_id": thread_id,
        }
    )


@router.post("/confirm/{job_id}")
async def confirm_agent_action(
    job_id: str,
    request: ConfirmationRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Confirm or deny a pending agent action (human-in-the-loop).

    Works identically in both dispatch modes: the graph re-enters via the
    Postgres checkpointer (keyed by thread_id), which any API pod can reach,
    so the resume itself runs here as a BackgroundTask BY DESIGN even when
    the original turn executed on a Celery worker (see agent_run_tasks).
    """
    await _enforce_rate_limit(
        _agent_rate_limiter, str(current_user.id), _AGENT_TURN_PREFIX
    )
    from src.services.agent.job_store import (
        ConfirmationCoordinationUnavailable,
        compare_and_set_status,
    )
    from src.services.agent.job_store import get_job_fresh as _get_job_fresh

    async def rollback_confirmation_session(context: str) -> None:
        try:
            await db.rollback()
        except Exception:
            logger.warning(
                "Failed to roll back confirmation %s",
                context,
                exc_info=True,
            )

    # Read the job Redis-first for the friendly 404 + ownership/status check.
    # In Celery dispatch mode the awaiting_confirmation write came from the
    # worker process, so this pod's L1 may still hold the stale "running"
    # dispatch record — trusting it would 409 every legitimate confirm.
    # (The authoritative claim is the guarded PostgreSQL transition below.)
    job = await _get_job_fresh(job_id)
    stored_request_payload = job.get("request") if isinstance(job, dict) else None
    # ``get_job_fresh`` returns None only when BOTH Redis and L1 have nothing —
    # exactly the two stores ``_resume_agent_graph`` reads for the request
    # payload. The projection fallback below can restore status and ownership,
    # never the payload, so this is the fail-closed signal for the guard.
    job_payload_missing = job is None
    job_from_projection = False
    if job is not None:
        # Fail closed before consulting another store: a live record owned by
        # someone else must never become a tenancy oracle through fallback.
        job_user_id = job.get("user_id")
        if not job_user_id or job_user_id != str(current_user.id):
            raise HTTPException(status_code=404, detail="Job not found")

    live_status = _normalized_job_status(job.get("status")) if job is not None else None
    if job is None or live_status != JobStatus.AWAITING_CONFIRMATION:
        from src.services.agent import agent_run_service

        try:
            run = await agent_run_service.get_run(
                db,
                job_id,
                organization_id=getattr(current_user, "organization_id", None),
                user_id=current_user.id,
            )
        except Exception as exc:
            await rollback_confirmation_session("projection read")
            raise HTTPException(
                status_code=503,
                detail="Confirmation is temporarily unavailable; please retry",
            ) from exc
        if run is not None:
            job_from_projection = True
            job = {
                "status": _normalized_job_status(run.status),
                "user_id": str(run.user_id),
                "request": stored_request_payload,
            }
        elif job is None:
            raise HTTPException(status_code=404, detail="Job not found")
    _validate_confirmable_job(job, current_user)

    # R7-L13: fail closed when the Redis job payload is gone. The payload
    # carries the original request — and with it the thread_id the resume
    # needs. Without it ``_resume_agent_graph`` used to fall back to
    # thread_id=job_id, read an empty snapshot, skip the ownership and
    # interrupt-consumed guards, and fail the run — after this endpoint had
    # already flipped it awaiting -> running. Refuse BEFORE the claim so the
    # run stays parked and a client that still holds the turn can retry.
    if job_payload_missing:
        logger.warning(
            "Confirm refused for %s: job payload expired (Redis miss)", job_id
        )
        raise HTTPException(
            status_code=409,
            detail="This confirmation has expired. Please start the request again.",
        )

    if stored_request_payload:
        try:
            stored_request = AgentExecuteRequest(**stored_request_payload)
            if stored_request.thread_id:
                await _resolve_thread(
                    db,
                    current_user,
                    stored_request,
                    create_if_missing=False,
                )
        except (AgentThreadResolutionError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Thread not found") from exc

    # PostgreSQL is the shared Stop/Confirm authority. Claim it before Redis so
    # cancellation and every resume path race on the same guarded transition.
    try:
        durable_claimed = await claim_awaiting_run_for_confirmation(
            db,
            job_id,
            organization_id=current_user.organization_id,
            user_id=current_user.id,
        )
    except Exception as exc:
        await rollback_confirmation_session("claim")
        raise HTTPException(
            status_code=503,
            detail="Confirmation is temporarily unavailable; please retry",
        ) from exc
    if not durable_claimed:
        raise HTTPException(status_code=409, detail="Job is not awaiting confirmation")

    async def release_durable_claim() -> None:
        try:
            await release_confirmation_claim(
                db,
                job_id,
                organization_id=current_user.organization_id,
                user_id=current_user.id,
            )
        except Exception:
            await rollback_confirmation_session("claim release")
            logger.exception("Failed to release confirmation claim for %s", job_id)

    # Mirror the durable claim into Redis. This remains the cross-worker job
    # payload coordinator, while PostgreSQL above decides Stop versus Confirm.
    try:
        result = await compare_and_set_status(
            job_id,
            JobStatus.AWAITING_CONFIRMATION,
            JobStatus.RUNNING,
            project=False,
        )
        if (
            result == "conflict"
            and job_from_projection
            and live_status in {JobStatus.QUEUED, JobStatus.RUNNING}
        ):
            # The worker may have parked the durable run successfully while
            # its Redis running and/or awaiting-confirmation writes failed.
            # PostgreSQL is now the authoritative winner; claim the exact
            # known-stale live state atomically so a delayed awaiting write or
            # terminal transition still conflicts.
            result = await compare_and_set_status(
                job_id, live_status, JobStatus.RUNNING, project=False
            )
    except ConfirmationCoordinationUnavailable as exc:
        await release_durable_claim()
        raise HTTPException(
            status_code=503,
            detail="Confirmation is temporarily unavailable; please retry",
        ) from exc
    if result == "missing":
        await release_durable_claim()
        if job_from_projection:
            raise HTTPException(
                status_code=503,
                detail="Confirmation is temporarily unavailable; please retry",
            )
        raise HTTPException(status_code=404, detail="Job not found")
    if result == "conflict":
        # PostgreSQL was claimed first, so a Redis conflict is a stale mirror,
        # not another durable winner. Put the run back so a retry can recover.
        await release_durable_claim()
        raise HTTPException(status_code=409, detail="Job is not awaiting confirmation")

    # Winner: keep the local L1 view consistent, then resume.
    with _jobs_lock:
        cached = _jobs.get(job_id)
        if cached is not None:
            cached["status"] = JobStatus.RUNNING

    background_tasks.add_task(
        _resume_agent_graph,
        job_id,
        request.confirmed,
        current_user,
    )
    return {"status": JobStatus.RUNNING, "job_id": job_id}


@router.post(
    "/stream",
    response_class=StreamingResponse,
    responses={
        **_SSE_RESPONSE,
        429: {"model": HTTPErrorResponse, "description": "Rate limit exceeded"},
    },
)
async def stream_agent(
    request_body: AgentExecuteRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """Stream agent responses via Server-Sent Events.

    SSE event types are the ``AgentStreamEvent`` wire vocabulary
    (``src/shared/enums.py`` — the single source of truth): token,
    reasoning_delta, tool_start, tool_end, rag_context, plan, reflection,
    trace, usage, heartbeat, status, confirmation, done, error. The terminal
    frames are done, error, or confirmation.
    """
    # Stamp the accepted-latency SLI clock on handler entry. Rate limiting,
    # body parsing, and StreamingResponse setup all cost the client wall time
    # before the generator builds its emitter, so starting the clock there
    # reports a latency that excludes the overhead the SLI exists to surface.
    request_started_at = time.monotonic()
    await _enforce_rate_limit(
        _agent_rate_limiter, str(current_user.id), _AGENT_TURN_PREFIX
    )
    return StreamingResponse(
        stream_event_generator(
            request_body,
            request,
            current_user,
            background_tasks=background_tasks,
            request_started_at=request_started_at,
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
        background=background_tasks,
    )


@router.post(
    "/stream/confirm",
    response_class=StreamingResponse,
    responses={
        **_SSE_RESPONSE,
        429: {"model": HTTPErrorResponse, "description": "Rate limit exceeded"},
    },
)
async def stream_confirm_agent(
    request_body: StreamConfirmRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """Resume a graph interrupted by HITL via SSE streaming."""
    await _enforce_rate_limit(
        _agent_rate_limiter, str(current_user.id), _AGENT_TURN_PREFIX
    )
    return StreamingResponse(
        stream_confirm_event_generator(
            request_body,
            request,
            current_user,
            background_tasks=background_tasks,
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post(
    "/stream/cancel/{thread_id}",
    status_code=204,
    responses={
        404: {"model": HTTPErrorResponse, "description": "Thread not found"},
        409: {
            "model": HTTPErrorResponse,
            "description": "Run is no longer the expected active run",
        },
        503: {
            "model": HTTPErrorResponse,
            "description": "Cancellation temporarily unavailable",
        },
    },
)
async def cancel_stream_confirmation(
    thread_id: _uuid.UUID,
    request: Request,
    cancellation: Optional[StreamCancelRequest] = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Request durable cancellation of a caller-owned stream run.

    An omitted body preserves the parked-confirmation endpoint contract. A
    body identifies a normal running turn; its producer must observe the
    ``stopping`` marker and acknowledge cancellation before the response can
    become terminal.
    """
    await _enforce_rate_limit(
        _agent_read_rate_limiter, str(current_user.id), _AGENT_READ_PREFIX
    )
    ownership_stmt = (
        select(Thread)
        .join(Conversation, Thread.conversation_id == Conversation.id)
        .join(Workspace, Conversation.workspace_id == Workspace.id)
        .where(
            Thread.id == thread_id,
            Workspace.owner_id == current_user.id,
            Thread.is_deleted == False,
        )
    )
    if (await db.execute(ownership_stmt)).scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    active = await get_active_run_for_thread(
        db,
        thread_id,
        organization_id=current_user.organization_id,
        user_id=current_user.id,
    )
    if active is None:
        # A body carrying an exact identity is an acknowledged cancellation
        # command, rather than the legacy parked-confirmation probe. Preserve
        # the distinction between a repeated stop of an already-cancelled run
        # (idempotent success) and a late stop after completion (conflict).
        expected_run_id = cancellation.expected_run_id if cancellation else None
        if cancellation is not None and expected_run_id is not None:
            terminal = await get_run(
                db,
                expected_run_id,
                organization_id=current_user.organization_id,
                user_id=current_user.id,
            )
            if terminal is not None and terminal.status != JobStatus.CANCELLED.value:
                raise HTTPException(status_code=409, detail="Run is already complete")
        return Response(status_code=204)
    expected_run_id = cancellation.expected_run_id if cancellation else None
    if expected_run_id is not None and expected_run_id != active.job_id:
        raise HTTPException(status_code=409, detail="Run is no longer active")
    if (
        cancellation is not None
        and active.status != JobStatus.AWAITING_CONFIRMATION.value
    ):
        if expected_run_id is None:
            raise HTTPException(status_code=409, detail="Run identity is required")
        request_id = getattr(getattr(request, "state", None), "request_id", None)
        try:
            requested = await request_run_cancellation(
                db,
                run_id=expected_run_id,
                thread_id=thread_id,
                organization_id=current_user.organization_id,
                user_id=current_user.id,
                reason="user_requested",
                request_id=request_id,
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.exception("Failed to request cancellation of agent stream")
            raise HTTPException(
                status_code=503,
                detail="Cancellation is temporarily unavailable; please retry",
            ) from exc
        if requested is None:
            # The exact run disappeared between the active lookup and claim;
            # a newer owner must never be cancelled by this stale request.
            raced = await get_active_run_for_thread(
                db,
                thread_id,
                organization_id=current_user.organization_id,
                user_id=current_user.id,
            )
            if raced is not None and raced.job_id != expected_run_id:
                raise HTTPException(status_code=409, detail="Run is no longer active")
            return Response(status_code=204)
        if requested.status == JobStatus.AWAITING_CONFIRMATION:
            raise HTTPException(
                status_code=409, detail="Run is already awaiting confirmation"
            )
        if requested.status.is_terminal:
            if requested.status != JobStatus.CANCELLED:
                raise HTTPException(status_code=409, detail="Run is already complete")
            return Response(status_code=204)
        return Response(status_code=204)
    if active.status != JobStatus.AWAITING_CONFIRMATION.value:
        raise HTTPException(status_code=409, detail="Run is not awaiting confirmation")

    try:
        abandoned = await abandon_awaiting_submission(
            db,
            thread_id=thread_id,
            organization_id=current_user.organization_id,
            user_id=current_user.id,
            reason="user_stopped_confirmation",
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.exception("Failed to cancel pending agent confirmation")
        raise HTTPException(
            status_code=503,
            detail="Cancellation is temporarily unavailable; please retry",
        ) from exc
    if abandoned is None:
        # Confirmation may have won the guarded awaiting->running transition
        # after our first read. Never report a successful Stop while it runs.
        raced = await get_active_run_for_thread(
            db,
            thread_id,
            organization_id=current_user.organization_id,
            user_id=current_user.id,
        )
        if raced is not None:
            raise HTTPException(status_code=409, detail="Run is already executing")
        return Response(status_code=204)

    # Keep the Redis/L1 confirmation gate aligned with the durable winner. If
    # Redis is unavailable the PostgreSQL claim still makes every resume fail
    # closed, so this mirror remains best-effort.
    try:
        from src.services.agent.job_store import compare_and_set_status

        await compare_and_set_status(
            abandoned,
            JobStatus.AWAITING_CONFIRMATION,
            JobStatus.CANCELLED,
            project=False,
        )
    except Exception:
        logger.warning(
            "Cancelled HITL run but could not mirror job-store state for %s",
            abandoned,
            exc_info=True,
        )

    # The durable terminal write above is authoritative. Checkpoint cleanup is
    # best-effort; a fresh turn repeats it before invoking the graph.
    try:
        from src.services.agent.checkpointer import get_checkpointer
        from src.services.agent.graph import compile_agent_graph
        from src.services.agent.memory import get_memory_store

        graph = compile_agent_graph(
            checkpointer=await get_checkpointer(),
            store=await get_memory_store(),
        )
        await _clear_stale_pending_confirmation(
            graph,
            {
                "configurable": {
                    "thread_id": str(thread_id),
                    "user_id": str(current_user.id),
                    "organization_id": str(current_user.organization_id or ""),
                }
            },
        )
    except Exception:
        logger.warning(
            "Cancelled HITL run but could not clear checkpoint for thread %s",
            str(thread_id),
            exc_info=True,
        )
    return Response(status_code=204)


@router.get("/graph/mermaid")
async def get_graph_mermaid(
    current_user: User = Depends(require_admin),
):
    """Get the agent graph structure as a Mermaid diagram. Admin-only."""
    from src.services.agent.visualization import get_graph_mermaid

    diagram = get_graph_mermaid()
    return {"mermaid": diagram}


@router.get("/graph/trace/{thread_id}")
async def get_graph_trace(
    thread_id: str = Path(pattern=r"^[0-9a-fA-F-]{36}$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get an execution trace for a thread as a Mermaid sequence diagram."""
    try:
        thread_uuid = _uuid.UUID(thread_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid thread_id")

    # Verify the thread exists and belongs to the current user before
    # exposing any execution trace data (IDOR guard).
    ownership_stmt = (
        select(Thread)
        .join(Conversation, Thread.conversation_id == Conversation.id)
        .join(Workspace, Conversation.workspace_id == Workspace.id)
        .where(
            Thread.id == thread_uuid,
            Workspace.owner_id == current_user.id,
            Thread.is_deleted == False,
        )
    )
    thread_row = (await db.execute(ownership_stmt)).scalar_one_or_none()
    if thread_row is None:
        raise HTTPException(status_code=404, detail="Trace not found")

    from src.services.agent.visualization import get_execution_trace_mermaid

    diagram = await get_execution_trace_mermaid(thread_id)
    if diagram is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {"mermaid": diagram, "thread_id": thread_id}


async def _single_frame(frame: str):
    """One-frame SSE body."""
    yield frame


async def _pending_confirmation_frame(
    thread_id: str, current_user: User, *, after: int = 0
) -> Optional[str]:
    """SSE ``confirmation`` frame when the graph is parked on a HITL interrupt.

    Reads the checkpoint directly rather than the stream buffer: the buffer's
    active pointer is cleared the moment a stream ends, while the interrupt
    outlives it and is only resolved by ``/confirm`` or discarded by the next
    turn. Detection mirrors ``streaming.py`` — ``aget_state`` plus
    ``snapshot.tasks[*].interrupts`` — because with a checkpointer attached
    ``interrupt()`` returns state rather than raising ``GraphInterrupt``.

    Best-effort: any failure returns None so resume degrades to its previous
    204 instead of failing the request.

    The frame goes through the shared envelope builder, so it carries the same
    schema_version / sequence / event_id / occurred_at / trace_id / thread_id /
    route fields as every live-stream frame and, critically, an ``id:`` line —
    without one the client's Last-Event-ID cursor never advances past this
    frame and a reconnect replays from a stale position.
    """
    try:
        from src.services.agent.checkpointer import get_checkpointer
        from src.services.agent.graph import compile_agent_graph
        from src.services.agent.memory import get_memory_store

        checkpointer = await get_checkpointer()
        store = await get_memory_store()
        graph = compile_agent_graph(checkpointer=checkpointer, store=store)

        config = {
            "configurable": {
                "thread_id": thread_id,
                "user_id": str(current_user.id),
                "organization_id": str(
                    getattr(current_user, "organization_id", "") or ""
                ),
            }
        }
        snapshot = await graph.aget_state(config)
        if snapshot is None:
            return None

        confirmation: Dict[str, Any] = {}
        for task in snapshot.tasks or ():
            for intr in getattr(task, "interrupts", ()) or ():
                confirmation = getattr(intr, "value", {}) or {}
                break
            if confirmation:
                break
        if not confirmation:
            return None

        payload = {"thread_id": thread_id, "confirmation": confirmation}
        logger.info(
            "Re-delivering pending HITL confirmation on resume for thread %s",
            thread_id,
        )
        # Seq continues from the cursor the client sent (``after``) so echoing
        # this frame's id back as Last-Event-ID can only move the cursor
        # forward — the run this interrupt belongs to is over, so there is no
        # live buffer to stay in lockstep with.
        return format_stream_envelope_frame(
            AgentStreamEvent.CONFIRMATION,
            payload,
            seq=after + 1,
            thread_id=thread_id,
            route="graph",
        )
    except Exception:
        logger.warning(
            "Failed to check for a pending confirmation on resume for thread %s",
            thread_id,
            exc_info=True,
        )
        return None


@router.get("/stream/resume/{thread_id}")
async def resume_stream(
    request: Request,
    thread_id: str = Path(pattern=r"^[0-9a-fA-F-]{36}$"),
    after: int = Query(0, ge=0),
    stream: Optional[str] = Query(
        default=None,
        pattern=r"^[0-9a-fA-F-]{36}$",
        description=(
            "Stream id the cursor belongs to (the envelope's stream_id). "
            "When set, resume refuses to attach the cursor to a different "
            "(newer) run on the same thread."
        ),
    ),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replay buffered SSE frames (seq > after) for the thread's active run."""
    await _enforce_rate_limit(
        _agent_read_rate_limiter, str(current_user.id), _AGENT_READ_PREFIX
    )
    if last_event_id is not None:
        try:
            after = int(last_event_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Last-Event-ID must be a non-negative integer sequence",
            ) from exc
        if after < 0:
            raise HTTPException(
                status_code=400,
                detail="Last-Event-ID must be a non-negative integer sequence",
            )
    try:
        thread_uuid = _uuid.UUID(thread_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid thread_id")

    # Same IDOR guard as get_graph_trace: thread must belong to the caller's
    # workspace; 404 (not 403) so we don't confirm another tenant's thread.
    ownership_stmt = (
        select(Thread)
        .join(Conversation, Thread.conversation_id == Conversation.id)
        .join(Workspace, Conversation.workspace_id == Workspace.id)
        .where(
            Thread.id == thread_uuid,
            Workspace.owner_id == current_user.id,
            Thread.is_deleted == False,
        )
    )
    thread_row = (await db.execute(ownership_stmt)).scalar_one_or_none()
    if thread_row is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    sid = await _stream_buffer.active_stream_id(thread_id)
    # Run correlation (codex audit CX1): the client's seq cursor is only
    # meaningful against the stream it was read from. If the caller names its
    # stream and a DIFFERENT run now owns the thread's active pointer, replaying
    # the new run's frames against the old cursor would skip or duplicate
    # frames — 204 tells the client its old run is over.
    if stream is not None and sid is not None and stream.lower() != sid.lower():
        return Response(status_code=204)
    if sid is None and stream is not None:
        # The active pointer is deliberately cleared after the terminal frame,
        # but the per-stream buffer remains for an hour. Verify the immutable
        # stream->thread mapping before replaying that just-finished stream.
        owner_thread_id = await _stream_buffer.thread_id_for_stream(stream)
        if owner_thread_id is None or owner_thread_id.lower() != thread_id.lower():
            return Response(status_code=204)
        sid = stream
    if sid is None:
        # No live stream — but the graph may still be parked on a HITL
        # interrupt. The confirmation frame was emitted on a stream that has
        # since ended, so a client that missed it (backgrounded tab, reconnect,
        # dropped frame) had no way to ever get it back: this returned 204
        # forever while the run sat waiting for an answer. The user sees a
        # turn that produced nothing, re-sends, and the pending interrupt is
        # discarded as abandoned. Re-deliver it instead.
        frame = await _pending_confirmation_frame(thread_id, current_user, after=after)
        if frame is not None:
            return StreamingResponse(
                _single_frame(frame),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )
        return Response(status_code=204)

    return StreamingResponse(
        replay_buffered_stream(
            request,
            thread_id=thread_id,
            stream_id=sid,
            after=after,
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/health")
async def agent_health():
    """Health check for agent service."""
    return {"status": "ok", "service": "agent"}


# ---------------------------------------------------------------------------
# Thread listing & message retrieval schemas
# ---------------------------------------------------------------------------


class ThreadSummary(BaseModel):
    id: str
    title: Optional[str] = None
    created_at: str
    updated_at: str
    message_count: int
    last_message_at: Optional[str] = None
    source_project_id: Optional[str] = None
    status: str = "active"
    conversation_id: str = ""


class ThreadListResponse(BaseModel):
    threads: List[ThreadSummary]
    total: int


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    created_at: str
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    citations: Optional[List[Dict[str, Any]]] = None
    tool_executions: Optional[List[Dict[str, Any]]] = None
    # Per-turn agent provenance (assistant rows only; None for legacy rows).
    plan: Optional[List[Dict[str, Any]]] = None
    plan_reasoning: Optional[str] = None
    token_usage: Optional[Dict[str, int]] = None


class ThreadMessagesResponse(BaseModel):
    messages: List[MessageResponse]
    total: int
    has_more: bool = False


# ---------------------------------------------------------------------------
# Thread listing & message retrieval endpoints
# ---------------------------------------------------------------------------


@router.get("/threads", response_model=ThreadListResponse)
async def list_agent_threads(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List threads for the current user, ordered by most recently updated."""
    # Build query: Thread -> Conversation -> Workspace, filter by owner
    # Single query: a window count returns the (pre-limit) total alongside the
    # page, so we avoid firing a second full count query on every thread-list
    # load.
    stmt = (
        select(Thread, func.count().over().label("total"))
        .join(Conversation, Thread.conversation_id == Conversation.id)
        .join(Workspace, Conversation.workspace_id == Workspace.id)
        .where(
            Workspace.owner_id == current_user.id,
            Thread.is_deleted == False,
            Thread.rag_document_scope.contains(AGENT_THREAD_MARKER),
        )
        .order_by(desc(Thread.updated_at))
        .limit(50)
    )
    rows = (await db.execute(stmt)).all()
    threads = [row[0] for row in rows]
    total = rows[0][1] if rows else 0

    thread_summaries = []
    for t in threads:
        thread_summaries.append(
            ThreadSummary(
                id=str(t.id),
                title=t.title,
                created_at=t.created_at.isoformat() if t.created_at else "",
                updated_at=t.updated_at.isoformat() if t.updated_at else "",
                message_count=t.message_count or 0,
                last_message_at=(
                    t.last_message_at.isoformat() if t.last_message_at else None
                ),
                source_project_id=(
                    str(t.source_project_id) if t.source_project_id else None
                ),
                status=t.status.value if t.status else "active",
                conversation_id=str(t.conversation_id) if t.conversation_id else "",
            )
        )

    return ThreadListResponse(threads=thread_summaries, total=total)


@router.get("/threads/{thread_id}/messages", response_model=ThreadMessagesResponse)
async def get_thread_messages(
    thread_id: UUID,
    limit: Optional[int] = Query(
        None,
        ge=1,
        le=500,
        description="Max messages to return (most recent first). Omit for full history.",
    ),
    before: Optional[datetime] = Query(
        None,
        description=(
            "Return only messages created strictly before this time (ISO 8601), "
            "for loading older messages."
        ),
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get messages for a specific agent thread, verifying user ownership.

    With no query params this returns the full history (ascending) — unchanged.
    Pass ``limit`` to fetch the most recent N (and ``before`` to page older),
    with ``has_more`` signalling whether older messages remain.
    """
    # Verify thread exists and belongs to the current user via ownership chain
    ownership_stmt = (
        select(Thread)
        .join(Conversation, Thread.conversation_id == Conversation.id)
        .join(Workspace, Conversation.workspace_id == Workspace.id)
        .where(
            Thread.id == thread_id,
            Workspace.owner_id == current_user.id,
            Thread.is_deleted == False,
        )
    )
    ownership_result = await db.execute(ownership_stmt)
    thread = ownership_result.scalar_one_or_none()

    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Fetch messages with citations eagerly loaded.
    if limit is None and before is None:
        # Default (no params): full history ascending — unchanged behavior.
        messages_stmt = (
            select(ChatMessage)
            .where(
                ChatMessage.thread_id == thread_id,
                # Edit-and-resend tombstones: a superseded turn (and everything
                # after it) must never render, or a reload shows the answer to a
                # question the user replaced.
                ChatMessage.superseded_by_message_id.is_(None),
            )
            .options(selectinload(ChatMessage.citations))
            .order_by(ChatMessage.created_at.asc())
        )
        messages = (await db.execute(messages_stmt)).scalars().all()
        total = len(messages)
        has_more = False
    else:
        # Opt-in window: the most recent N (optionally older than ``before``),
        # then reversed to chronological order for the client. Backed by the
        # existing ix_chat_messages_thread_created (thread_id, created_at) index.
        eff_limit = limit or 50
        # Built once and reused for both statements below — a count query
        # built from its own copy of these filters silently drifts from the
        # page query the moment one of them changes (M12: the count used to
        # omit ``created_at < before``, so a ``before=`` page reported the
        # full-thread total instead of the filtered one).
        filters = [
            ChatMessage.thread_id == thread_id,
            ChatMessage.superseded_by_message_id.is_(None),
        ]
        if before is not None:
            filters.append(ChatMessage.created_at < before)
        page_stmt = (
            select(ChatMessage)
            .where(*filters)
            .options(selectinload(ChatMessage.citations))
            .order_by(ChatMessage.created_at.desc())
            .limit(eff_limit + 1)  # +1 sentinel to detect older messages
        )
        rows = (await db.execute(page_stmt)).scalars().all()
        has_more = len(rows) > eff_limit
        messages = list(reversed(rows[:eff_limit]))
        total = (
            await db.execute(select(func.count(ChatMessage.id)).where(*filters))
        ).scalar() or 0

    message_responses = []
    for msg in messages:
        # Build citations list from the relationship
        citations_data = None
        if msg.citations:
            citations_data = [c.to_frontend_format() for c in msg.citations]

        message_responses.append(
            MessageResponse(
                id=str(msg.id),
                role=msg.role.value if msg.role else "user",
                content=msg.content or "",
                created_at=msg.created_at.isoformat() if msg.created_at else "",
                tool_name=msg.tool_name,
                tool_call_id=msg.tool_call_id,
                citations=citations_data,
                # Serve-time redaction: rows were persisted with raw args
                # (before and after #1046 redacted the live SSE preview), so
                # redacting here is what covers historical rows on reload.
                tool_executions=redact_tool_executions(msg.tool_executions),
                plan=msg.plan,
                plan_reasoning=msg.plan_reasoning,
                token_usage=msg.token_usage,
            )
        )

    return ThreadMessagesResponse(
        messages=message_responses, total=total, has_more=has_more
    )
