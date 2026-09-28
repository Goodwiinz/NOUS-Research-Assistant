"""SSE observation of externally executed runs from the durable event ledger."""

import asyncio
import time
from typing import Any, AsyncIterator
from uuid import UUID

from sqlalchemy import select
from starlette.requests import Request

from src.core.database import AsyncSessionLocal
from src.models.agent_run import AgentRun
from src.models.bridge_device import WorkspaceBinding
from src.models.harness_session import HarnessSession
from src.models.integration_grant import IntegrationGrant
from src.schemas.integration_context import IntegrationContext
from src.services.agent.run_event_store import read_events
from src.services.agent.run_event_types import TERMINAL_RUN_EVENTS, RunEventType
from src.services.integrations.context import mint_integration_grant
from src.shared.enums import AgentStreamEvent

_POLL_INTERVAL_SECONDS = 0.25
_HEARTBEAT_SECONDS = 15.0


def external_resume_cursor(after_seq: int, stream_id: str | None, run_id: UUID) -> int:
    """Use a replay cursor only when it was recorded for this external run."""
    if stream_id is None or stream_id.casefold() != str(run_id).casefold():
        return 0
    return after_seq


async def create_chat_context(
    db: Any,
    *,
    current_user: Any,
    thread: Any,
    device_id: UUID,
    workspace_id: UUID,
) -> IntegrationContext:
    """Mint a short-lived run-authority from prior browser-approved consent.

    The returned token is intentionally discarded. It is never returned to the
    browser; only its database identity is attached to the accepted run.
    """
    from src.core.config import settings

    if not settings.HARNESS_BRIDGE_ENABLED:
        raise ValueError("Local harness execution is disabled by server policy")
    if thread is None or thread.source_project_id is None:
        raise ValueError("Codex runs require a project-bound NOUS chat thread")
    if current_user.organization_id is None:
        raise ValueError("Codex runs require an organization-backed account")
    binding = await db.scalar(
        select(WorkspaceBinding).where(
            WorkspaceBinding.device_id == device_id,
            WorkspaceBinding.workspace_id == workspace_id,
            WorkspaceBinding.project_id == thread.source_project_id,
            WorkspaceBinding.is_deleted.is_(False),
        )
    )
    if binding is None:
        raise ValueError("The selected workspace is not bound to this chat project")
    issued = await mint_integration_grant(
        db,
        user_id=current_user.id,
        organization_id=current_user.organization_id,
        project_id=thread.source_project_id,
        scopes=frozenset({"harness:execute"}),
        thread_id=thread.id,
        device_id=device_id,
    )
    # Re-resolve through the same validation gate used by the bridge transport.
    grant = await db.get(IntegrationGrant, issued.grant_id, populate_existing=True)
    if grant is None:
        raise PermissionError("Could not bind Codex run to its accepted grant")
    return IntegrationContext(
        user_id=grant.user_id,
        organization_id=grant.organization_id,
        project_id=grant.project_id,
        thread_id=grant.thread_id,
        run_id=None,
        grant_id=grant.id,
    )


async def context_for_accepted_run(
    db: Any, *, run_id: UUID, current_user: Any
) -> IntegrationContext:
    """Derive stream authority from the accepted run's immutable owner binding."""
    row = await db.execute(
        select(AgentRun, HarnessSession, IntegrationGrant)
        .join(HarnessSession, HarnessSession.run_id == AgentRun.job_id)
        .join(IntegrationGrant, IntegrationGrant.id == HarnessSession.grant_id)
        .where(
            AgentRun.job_id == str(run_id),
            AgentRun.execution_provider == "codex",
            AgentRun.user_id == current_user.id,
            AgentRun.organization_id == current_user.organization_id,
        )
    )
    result = row.first()
    if result is None:
        raise PermissionError("Accepted Codex run is not owned by this user")
    _run, _session, grant = result
    return IntegrationContext(
        user_id=grant.user_id,
        organization_id=grant.organization_id,
        project_id=grant.project_id,
        thread_id=grant.thread_id,
        run_id=run_id,
        grant_id=grant.id,
    )


