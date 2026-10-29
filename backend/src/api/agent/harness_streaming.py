"""SSE observation of externally executed runs from the durable event ledger."""

import asyncio
import time
from typing import Any, AsyncIterator, cast
from uuid import UUID

from sqlalchemy import select
from starlette.requests import Request

from src.core.database import AsyncSessionLocal
from src.models.agent_run import AgentRun
from src.models.harness_session import HarnessSession
from src.models.integration_grant import IntegrationGrant
from src.schemas.integration_context import BindingKind, IntegrationContext
from src.services.agent.run_event_store import read_events
from src.services.agent.run_event_types import TERMINAL_RUN_EVENTS, RunEventType

# Compatibility: chat-run grant minting moved to the service layer so the
# handoff service can mint without importing the API package. Tests patch
# ``src.api.agent.harness_streaming.create_chat_context``; streaming.py's lazy
# import reads this binding at call time, so those patches still apply.
from src.services.harness.chat_context import (  # noqa: F401
    _authorize_chat_run,
    _run_consent_kind,
    create_chat_context,
)
from src.shared.enums import AgentStreamEvent

_POLL_INTERVAL_SECONDS = 0.25
_HEARTBEAT_SECONDS = 15.0


def external_resume_cursor(after_seq: int, stream_id: str | None, run_id: UUID) -> int:
    """Use a replay cursor only when it was recorded for this external run."""
    if stream_id is None or stream_id.casefold() != str(run_id).casefold():
        return 0
    return after_seq


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
        binding_kind=cast(BindingKind, grant.binding_kind),
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
    elif kind is RunEventType.ARTIFACT_VERSION_CREATED:
        # IDs only; the client refetches artifact metadata through Query.
        artifact_id, version_id = payload.get("artifact_id"), payload.get("version_id")
        if not artifact_id or not version_id:
            return None
        event_type, data = AgentStreamEvent.ARTIFACT, {
            "artifact_id": artifact_id,
            "version_id": version_id,
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
            if RunEventType(str(event.event_type)) in TERMINAL_RUN_EVENTS:
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