def _frame_for_event(event: Any, *, run_id: UUID) -> str | None:
    """Adapt a canonical run fact to the existing chat SSE event vocabulary."""
    kind = RunEventType(event.event_type)
    payload = dict(event.payload or {})
    seq = int(event.seq)
    if kind is RunEventType.RUN_CREATED:
        from src.api.agent.streaming import _format_sse_event

        return _format_sse_event(
            AgentStreamEvent.STATUS,
            {"phase": "accepted", "detail": "Request accepted", "run_id": str(run_id)},
            seq=seq,
        )
    if kind is RunEventType.ASSISTANT_DELTA:
        event_type, data = AgentStreamEvent.TOKEN, {"content": payload.get("text", "")}
    elif kind is RunEventType.RETRIEVAL_CONTEXT:
        event_type, data = AgentStreamEvent.RAG_CONTEXT, {
            "contexts": payload.get("items", [])
        }
    elif kind is RunEventType.PLAN_UPDATED:
        event_type, data = AgentStreamEvent.PLAN, {
            "steps": payload.get("steps", []),
            "reasoning": "",
        }
    elif kind is RunEventType.REFLECTION_COMPLETED:
        event_type, data = AgentStreamEvent.REFLECTION, {
            "passed": True,
            "issues": [payload.get("summary", "")],
            "round": 0,
            "revising": False,
        }
    elif kind is RunEventType.TOOL_STARTED:
        event_type, data = AgentStreamEvent.TOOL_START, {
            "call_id": payload.get("tool_call_id"),
            "tool": payload.get("name"),
            "args": payload.get("args", {}),
        }
    elif kind is RunEventType.TOOL_COMPLETED:
        event_type, data = AgentStreamEvent.TOOL_END, {
            "call_id": payload.get("tool_call_id"),
            "tool": payload.get("name"),
            "result": payload.get("result_preview") or payload.get("error") or "",
            "is_error": payload.get("status") == "error",
        }
    elif kind is RunEventType.APPROVAL_REQUIRED:
        event_type, data = AgentStreamEvent.APPROVAL_REQUIRED, {
            "request_id": payload.get("approval_id"),
            "name": payload.get("name"),
            "args": payload.get("args", {}),
        }
    elif kind is RunEventType.APPROVAL_RESOLVED:
        event_type, data = AgentStreamEvent.STATUS, {
            "phase": "writing",
            "detail": "Native request resolved",
        }
    elif kind is RunEventType.USAGE_UPDATED:
        event_type, data = AgentStreamEvent.USAGE, {
            "input_tokens": payload.get("input_tokens", 0),
            "output_tokens": payload.get("output_tokens", 0),
        }
    elif kind is RunEventType.RUN_STOPPING:
        event_type, data = AgentStreamEvent.STATUS, {
            "phase": "finalizing",
            "detail": "Stop requested; waiting for Codex to stop",
        }
    elif kind is RunEventType.RUN_COMPLETED:
        event_type, data = AgentStreamEvent.DONE, {
            "status": "completed",
            "assistant_message_id": payload.get("assistant_message_id"),
        }
    elif kind is RunEventType.RUN_FAILED:
        event_type, data = AgentStreamEvent.ERROR, {
            "error": payload.get("message", "Codex run failed."),
            "category": "internal",
        }
    elif kind is RunEventType.RUN_CANCELLED:
        event_type, data = AgentStreamEvent.ERROR, {
            "error": "Codex stopped.",
            "category": "cancelled",
        }
    else:
        return None
    from src.api.agent.streaming import _format_sse_event

    return _format_sse_event(event_type, data, seq=seq)


async def stream_harness_run(
    request: Request,
    run_id: UUID,
    context: IntegrationContext,
    after_seq: int = 0,
) -> AsyncIterator[str]:
    """Observe canonical events until terminal evidence or browser disconnect.

    Each poll uses a short-lived database session. Closing this iterator only
    stops reads; it does not signal cancellation to the bridge or run owner.
    """
    cursor = after_seq
    last_heartbeat = time.monotonic()
    while True:
        if await request.is_disconnected():
            return
        async with AsyncSessionLocal() as db:
            events = await read_events(
                db,
                str(run_id),
                organization_id=context.organization_id,
                user_id=context.user_id,
                after_seq=cursor,
            )
        for event in events:
            cursor = int(event.seq)
            frame = _frame_for_event(event, run_id=run_id)
            if frame is not None:
                yield frame
            if RunEventType(event.event_type) in TERMINAL_RUN_EVENTS:
                return
        if await request.is_disconnected():
            return
        now = time.monotonic()
        if now - last_heartbeat >= _HEARTBEAT_SECONDS:
            from src.api.agent.streaming import _format_sse_event

            yield _format_sse_event(
                AgentStreamEvent.HEARTBEAT,
                {"elapsed_ms": int((now - last_heartbeat) * 1000)},
            )
            last_heartbeat = now
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
