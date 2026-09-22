"""SSE streaming logic for agent execution.

Contains the event_generator functions used by the /stream and
/stream/confirm endpoints, plus SSE formatting helpers.
"""

import asyncio
import contextlib
import json as _json
import logging
import re
import threading
import time
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, cast

from anyio import CancelScope
from langgraph.errors import GraphInterrupt

from src.core.database import AsyncSessionLocal
from src.middleware.disconnect_signal import AGENT_DISCONNECT_EVENT
from src.models.user import User

# The job runner lives in the service layer (audit B5); `_jobs_mod` keeps its
# historical alias so the late-bound call sites (and the tests that patch
# them) read unchanged.
from src.services.agent import agent_execution_service as _jobs_mod
from src.services.agent import stream_buffer as _stream_buffer
from src.services.agent._builders import RECURSION_LIMIT
from src.services.agent._errors import (
    classify_agent_error,
    client_safe_error,
    error_frame_payload,
    extract_interrupt_confirmation,
)
from src.services.agent._pii_redact import redact_pii, redact_tool_args
from src.services.agent.agent_execution_service import (
    AgentThreadResolutionError,
    TombstoneReport,
    _clear_stale_pending_confirmation,
    _latest_user_client_message_id,
    _page_context_to_dict,
    _persist_assistant_message,
    _persist_user_message_guarded,
    _resolve_and_bind_project,
    _resolve_thread,
    resync_thread_checkpoint,
)
from src.services.agent.agent_run_service import (
    ActiveRunConflict,
    claim_awaiting_run_for_confirmation,
    get_active_run_for_thread,
    get_run,
    is_run_cancellation_requested,
    release_confirmation_claim,
)
from src.services.agent.agent_submission_service import (
    AcceptedSubmission,
    accept_submission,
    fail_queued_submission,
    finalize_submission,
    mark_submission_dispatched,
)
from src.services.agent.job_store import process_local_confirmation_coordination_allowed
from src.services.agent.observability import AgentStreamSLOTracker, record_token_usage
from src.services.agent.run_event_types import MAX_PAYLOAD_BYTES, RunEventType
from src.services.agent.state import (
    THREAD_PERSISTENCE_DURABLE,
    THREAD_PERSISTENCE_EPHEMERAL,
)
from src.services.agent.trace_metadata import TraceSource, build_trace_metadata
from src.shared.enums import (
    TERMINAL_STREAM_EVENTS,
    AgentErrorCategory,
    AgentStreamEvent,
    JobStatus,
)

from .trace_context import build_trace_payload

logger = logging.getLogger(__name__)

AGENT_STREAM_SCHEMA_VERSION = "1.0"
_STREAM_ROUTES = frozenset({"pending", "luna", "graph", "unknown"})
_PROGRESS_PHASES = frozenset(
    {"accepted", "routing", "retrieving", "planning", "writing", "finalizing"}
)
_MAX_PROGRESS_STEPS = 16
_MAX_REASONING_SUMMARY_CHARS = 8_000
_REPLAY_STREAM_START_GRACE_S = 5.0
_REPLAY_STREAM_POLL_INTERVAL_S = 0.1


def _ttft_ms(emitter: Any, stream_started_at: float) -> Optional[int]:
    """Milliseconds from stream start to this turn's first token frame.

    ``None`` when the turn emitted no token at all — an error raised before
    generation, or a stop during the tool phase. Measured against the caller's
    ``stream_started_at`` rather than the tracker's own (earlier) origin so the
    reading stays a subset of the ``latency_ms`` written to the same row and
    the two subtract into an honest "working" / "writing" split.
    """
    at = getattr(getattr(emitter, "slo_tracker", None), "first_token_at", None)
    if at is None:
        return None
    return max(0, int((at - stream_started_at) * 1000))


def _accept_eligible(thread_obj: Any) -> bool:
    """True when this submission has a thread the accept transaction can use.

    ``agent_runs.thread_id`` and ``chat_messages.thread_id`` are real GUID
    foreign keys, so an ownership-verified ``Thread`` with a parseable id is
    the precondition for making acceptance durable. When it is absent
    (``_resolve_thread`` found no workspace) there is no turn to persist at
    all, and the stream degrades to its pre-P0-C behaviour rather than
    failing a chat that can still answer.
    """
    if thread_obj is None:
        return False
    try:
        _uuid.UUID(str(getattr(thread_obj, "id", None)))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


async def _finalize_run(
    db: Any,
    acceptance: Optional[AcceptedSubmission],
    current_user: Any,
    *,
    status: JobStatus,
    event_type: Optional[RunEventType] = None,
    payload: Optional[Dict[str, Any]] = None,
    error_code: Optional[str] = None,
    error: Optional[str] = None,
    run_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Close the accepted run out at a stream exit. No-op without an accept.

    A run left non-terminal keeps holding ``uq_agent_runs_active_thread`` and
    is eventually reaped as "stale running". Terminal failures therefore
    propagate before a ``done`` frame; HITL parking remains best-effort.
    """
    if acceptance is None or acceptance.replayed:
        return True
    return await _finalize_run_id(
        db,
        acceptance.run_id,
        current_user,
        status=status,
        event_type=event_type,
        payload=payload,
        error_code=error_code,
        error=error,
        run_metadata=run_metadata,
    )


async def _finalize_run_id(
    db: Any,
    run_id: Optional[str],
    current_user: Any,
    *,
    status: JobStatus,
    event_type: Optional[RunEventType] = None,
    payload: Optional[Dict[str, Any]] = None,
    error_code: Optional[str] = None,
    error: Optional[str] = None,
    run_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Close a caller-owned durable run when only its id is available."""
    if not run_id:
        return True
    return await finalize_submission(
        db,
        run_id=run_id,
        status=status,
        organization_id=getattr(current_user, "organization_id", None),
        event_type=event_type,
        payload=payload,
        error_code=error_code,
        error=error,
        run_metadata=run_metadata,
    )


# `accept_submission` raises a bare ValueError when the submitted messages
# contain no user turn — a malformed *request*, not a server fault. It carries
# no dedicated type, so the branch is keyed on the exact sentinel text raised in
# `agent_submission_service.accept_submission`; anything else falls through to
# the generic classifier rather than being optimistically blamed on the client.
_NO_USER_MESSAGE_SENTINEL = "accept_submission requires a user message"


# Anti-enumeration: /stream/confirm's "no such checkpoint" and "not your
# checkpoint" branches MUST be indistinguishable to the caller — same message,
# same category, byte-identical payload. Both branches build their frame from
# these two constants precisely so neither the string nor the category can
# drift into an oracle for whether a guessed thread id exists.
_CONFIRM_NOT_FOUND_MESSAGE = "Thread not found"
_CONFIRM_NOT_FOUND_CATEGORY = AgentErrorCategory.INVALID_REQUEST


def _stream_failure_category(exc: BaseException) -> AgentErrorCategory:
    """Category for the /stream catch-all: `invalid_request` for the
    missing-user-message rejection, otherwise the generic classification."""
    if isinstance(exc, ValueError) and _NO_USER_MESSAGE_SENTINEL in str(exc):
        return AgentErrorCategory.INVALID_REQUEST
    if isinstance(exc, ActiveRunConflict):
        return AgentErrorCategory.CONFLICT
    if isinstance(exc, AgentThreadResolutionError):
        return AgentErrorCategory.INVALID_REQUEST
    return classify_agent_error(exc)


def _request_trace_id(request: Any) -> str:
    """Return a safe per-turn correlation id.

    Reuse the request id installed by middleware when available so HTTP logs,
    SSE frames, LangSmith metadata, and Prometheus exemplars can be correlated.
    Unit generators and middleware-free callers get a UUID fallback.
    """
    state = getattr(request, "state", None)
    request_id = getattr(state, "request_id", None)
    if not request_id:
        headers = getattr(request, "headers", {})
        request_id = headers.get("x-request-id") if headers else None
    value = str(request_id or _uuid.uuid4())
    # The header is caller-controlled: anything outside a plain token charset
    # or over 128 chars gets replaced rather than echoed into envelopes /
    # ledger / LangSmith metadata. No truncation — two distinct oversized ids
    # sharing a 128-char prefix must not collapse into one trace id.
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        return str(_uuid.uuid4())
    return value


def _assistant_client_message_id(request_body: Any) -> Optional[str]:
    last_user = next(
        (
            message
            for message in reversed(request_body.messages)
            if message.role == "user"
        ),
        None,
    )
    user_cmid = getattr(last_user, "client_message_id", None)
    if user_cmid is None:
        return None
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"nous-assistant:{user_cmid}"))


def _chunk_text(chunk: Any) -> str:
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def _chunk_reasoning_summary(chunk: Any) -> str:
    """Extract provider-authored reasoning-summary deltas, never raw reasoning."""
    content = getattr(chunk, "content", None)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        summaries = item.get("summary")
        if not isinstance(summaries, list):
            continue
        parts.extend(
            str(summary.get("text", ""))
            for summary in summaries
            if isinstance(summary, dict) and summary.get("type") == "summary_text"
        )
    return "".join(parts)


def _append_reasoning_summary(current: str, delta: str) -> str:
    """Accumulate only bounded, provider-authored public summary text."""
    if not isinstance(delta, str) or not delta:
        return current
    return (current + delta)[:_MAX_REASONING_SUMMARY_CHARS]


def _latest_turn_assistant_text(messages: Any) -> str:
    """Text of the newest AI message produced AFTER the newest human turn.

    A plain reversed last-AI-with-content scan reaches PAST the current turn:
    when a turn parks on a HITL interrupt (or otherwise produces no AI text),
    the scan lands on the PREVIOUS turn's answer, which then gets streamed as
    a fake token frame and persisted as this turn's assistant row — the exact
    duplicate observed live on thread 014caf59 (2026-08-12). Stopping at the
    first human message bounds the scan to the current turn.
    """
    for msg in reversed(list(messages or [])):
        msg_type = getattr(msg, "type", None)
        if msg_type == "human":
            return ""
        if msg_type == "ai":
            text = _chunk_text(msg)
            if text:
                return text
    return ""


async def _stream_luna_fast_path(
    *,
    request_body: Any,
    request: Any,
    current_user: User,
    db: Any,
    resolved_thread_id: str,
    emitter: Any,
    stream_started_at: float,
    trace_run_id: _uuid.UUID,
    acceptance: Optional[AcceptedSubmission] = None,
):
    """Run one evidence-independent turn without entering LangGraph execution.

    ``acceptance`` is the committed P0-C accept record (``None`` on the
    degraded no-durable-thread path). When present the user row is already
    durable, and this path owns closing the run out.
    """
    from langchain_core.messages import AIMessage, HumanMessage

    from src.core.config import get_settings
    from src.services.agent.checkpointer import get_checkpointer
    from src.services.agent.fast_path import (
        build_fast_path_messages,
        stream_fast_path_chunks,
    )
    from src.services.agent.graph import compile_agent_graph
    from src.services.agent.llm_factory import (
        _resolve_fast_path_deployment,
        build_fast_path_llm,
    )
    from src.services.agent.memory import get_memory_store

    settings = get_settings()
    deployment = _resolve_fast_path_deployment()
    stream_thread_id = resolved_thread_id
    assistant_cmid = _assistant_client_message_id(request_body)
    last_user = next(
        (
            message
            for message in reversed(request_body.messages)
            if message.role == "user"
        ),
        None,
    )
    if last_user is None:
        # Reuse the shared sentinel so _stream_failure_category maps this to
        # invalid_request instead of internal.
        raise ValueError(_NO_USER_MESSAGE_SENTINEL)
    user_message_id = str(
        getattr(last_user, "client_message_id", None)
        or _uuid.uuid5(
            _uuid.NAMESPACE_URL,
            f"{stream_thread_id}:fast-user:{last_user.content}",
        )
    )
    assistant_message_id = assistant_cmid or str(
        _uuid.uuid5(_uuid.NAMESPACE_URL, f"{user_message_id}:fast-assistant")
    )

    prompt = build_fast_path_messages(
        request_body.messages,
        max_input_chars=settings.AGENT_FAST_PATH_MAX_INPUT_CHARS,
    )
    llm = build_fast_path_llm()
    parts: list[str] = []
    reasoning_summary = ""
    input_tokens = 0
    output_tokens = 0
    client_disconnected = False
    assistant_saved = False

    # Edit-and-resend on the fast path. Under an accept (P0-C) the tombstones
    # are already committed — they rode the accept transaction alongside the
    # user row — so the report is seeded from it. On the degraded path the
    # persist still owns them, and it runs CONCURRENTLY with the model stream
    # (that is the whole point of this route), so nothing can be converged
    # before the answer is streamed either way. Convergence happens at the
    # post-answer checkpoint write below, the first point where this route
    # holds a compiled graph.
    tombstones = TombstoneReport()
    if acceptance is not None and acceptance.tombstoned:
        tombstones.mark()

    async def persist_user() -> bool:
        # Already committed by the accept transaction (P0-C). Re-running the
        # insert would add a SECOND user row for a legacy client that sends no
        # client_message_id — there is nothing for ON CONFLICT to infer.
        if acceptance is not None:
            return False
        return await _persist_user_message_guarded(
            db, current_user, request_body, tombstoned_out=tombstones
        )

    # Declared here so cancel_fast_path can link the cancelled run to the
    # stopped partial row it persists — same contract as the graph path.
    persisted_partial_id: Optional[str] = None
    persisted_assistant_id: Optional[str] = None

    async def persist_partial() -> None:
        nonlocal persisted_partial_id
        if assistant_saved:
            if persisted_assistant_id:
                with contextlib.suppress(BaseException):
                    await _jobs_mod._mark_assistant_message_stopped_safe(
                        thread_id=resolved_thread_id,
                        message_id=persisted_assistant_id,
                    )
            return
        partial = "".join(parts)
        if not partial and not reasoning_summary:
            return
        persisted_partial_id = await _jobs_mod._persist_assistant_message_safe(
            thread_id=resolved_thread_id,
            content=partial,
            model_name=deployment,
            tool_executions_out=None,
            retrieved_contexts=None,
            latency_ms=int((time.monotonic() - stream_started_at) * 1000),
            ttft_ms=_ttft_ms(emitter, stream_started_at),
            stopped=True,
            client_message_id=assistant_cmid,
            reasoning_summary=reasoning_summary or None,
            progress_steps=emitter.progress_steps or None,
        )

    async def cancel_fast_path(reason: str = "client_disconnected") -> None:
        async def finalize_cancelled() -> None:
            cancelled_payload: Dict[str, Any] = {
                "reason": reason,
                "request_id": emitter.trace_id,
            }
            # Link the run to the stopped partial row persisted just above,
            # so a cancelled run can still name the message holding its
            # output. Omitted when nothing streamed before the abort.
            linked_assistant_id = persisted_partial_id or persisted_assistant_id
            if linked_assistant_id:
                cancelled_payload["assistant_message_id"] = linked_assistant_id
            await _finalize_run(
                db,
                acceptance,
                current_user,
                status=JobStatus.CANCELLED,
                event_type=RunEventType.RUN_CANCELLED,
                payload=cancelled_payload,
            )

        await _run_cancel_cleanup(
            ("persist_partial", persist_partial),
            ("finish_emitter", emitter.finish),
            ("finalize_cancelled", finalize_cancelled),
        )

    async def durable_stop_requested() -> bool:
        if acceptance is None:
            return False
        try:
            return await is_run_cancellation_requested(
                db,
                acceptance.run_id,
                organization_id=getattr(current_user, "organization_id", None),
                user_id=current_user.id,
            )
        except Exception:
            logger.warning(
                "Failed to poll durable stop marker for Luna run %s",
                acceptance.run_id,
                exc_info=True,
            )
            return False

    try:
        # Bind the buffer to the durable run so a client retry with the same
        # cmid can reattach via stream_id_for_run instead of dying after the
        # 5s poll (audit S2-H1). The degraded no-acceptance path has no run.
        await emitter.start(
            stream_thread_id,
            run_id=acceptance.run_id if acceptance is not None else None,
        )
        yield await emitter.emit(
            AgentStreamEvent.STATUS,
            {"phase": "routing", "detail": "Using the direct Luna path"},
        )
        yield await emitter.emit(
            AgentStreamEvent.TRACE,
            build_trace_payload(
                thread_id=stream_thread_id,
                cli_session_id="",
                langsmith_run_id=str(trace_run_id),
            ),
        )

        if acceptance is not None:
            # The dispatch this outbox row recorded is about to happen
            # in-process. Stamping it keeps a future relay from re-dispatching
            # a run that already ran; storage failures enter the terminal error
            # path below rather than masquerading as a lost execution claim.
            dispatch_claimed = await mark_submission_dispatched(
                db,
                run_id=acceptance.run_id,
                outbox_id=acceptance.outbox_id,
                organization_id=getattr(current_user, "organization_id", None),
                user_id=current_user.id,
            )
            if dispatch_claimed is False:
                if await durable_stop_requested():
                    await cancel_fast_path(reason="user_requested")
                    return
                yield await emitter.emit(
                    AgentStreamEvent.ERROR,
                    error_frame_payload(
                        "This response is no longer active. Please retry.",
                        AgentErrorCategory.CONFLICT,
                    ),
                )
                await emitter.finish()
                return
        async with asyncio.timeout(settings.AGENT_FAST_PATH_REQUEST_TIMEOUT):
            writing_emitted = False
            fast_path_chunks = stream_fast_path_chunks(
                llm=llm,
                messages=prompt,
                persist_user=persist_user,
            ).__aiter__()
            fast_path_events = _graph_events_with_keepalive(
                fast_path_chunks,
                request,
                stop_requested=durable_stop_requested,
            )
            try:
                async for item in fast_path_events:
                    if item["type"] == "disconnect":
                        client_disconnected = True
                        await cancel_fast_path()
                        return
                    if item["type"] == "stop":
                        await cancel_fast_path(reason="user_requested")
                        return
                    if item["type"] == "keepalive":
                        frame = await emitter.emit(
                            AgentStreamEvent.HEARTBEAT,
                            {
                                "elapsed_ms": int(
                                    (time.monotonic() - stream_started_at) * 1000
                                )
                            },
                            buffer=False,
                        )
                        if not client_disconnected:
                            yield frame
                        continue

                    chunk = item["event"]
                    reasoning_delta = _chunk_reasoning_summary(chunk)
                    if reasoning_delta:
                        reasoning_summary = _append_reasoning_summary(
                            reasoning_summary, reasoning_delta
                        )
                        frame = await emitter.emit(
                            AgentStreamEvent.REASONING_DELTA,
                            {"content": reasoning_delta},
                        )
                        if not client_disconnected:
                            yield frame
                    text = _chunk_text(chunk)
                    usage = getattr(chunk, "usage_metadata", None)
                    if isinstance(usage, dict):
                        input_tokens = max(
                            input_tokens, int(usage.get("input_tokens", 0) or 0)
                        )
                        output_tokens = max(
                            output_tokens, int(usage.get("output_tokens", 0) or 0)
                        )
                    if not text:
                        continue
                    if not writing_emitted:
                        yield await emitter.emit(
                            AgentStreamEvent.STATUS,
                            {"phase": "writing", "detail": "Luna is responding"},
                        )
                        writing_emitted = True
                    # Buffer BEFORE recording for persistence — same ordering
                    # invariant as the graph path: the stopped partial must stay
                    # a prefix of the buffered stream, so a cancellation inside
                    # this await can only lose the last chunk, never invent one.
                    frame = await emitter.emit(
                        AgentStreamEvent.TOKEN,
                        {"content": text},
                    )
                    parts.append(text)
                    if not client_disconnected:
                        yield frame
            finally:
                await _close_async_iterator(fast_path_events)

        if await durable_stop_requested():
            await cancel_fast_path(reason="user_requested")
            return

        assistant_content = "".join(parts)
        if not assistant_content:
            raise RuntimeError("Luna completed without response content")

        yield await emitter.emit(
            AgentStreamEvent.STATUS,
            {"phase": "finalizing", "detail": "Saving the response"},
        )

        if await durable_stop_requested():
            await cancel_fast_path(reason="user_requested")
            return

        persisted_assistant_id = await _jobs_mod._persist_assistant_message_safe(
            thread_id=resolved_thread_id,
            content=assistant_content,
            model_name=deployment,
            tool_executions_out=None,
            retrieved_contexts=None,
            latency_ms=int((time.monotonic() - stream_started_at) * 1000),
            ttft_ms=_ttft_ms(emitter, stream_started_at),
            stopped=False,
            client_message_id=assistant_cmid,
            reasoning_summary=reasoning_summary or None,
            progress_steps=emitter.progress_steps or None,
            token_usage=(
                {"input_tokens": input_tokens, "output_tokens": output_tokens}
                if input_tokens or output_tokens
                else None
            ),
        )
        assistant_saved = persisted_assistant_id is not None

        # Keep the graph checkpoint authoritative for a later grounded/tool turn.
        checkpointer, store = await asyncio.gather(
            get_checkpointer(),
            get_memory_store(),
        )
        graph = compile_agent_graph(checkpointer=checkpointer, store=store)
        checkpoint_config = {
            "configurable": {
                "thread_id": stream_thread_id,
                "user_id": str(current_user.id),
                "organization_id": str(
                    getattr(current_user, "organization_id", "") or ""
                ),
            }
        }
        # Edit-and-resend: rebuild HEAD from the post-edit DB first, so the
        # replaced turn stops feeding the model.
        if tombstones.any:
            if await durable_stop_requested():
                await cancel_fast_path(reason="user_requested")
                return
            await resync_thread_checkpoint(
                graph,
                thread_id=stream_thread_id,
                user=current_user,
            )
        # The append ALWAYS runs, including right after a resync. It is
        # idempotent by id: ``add_messages`` (the reducer behind
        # ``aupdate_state``) replaces a same-id message in place rather than
        # appending a duplicate (pinned by
        # test_agent_edit_resend_checkpoint.py::
        # test_langgraph_replaces_a_same_id_message_in_place), so the write is
        # at worst a no-op. It is also the REPAIR for a partial-row dedup: a
        # cancelled attempt can persist a stopped partial answer under this
        # same deterministic assistant cmid, the retry's insert dedups onto that
        # row WITHOUT updating its content, and the resync then seeds the
        # PARTIAL text. Skipping the append there would leave the model's
        # context holding a truncated answer the client never saw.
        if await durable_stop_requested():
            await cancel_fast_path(reason="user_requested")
            return
        await graph.aupdate_state(
            checkpoint_config,
            {
                "messages": [
                    HumanMessage(content=last_user.content, id=user_message_id),
                    AIMessage(content=assistant_content, id=assistant_message_id),
                ]
            },
            as_node="memory_save_node",
        )

        if persisted_assistant_id is None:
            raise RuntimeError("Assistant message persistence returned no id")

        if input_tokens or output_tokens:
            record_token_usage(deployment, input_tokens, output_tokens)
            yield await emitter.emit(
                AgentStreamEvent.USAGE,
                {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            )

        done_payload: Dict[str, Any] = {
            "status": "complete",
            "progress_steps": emitter.progress_steps,
            "tool_executions": [],
        }
        if reasoning_summary:
            done_payload["reasoning_summary"] = reasoning_summary
        if _canonical_persistence_enabled():
            done_payload.update(
                {
                    "thread_id": resolved_thread_id,
                    "assistant_message_id": persisted_assistant_id,
                    "client_message_id": assistant_cmid,
                }
            )
        # Commit completion before exposing the terminal frame. A disconnect
        # immediately after ``done`` must not race into run.cancelled.
        if await durable_stop_requested():
            await cancel_fast_path(reason="user_requested")
            return
        finalized = await _finalize_run(
            db,
            acceptance,
            current_user,
            status=JobStatus.COMPLETED,
            event_type=RunEventType.RUN_COMPLETED,
            payload=(
                {"assistant_message_id": persisted_assistant_id}
                if persisted_assistant_id
                else {}
            ),
        )
        if finalized is False:
            if await durable_stop_requested():
                await cancel_fast_path(reason="user_requested")
            else:
                # Another producer already reached a terminal state. Its
                # durable status is authoritative; do not manufacture a
                # cancellation ACK for an ordinary completed run.
                await emitter.finish()
            return
        yield await emitter.emit(AgentStreamEvent.DONE, done_payload)
        await emitter.finish()
    except (asyncio.CancelledError, GeneratorExit) as exit_exc:
        await _run_interrupted_cleanup(cancel_fast_path)
        raise exit_exc
    except Exception as exc:
        logger.error("Luna fast-path stream failed", exc_info=exc)
        await persist_partial()
        yield await emitter.emit(
            AgentStreamEvent.ERROR,
            error_frame_payload(exc, category=_stream_failure_category(exc)),
        )
        await emitter.finish()
        finalized = await _finalize_run(
            db,
            acceptance,
            current_user,
            status=JobStatus.FAILED,
            event_type=RunEventType.RUN_FAILED,
            payload={
                "code": "luna_stream_failed",
                "message": client_safe_error(exc),
            },
        )
        if finalized is False and await durable_stop_requested():
            await cancel_fast_path(reason="user_requested")
        try:
            from langsmith.run_helpers import get_current_run_tree

            root_run = get_current_run_tree()
            while getattr(root_run, "parent_run", None) is not None:
                root_run = root_run.parent_run
            if root_run is not None:
                root_run.error = repr(exc)
        except Exception:
            logger.debug("Failed to mark Luna trace root failed", exc_info=True)
        return


def _canonical_persistence_enabled() -> bool:
    """Server-canonical persistence rollout flag (PR 1 of the
    dual-persistence consolidation). When on, the done payload carries the
    persisted ids so the client can reconcile instead of writing its own copy.
    Graph routes always persist synchronously before `done`; this flag now
    controls only that additive client contract. Read per-call (not at import)
    so tests and the dev cluster can flip it without a process restart."""
    import os

    return os.getenv("AGENT_CANONICAL_PERSISTENCE", "").lower() in (
        "1",
        "true",
        "yes",
    )


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# Only chat-model streams originating from these LangGraph nodes are
# forwarded as user-visible `token` SSE events. Internal LLM calls
# (intent classifier inside rag_node, planner's structured-output
# complexity check, reflection critique, summarisers inside subgraph
# tool nodes) ALSO trigger on_chat_model_stream — emitting their tokens
# leaks raw JSON ({"intent":…}, {"step_count":1}) and interleaves
# parallel summarisations into the response stream. These allow-listed nodes
# are the only ones whose chat output the user is meant to see.
_USER_FACING_LLM_NODES = frozenset(
    {
        "llm_node",
        "force_synthesis_node",
        "research_llm_node",
        "research_force_synthesis_node",
        "writing_llm_node",
        "writing_force_synthesis_node",
        "data_llm_node",
        "data_force_synthesis_node",
    }
)


def _is_user_facing_token_event(event: Dict[str, Any]) -> bool:
    """Return True when this on_chat_model_stream event came from a node
    whose tokens we want to forward to the client.

    astream_events v2 records the originating LangGraph node on
    ``event['metadata']['langgraph_node']``. Nested subgraph nodes set
    this to the subgraph's own node name (e.g. ``research_llm_node``),
    not the parent's ``research_subgraph`` wrapper — so a flat allow-list
    on the inner node names is enough.
    """
    metadata = event.get("metadata") or {}
    node = metadata.get("langgraph_node")
    return node in _USER_FACING_LLM_NODES


def _bootstrap_langsmith() -> None:
    """Enable LangSmith tracing when the API key is configured."""
    try:
        from src.services.agent.observability import configure_langsmith

        configure_langsmith()
    except Exception:
        logger.warning(
            "Failed to configure LangSmith tracing; continuing without tracing",
            exc_info=True,
        )


def _extract_usage_tokens(event: Dict[str, Any]) -> tuple[int, int]:
    """Pull (input_tokens, output_tokens) out of an `on_chat_model_end` event.

    LangChain attaches usage metadata to the AIMessage in ``data.output`` —
    either as ``usage_metadata`` (preferred, normalized across providers) or
    on ``response_metadata.token_usage`` (raw provider payload). We try both,
    defaulting to (0, 0) when the model didn't report usage.
    """
    output = event.get("data", {}).get("output")
    if output is None:
        return 0, 0

    usage = getattr(output, "usage_metadata", None)
    if isinstance(usage, dict):
        return (
            int(usage.get("input_tokens", 0) or 0),
            int(usage.get("output_tokens", 0) or 0),
        )

    response_meta = getattr(output, "response_metadata", None)
    if isinstance(response_meta, dict):
        token_usage = response_meta.get("token_usage") or {}
        if isinstance(token_usage, dict):
            return (
                int(
                    token_usage.get("prompt_tokens")
                    or token_usage.get("input_tokens")
                    or 0
                ),
                int(
                    token_usage.get("completion_tokens")
                    or token_usage.get("output_tokens")
                    or 0
                ),
            )

    return 0, 0


def _encode_tool_result(output: Any) -> str:
    """Render a tool's return value for the SSE ``tool_end.result`` field.

    For dict/list outputs, emit JSON so the CLI can parse and summarize.
    For everything else (strings, primitives, exotic objects), fall back
    to ``str()`` — same as before. Catches serialization failures so an
    unexpectedly non-JSON-able value (e.g. a tool that returns a
    ``datetime``) never breaks the stream.
    """
    if isinstance(output, (dict, list)):
        try:
            encoded = _json.dumps(output, default=str)
            if len(encoded) <= 500:
                return encoded
            if isinstance(output, dict):
                # Over the cap: shorten long string values instead of slicing
                # the serialized JSON mid-token — identity fields the frontend
                # parses (note_id, project_id, …) must survive as valid JSON.
                compact = {
                    key: (
                        value[:120] + "…"
                        if isinstance(value, str) and len(value) > 120
                        else value
                    )
                    for key, value in output.items()
                }
                encoded = _json.dumps(compact, default=str)
                if len(encoded) <= 500:
                    return encoded
            if isinstance(output, dict):
                priority_keys = (
                    "status",
                    "task_id",
                    "draft_id",
                    "project_id",
                    "error",
                    "message",
                )
                priority_limits = {
                    "status": 32,
                    "task_id": 88,
                    "draft_id": 88,
                    "project_id": 88,
                    "error": 40,
                    "message": 40,
                }
                summary = {
                    key: (
                        output[key][: priority_limits[key]] + "…"
                        if isinstance(output[key], str)
                        and len(output[key]) > priority_limits[key]
                        else output[key]
                    )
                    for key in priority_keys
                    if key in output
                }
                summary["truncated"] = True
                encoded = _json.dumps(summary, default=str)
                if len(encoded) <= 500:
                    return encoded
                # Extremely long/non-string optional details must never evict
                # the terminal status and durable identifiers the client uses.
                core = {
                    key: summary[key]
                    for key in ("status", "task_id", "draft_id", "project_id")
                    if key in summary
                }
                core["truncated"] = True
                encoded = _json.dumps(core, default=str)
                if len(encoded) <= 500:
                    return encoded
            return _json.dumps({"truncated": True})
        except (TypeError, ValueError):
            pass
    return str(output)[:500]


def _tool_args_preview(tool_input: Any) -> Any:
    """Render a tool's input args for the SSE ``tool_start.args`` field.

    Returns a JSON-safe **object** when the tool input is a dict, so the
    frontend's args summarizer (which ignores non-object args) renders the
    live preview. Previously this emitted a Python-repr string
    (``str({'query': 'x'})``) which rendered nothing live while the persisted
    object rendered on reload — the designed live preview never worked. PII is
    redacted per value before the payload leaves the server (browser-visible
    SSE). Non-dict inputs fall back to a redacted, capped string.

    Shared by the main and confirm/resume streams so they can never drift
    (the confirm path previously skipped redaction entirely).
    """
    if isinstance(tool_input, dict):
        return redact_tool_args(tool_input)
    return redact_pii(str(tool_input))[:500] if tool_input else ""


def _tool_event_identity(event: Mapping[str, Any]) -> Dict[str, str]:
    """Correlation carried by matching LangGraph tool start/end events."""
    run_id = event.get("run_id")
    return {"call_id": str(run_id)} if run_id else {}


def _format_sse_event(
    event_type: str, data: Dict[str, Any], seq: Optional[int] = None
) -> str:
    """Format a single SSE event frame.

    When ``seq`` is given, prepend an ``id:`` line so EventSource clients
    (and the resume endpoint) can address individual frames.
    """
    prefix = f"id: {seq}\n" if seq is not None else ""
    return f"{prefix}event: {event_type}\ndata: {_json.dumps(data)}\n\n"


def build_stream_envelope(
    data: Dict[str, Any],
    *,
    seq: int,
    trace_id: str,
    thread_id: Optional[str] = None,
    route: str = "pending",
    stream_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Wrap an SSE payload in the agent stream envelope.

    Single source of the envelope shape (schema_version, sequence, event_id,
    occurred_at, trace_id, thread_id, route). ``_SeqEmitter.emit`` is the main
    caller; anything else that has to put a frame on the wire outside a live
    stream (see ``_pending_confirmation_frame`` on the resume path) goes
    through here rather than hand-rolling a payload that drifts.
    """
    envelope = {
        **data,
        "schema_version": AGENT_STREAM_SCHEMA_VERSION,
        "sequence": seq,
        "event_id": f"{trace_id}:{seq}",
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "trace_id": trace_id,
        "thread_id": thread_id,
        "route": route,
    }
    # Additive run-correlation field (codex audit CX1): clients echo it on
    # /stream/resume so a replayed resume can't attach to a NEWER stream on
    # the same thread. Omitted (not null) when the emitter has no buffer id,
    # so non-buffered frames keep their exact pre-change shape.
    if stream_id is not None:
        envelope["stream_id"] = stream_id
    return envelope


def format_stream_envelope_frame(
    event_type: Any,
    data: Dict[str, Any],
    *,
    seq: int,
    trace_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    route: str = "pending",
    stream_id: Optional[str] = None,
) -> str:
    """Serialize one enveloped SSE frame, ``id:`` line included.

    The ``id:`` line is what EventSource turns into Last-Event-ID, so a frame
    emitted without it silently freezes the client's resume cursor.
    """
    return _format_sse_event(
        event_type,
        build_stream_envelope(
            data,
            seq=seq,
            trace_id=trace_id or str(_uuid.uuid4()),
            thread_id=thread_id,
            route=route,
            stream_id=stream_id,
        ),
        seq=seq,
    )


# Audit S2-M2: the durable ledger enforces MAX_PAYLOAD_BYTES per event, but
# live/buffered SSE frames historically bounded item counts only
# (contexts[:3], ltrim frame caps), so one oversized retrieval context / plan
# step / reflection issue amplified onto the socket and into up to 5000 Redis
# replay copies. emit() clamps any over-budget payload through this ladder
# before formatting — wire and buffer share the single clamped frame.
_TRUNCATED_MARKER = "payload_truncated"
_STUB_MARKER = "payload_stub"
_TRUNCATION_SUFFIX = "...[truncated]"
# Longest string value the identity stub will carry per key. Identity fields
# (ids, rounds, flags, short titles) sit well under this; prose does not.
_STUB_SCALAR_MAX_CHARS = 128
# Rungs of (max string chars, max list items); the first rung whose JSON
# serialization fits the budget wins. The final rung fits any payload whose
# key set is code-controlled.
_CLIP_LADDER: tuple[tuple[int, int], ...] = (
    (2000, 32),
    (512, 8),
    (128, 4),
    (64, 1),
)

_PUBLIC_RAG_CONTEXT_KEYS = frozenset(
    {
        "document_id",
        "external_reference_id",
        "title",
        "content",
        "text",
        "snippet",
        "score",
        "score_source",
        "rerank_score",
        "chunk_id",
        "chunk_index",
        "page_number",
        "source_position",
    }
)
_RAG_IDENTITY_KEYS = frozenset({"document_id", "external_reference_id", "chunk_id"})
_RAG_DESCRIPTIVE_KEYS = frozenset(
    {"title", "content", "text", "snippet", "score_source"}
)
_MAX_RAG_IDENTIFIER_JSON_BYTES = 192


def _changed_retrieved_context_snapshot(
    event: Mapping[str, Any], previous_fingerprint: Optional[str]
) -> tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Return a changed cumulative context snapshot from any chain node."""
    if event.get("event") != "on_chain_end":
        return None, previous_fingerprint
    data = event.get("data")
    output = data.get("output") if isinstance(data, Mapping) else None
    contexts = output.get("retrieved_contexts") if isinstance(output, Mapping) else None
    if not isinstance(contexts, list):
        return None, previous_fingerprint
    try:
        fingerprint = _json.dumps(contexts, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None, previous_fingerprint
    if fingerprint == previous_fingerprint:
        return None, previous_fingerprint
    return contexts, fingerprint


def _json_size_bytes(value: Any) -> int:
    return len(_json.dumps(value).encode("utf-8"))


def _bound_rag_context_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Bound source fields while retaining every canonical source slot."""
    raw_contexts = data.get("contexts")
    if not isinstance(raw_contexts, list):
        return data
    projected: List[Dict[str, Any]] = []
    for source_position, context in enumerate(raw_contexts, start=1):
        if not isinstance(context, dict):
            continue
        public_context: Dict[str, Any] = {"source_position": source_position}
        for key, value in context.items():
            if key not in _PUBLIC_RAG_CONTEXT_KEYS or key == "source_position":
                continue
            if key in _RAG_IDENTITY_KEYS:
                if (
                    not isinstance(value, str)
                    or not value.strip()
                    or _json_size_bytes(value) > _MAX_RAG_IDENTIFIER_JSON_BYTES
                ):
                    continue
            public_context[key] = value
        projected.append(public_context)
    candidate: Dict[str, Any] = {**data, "contexts": projected}
    if _json_size_bytes(candidate) <= MAX_PAYLOAD_BYTES:
        return candidate

    for max_chars in (2000, 1024, 512, 256, 128, 64, 16, 0):
        clipped_contexts = [
            {
                key: (
                    (value[:max_chars] + _TRUNCATION_SUFFIX if max_chars else "")
                    if key in _RAG_DESCRIPTIVE_KEYS
                    and isinstance(value, str)
                    and len(value) > max_chars
                    else value
                )
                for key, value in context.items()
            }
            for context in projected
        ]
        candidate = {
            **data,
            "contexts": clipped_contexts,
            _TRUNCATED_MARKER: True,
        }
        if _json_size_bytes(candidate) <= MAX_PAYLOAD_BYTES:
            return candidate

    # The projection admits only code-controlled scalar fields and identifiers
    # whose serialized size is bounded above. Twenty minimal source records
    # therefore fit. Keep the assertion fail-closed during development rather
    # than handing this frame to the generic list-slicing ladder.
    minimal = {
        "contexts": [
            {
                key: value
                for key, value in context.items()
                if key not in _RAG_DESCRIPTIVE_KEYS
            }
            for context in projected
        ],
        _TRUNCATED_MARKER: True,
    }
    if _json_size_bytes(minimal) <= MAX_PAYLOAD_BYTES:
        return minimal
    raise ValueError("canonical rag_context identity payload exceeds byte budget")


def _clip_payload_values(value: Any, *, max_chars: int, max_items: int) -> Any:
    """Depth-preserving clip: strings keep their prefix (+ marker), lists keep
    their head items, dicts/scalars pass through. Shapes stay valid for FE
    consumers that index or iterate them."""
    if isinstance(value, str):
        if len(value) > max_chars:
            return value[:max_chars] + _TRUNCATION_SUFFIX
        return value
    if isinstance(value, list):
        return [
            _clip_payload_values(item, max_chars=max_chars, max_items=max_items)
            for item in value[:max_items]
        ]
    if isinstance(value, dict):
        return {
            key: _clip_payload_values(item, max_chars=max_chars, max_items=max_items)
            for key, item in value.items()
        }
    return value


def _stub_identity_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Floor of the clamp ladder: keep the frame's identity, drop its bulk.

    The ladder's last rung is not a guaranteed fit — a payload with enough
    top-level keys stays over budget even clipped to (64, 1). This builds the
    replacement bottom-up under a running byte budget instead, so the result
    fits by construction no matter how many keys the producer sent. Small
    scalars survive in producer order (ids, counters, flags, short titles);
    strings over ``_STUB_SCALAR_MAX_CHARS`` and every container are dropped.

    Correlation fields the consumer needs (trace_id, thread_id, sequence,
    event_id) are added by ``build_stream_envelope`` downstream and so are
    never at risk here.
    """
    stub: Dict[str, Any] = {_TRUNCATED_MARKER: True, _STUB_MARKER: True}
    budget = MAX_PAYLOAD_BYTES - _json_size_bytes(stub)
    for key, value in data.items():
        if key in stub:
            continue
        if isinstance(value, str):
            if len(value) > _STUB_SCALAR_MAX_CHARS:
                continue
        elif not isinstance(value, (bool, int, float)) and value is not None:
            continue
        try:
            # Overstates the real cost by one byte (the added comma replaces
            # a brace) — deliberately conservative so the result cannot land
            # a byte over budget.
            cost = _json_size_bytes({key: value})
        except (TypeError, ValueError):
            continue
        if cost > budget:
            continue
        budget -= cost
        stub[key] = value
    return stub


def _bound_frame_payload(event_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``data`` unchanged when it fits MAX_PAYLOAD_BYTES; otherwise a
    clipped copy carrying ``payload_truncated: True``. Never raises and never
    mutates the input: an oversized producer degrades one frame instead of
    failing the stream."""
    if event_type == str(AgentStreamEvent.RAG_CONTEXT) and isinstance(
        data.get("contexts"), list
    ):
        return _bound_rag_context_payload(data)

    # Serialize once: the oversized path is exactly where the payload is
    # multi-megabyte, so re-encoding it for a log field is the one cost worth
    # avoiding here. Every length below reuses an encoding already computed.
    try:
        encoded = _json.dumps(data)
    except (TypeError, ValueError):
        # Not JSON-serializable: the formatter downstream already surfaces
        # this exactly as it did before the byte budget existed.
        return data
    original_bytes = len(encoded.encode("utf-8"))
    if original_bytes <= MAX_PAYLOAD_BYTES:
        return data
    bounded = data
    bounded_bytes = original_bytes
    for max_chars, max_items in _CLIP_LADDER:
        candidate = cast(
            Dict[str, Any],
            _clip_payload_values(data, max_chars=max_chars, max_items=max_items),
        )
        candidate[_TRUNCATED_MARKER] = True
        bounded = candidate
        bounded_bytes = _json_size_bytes(candidate)
        if bounded_bytes <= MAX_PAYLOAD_BYTES:
            break
    if bounded_bytes > MAX_PAYLOAD_BYTES:
        # Ladder exhausted and still over: too many top-level keys for any
        # per-value clip to save. Degrade to identity only.
        bounded = _stub_identity_payload(data)
        bounded_bytes = _json_size_bytes(bounded)
    logger.warning(
        "stream frame payload exceeded byte budget; clipped",
        extra={
            "event_type": event_type,
            "original_bytes": original_bytes,
            "clipped_bytes": bounded_bytes,
        },
    )
    return bounded


class _SeqEmitter:
    """Sequence-numbered SSE frames, teed into the resumable-stream Redis
    buffer (``src.services.agent.stream_buffer``). Buffering is best-effort:
    Redis down degrades to plain live streaming, never a failed turn.
    """

    def __init__(
        self,
        *,
        trace_id: Optional[str] = None,
        slo_tracker: Optional[AgentStreamSLOTracker] = None,
        started_at: Optional[float] = None,
    ) -> None:
        """``started_at`` is a ``time.monotonic()`` reading from the route
        handler's first line. The emitter is built only after auth, rate
        limiting, and body parsing have run, so stamping the SLI clock here
        would exclude the very overhead the accepted-latency SLI exists to
        measure. Falls back to "now" when a caller has no reading."""
        self.seq = 0
        self.sid: Optional[str] = None
        self.thread_id: Optional[str] = None
        self.trace_id = trace_id or str(_uuid.uuid4())
        self.route = "pending"
        self.slo_tracker = slo_tracker or AgentStreamSLOTracker(started_at=started_at)
        self.progress_steps: List[Dict[str, str]] = []

    def _record_progress(self, data: Dict[str, Any]) -> None:
        phase = data.get("phase")
        detail = data.get("detail")
        if phase not in _PROGRESS_PHASES or not isinstance(detail, str):
            return
        step = {"phase": phase, "detail": detail[:200]}
        if self.progress_steps[-1:] == [step]:
            return
        self.progress_steps.append(step)
        del self.progress_steps[:-_MAX_PROGRESS_STEPS]

    def seed_progress(self, steps: Any) -> None:
        for step in steps if isinstance(steps, list) else []:
            if isinstance(step, dict):
                self._record_progress(step)

    def set_context(
        self, *, thread_id: Optional[str] = None, route: Optional[str] = None
    ) -> None:
        if thread_id is not None:
            self.thread_id = thread_id
        if route is not None:
            self.route = route if route in _STREAM_ROUTES else "unknown"
            self.slo_tracker.set_route(self.route)

    async def start(
        self,
        thread_id: str,
        *,
        run_id: Optional[str] = None,
        continue_sequence: bool = False,
    ) -> None:
        if self.sid is not None:
            return
        self.set_context(thread_id=thread_id)
        if continue_sequence:
            try:
                previous_sid = (
                    await _stream_buffer.stream_id_for_run(run_id) if run_id else None
                )
                previous_frames = (
                    await _stream_buffer.read_after(previous_sid, 0)
                    if previous_sid
                    else []
                )
                if not previous_frames:
                    # Without a durable cursor high-water mark, publishing a
                    # fresh seq-1 active stream would hide its early frames from
                    # legacy no-?stream clients carrying the prior cursor. Stay
                    # live-only; a nested gate remains recoverable through the
                    # checkpoint-backed pending-confirmation replay.
                    logger.warning(
                        "Confirm stream sequence continuity unavailable; "
                        "continuing without a resumable buffer",
                        extra={"thread_id": thread_id, "run_id": run_id},
                    )
                    return
                # Continue the cursor only. Never copy old content into the new
                # buffer, where it could be mistaken for this resume's durable
                # output.
                self.seq = max(frame.seq for frame in previous_frames)
            except Exception:
                logger.warning(
                    "Failed to continue confirm stream sequence; continuing "
                    "without a resumable buffer",
                    exc_info=True,
                    extra={"thread_id": thread_id, "run_id": run_id},
                )
                return
        try:
            self.sid = await _stream_buffer.start_stream(thread_id, run_id=run_id)
        except Exception:
            logger.debug("stream_buffer.start_stream failed", exc_info=True)

    async def emit(
        self, event_type: str, data: Dict[str, Any], *, buffer: bool = True
    ) -> str:
        self.seq += 1
        if event_type == AgentStreamEvent.STATUS:
            self._record_progress(data)
        # Observability records the producer's truth; the byte budget applies
        # to what goes on the wire and into the resumable buffer (S2-M2).
        self.slo_tracker.record(event_type, data)
        data = _bound_frame_payload(str(event_type), data)
        frame = format_stream_envelope_frame(
            event_type,
            data,
            seq=self.seq,
            trace_id=self.trace_id,
            thread_id=self.thread_id,
            route=self.route,
            stream_id=self.sid,
        )
        if buffer and self.sid is not None:
            try:
                await _stream_buffer.append(self.sid, self.seq, frame)
            except Exception:
                # Best-effort, but never silent (audit S2-M1): a swallowed gap
                # makes the resumable ledger diverge from what clients saw.
                logger.warning(
                    "stream_buffer.append failed",
                    exc_info=True,
                    extra={
                        "stream_id": self.sid,
                        "seq": self.seq,
                        "event_type": event_type,
                    },
                )
        return frame

    async def finish(self) -> None:
        """Clear the thread's active-stream pointer after a terminal frame."""
        if self.sid is None or self.thread_id is None:
            return
        try:
            await _stream_buffer.finish_stream(self.thread_id, self.sid)
        except Exception:
            logger.debug("stream_buffer.finish_stream failed", exc_info=True)
        self.sid = None


async def replay_buffered_stream(
    request: Any,
    *,
    thread_id: str,
    stream_id: str,
    after: int = 0,
    wait_for_start: bool = False,
):
    """Replay and follow one immutable stream until its terminal frame."""
    last_seq = after
    saw_frame = False
    empty_start_polls = 0
    idle_polls = 0
    # Count of frames this loop has ever returned to its caller, threaded
    # back into read_after as the start_index hint (L15) so a long-running
    # replay stops re-fetching and re-parsing the whole buffer every poll.
    # Starts at 0 (full scan): on a resume with after > 0 we don't yet know
    # where that seq sits in the list, so there is nothing to hint.
    consumed = 0
    # Hard bound: ~10 minutes once the stream has started. A replayed POST can
    # win the small accept->buffer race, so give its original request 5 seconds
    # to publish the first frame without ever dispatching the graph again.
    for _ in range(600):
        frames = await _stream_buffer.read_after(
            stream_id, last_seq, start_index=consumed
        )
        if not frames:
            if await request.is_disconnected():
                return
            if await _stream_buffer.active_stream_id(thread_id) != stream_id:
                if wait_for_start and not saw_frame and empty_start_polls < 50:
                    empty_start_polls += 1
                    await asyncio.sleep(0.1)
                    continue
                # The terminal append and active-pointer delete are separate
                # Redis operations. Close their race with one final read.
                frames = await _stream_buffer.read_after(
                    stream_id, last_seq, start_index=consumed
                )
                if not frames:
                    return
        if frames:
            consumed += len(frames)
            idle_polls = 0
        for buffered in frames:
            saw_frame = True
            yield buffered.frame
            last_seq = buffered.seq
            event_line = next(
                (
                    line
                    for line in buffered.frame.split("\n")
                    if line.startswith("event: ")
                ),
                "",
            )
            if event_line.removeprefix("event: ") in TERMINAL_STREAM_EVENTS:
                return
        if not frames:
            # M13: the producer (planner/classifier/reflection) can go silent
            # for 20s+ with no buffered frame to replay. Left alone this loop
            # would poll for up to 600s yielding nothing, and a proxy with a
            # ~30s idle timeout kills the connection mid-run (see the
            # keepalive comment above _SSE_KEEPALIVE_SECONDS). A comment line
            # is spec-legal SSE that any client already has to tolerate
            # (frontend parsers only react to recognized `event:`/`data:`/
            # `id:` prefixes) — and, like the live path's `buffer=False`
            # heartbeat, it is connection liveness, not replayable content,
            # so it must never enter the resumable buffer.
            idle_polls += 1
            if idle_polls >= _SSE_KEEPALIVE_SECONDS:
                idle_polls = 0
                yield ": keepalive\n\n"
        if await request.is_disconnected():
            return
        # ponytail: poll-follow; pub/sub if latency matters
        await asyncio.sleep(1.0)


# Trace 019e6a0e: ~20s planner + internal LLM phases emit no SSE frames;
# idle connections get cut at ~30s. Comment keepalives reset proxy timers.
_SSE_KEEPALIVE_SECONDS = 10
_SSE_DISCONNECT_POLL_SECONDS = 0.5


def _get_disconnect_signal(request: Any) -> Optional[asyncio.Event]:
    signal = getattr(getattr(request, "state", None), AGENT_DISCONNECT_EVENT, None)
    return signal if isinstance(signal, asyncio.Event) else None


async def _request_disconnected(request: Any) -> bool:
    signal = _get_disconnect_signal(request)
    return bool(signal and signal.is_set()) or await request.is_disconnected()


async def _run_interrupted_cleanup(cleanup: Any) -> None:
    """Finish durable cleanup outside the request's cancelled AnyIO scope."""

    async def shielded_cleanup() -> None:
        with CancelScope(shield=True):
            await cleanup()

    cleanup_task = asyncio.create_task(shielded_cleanup())
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            continue
    # The outer cancellation can arrive on the same turn that the shielded
    # child finishes. In that case the loop observes ``done()`` after catching
    # CancelledError and would otherwise return without retrieving the child
    # exception, making a failed durable ACK look successful.
    cleanup_task.result()


async def _run_cancel_cleanup(
    *steps: tuple[str, Callable[[], Awaitable[Any]]],
) -> None:
    """Run every cancellation step and surface a failed durable ACK.

    Partial persistence and emitter cleanup are best-effort individually, but
    the terminal ``run.cancelled`` finalizer must remain observable. Running
    all steps before raising preserves stopped-output cleanup while ensuring a
    permanent database failure cannot be mistaken for an acknowledged Stop.
    ``_run_interrupted_cleanup`` shields this helper from ASGI cancellation.
    """
    first_error: Optional[BaseException] = None
    for label, step in steps:
        try:
            await step()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
            logger.error("Durable cancellation step failed: %s", label, exc_info=exc)
    if first_error is not None:
        raise first_error


def _cancel_current_task_on_disconnect(request: Any) -> Optional[asyncio.Task]:
    signal = _get_disconnect_signal(request)
    stream_task = asyncio.current_task()
    if signal is None or stream_task is None:
        return None

    async def cancel_stream() -> None:
        await signal.wait()
        stream_task.cancel()

    return asyncio.create_task(cancel_stream())


_PLANNER_CHAIN_NODES = frozenset(
    {
        "planner_node",
        "research_planner_node",
        "writing_planner_node",
        "data_planner_node",
    }
)


def _progress_status_for_event(kind: str, name: str) -> Optional[Dict[str, str]]:
    """Translate LangGraph internals into fixed, display-safe progress copy."""
    if kind == "on_tool_start":
        return {"phase": "planning", "detail": "Using the selected tools"}
    if kind != "on_chain_start":
        return None
    if name in _PLANNER_CHAIN_NODES:
        return {"phase": "planning", "detail": "Planning the response"}
    if name == "rag_node":
        return {"phase": "retrieving", "detail": "Reading relevant sources"}
    if name == "reflection_gate" or name.endswith("_reflection_gate"):
        return {"phase": "finalizing", "detail": "Checking the response"}
    return None


def _consume_pending_pull_result(pending: asyncio.Task) -> None:
    """Consume a detached pull result so asyncio never reports it as unhandled."""
    try:
        pending.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug("Detached agent stream pull failed", exc_info=True)


async def _close_async_iterator(iterator: Any) -> None:
    """Bound iterator shutdown so durable cancellation cannot wait on an LLM."""
    close_task = asyncio.create_task(iterator.aclose())
    try:
        done, _ = await asyncio.wait({close_task}, timeout=_SSE_DISCONNECT_POLL_SECONDS)
    except BaseException:
        close_task.cancel()
        if close_task.done():
            _consume_pending_pull_result(close_task)
        else:
            close_task.add_done_callback(_consume_pending_pull_result)
        raise
    if close_task in done:
        _consume_pending_pull_result(close_task)
        return
    close_task.cancel()
    await _cancel_pending_graph_pull(close_task, request_cancel=False)
    logger.warning(
        "Timed out closing agent stream iterator",
        extra={
            "event": "agent_stream_iterator_close_timeout",
            "cleanup_timeout_seconds": _SSE_DISCONNECT_POLL_SECONDS,
        },
    )


async def _cancel_pending_graph_pull(
    pending: asyncio.Task, *, request_cancel: bool = True
) -> None:
    """Cancel and briefly settle one pull without swallowing caller cancellation."""
    current_task = asyncio.current_task()
    cancellation_count = current_task.cancelling() if current_task else 0
    cancellation_active = cancellation_count > 0
    cancellation_exc: asyncio.CancelledError | None = None
    cleanup_deadline = time.monotonic() + _SSE_DISCONNECT_POLL_SECONDS

    if request_cancel and not pending.done():
        pending.cancel()
    while not pending.done():
        remaining = cleanup_deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(asyncio.shield(pending), timeout=remaining)
        except asyncio.TimeoutError:
            break
        except asyncio.CancelledError as exc:
            if (
                cancellation_exc is None
                and not cancellation_active
                and current_task is not None
                and current_task.cancelling() > cancellation_count
            ):
                cancellation_exc = exc
            continue
        except BaseException:
            break
    if pending.done():
        _consume_pending_pull_result(pending)
    else:
        logger.warning(
            "Timed out settling cancelled agent stream pull",
            extra={
                "event": "agent_stream_pull_cleanup_timeout",
                "cleanup_timeout_seconds": _SSE_DISCONNECT_POLL_SECONDS,
            },
        )
        pending.add_done_callback(_consume_pending_pull_result)
    if cancellation_exc is not None:
        raise cancellation_exc


async def _graph_events_with_keepalive(
    event_stream_iter,
    request: Any,
    stop_requested: Optional[Callable[[], Awaitable[bool]]] = None,
):
    """Yield LangGraph events, interleaving keepalive markers during long gaps.

    On client disconnect, emit a ``{"type": "disconnect"}`` sentinel and stop.
    A browser Stop is an explicit cancellation boundary even when resumable
    buffering is available; the caller closes the graph and records the
    terminal cancelled event instead of producing a later completion.
    """
    pending: asyncio.Task | None = None
    pending_cancel_requested = False
    started_at = time.monotonic()
    next_keepalive_at = started_at + _SSE_KEEPALIVE_SECONDS
    try:
        while True:
            if await _request_disconnected(request):
                if pending is not None:
                    pending.cancel()
                    pending_cancel_requested = True
                yield {"type": "disconnect"}
                return
            if stop_requested is not None and await stop_requested():
                if pending is not None:
                    pending.cancel()
                    pending_cancel_requested = True
                yield {"type": "stop"}
                return
            if pending is None:
                pending = asyncio.create_task(event_stream_iter.__anext__())
            done, _ = await asyncio.wait(
                {pending},
                timeout=min(
                    _SSE_DISCONNECT_POLL_SECONDS,
                    max(0, next_keepalive_at - time.monotonic()),
                ),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if pending in done:
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    pending = None
                    break
                except Exception:
                    pending = None
                    raise
                pending = None
                if stop_requested is not None and await stop_requested():
                    yield {"type": "stop"}
                    return
                yield {"type": "event", "event": event}
                continue

            if await _request_disconnected(request):
                pending.cancel()
                pending_cancel_requested = True
                yield {"type": "disconnect"}
                return
            if stop_requested is not None and await stop_requested():
                pending.cancel()
                pending_cancel_requested = True
                yield {"type": "stop"}
                return
            now = time.monotonic()
            if now >= next_keepalive_at:
                yield {
                    "type": "keepalive",
                    "elapsed_ms": int((now - started_at) * 1000),
                }
                next_keepalive_at = now + _SSE_KEEPALIVE_SECONDS
    finally:
        # Never leak the in-flight __anext__ task — on disconnect or error it
        # would otherwise drive one more graph step after we stop reading.
        if pending is not None:
            pending_pull = pending
            pending = None
            await _cancel_pending_graph_pull(
                pending_pull,
                request_cancel=not pending_cancel_requested,
            )


async def stream_event_generator(
    request_body: Any,  # AgentExecuteRequest
    request: Any,  # FastAPI Request
    current_user: User,
    *,
    background_tasks: Any = None,  # fastapi.BackgroundTasks (optional for tests)
    request_started_at: Optional[float] = None,
):
    """SSE event generator for the /stream endpoint.

    Yields SSE-formatted events: token, tool_start, tool_end,
    rag_context, plan, reflection, confirmation, done, error.

    ``request_started_at`` is the route handler's entry ``time.monotonic()``
    reading; it anchors the accepted-latency SLI to request arrival rather
    than to generator start (which happens after auth and rate limiting).

    Since P0-C the ``accepted`` frame follows the accept transaction rather
    than preceding it, so that SLI now covers thread resolution and the commit
    as well. That is intended: the number the SLI exists to report is how long
    until the client has a *trustworthy* acknowledgment, and an acknowledgment
    emitted before the write it describes was never worth measuring.
    """
    from src.services.agent.checkpointer import get_checkpointer, reset_checkpointer
    from src.services.agent.graph import compile_agent_graph
    from src.services.agent.memory import get_memory_store

    # Lazy import schemas to avoid circular imports
    from .execute import (
        AgentExecuteRequest,
        AgentMessage,
        PageContextRequest,
        ToolExecutionResponse,
    )

    stream_thread_id = request_body.thread_id or "unknown"
    trace_run_id = _uuid.uuid4()
    config: Dict[str, Any] = (
        {}
    )  # Initialize before try block for safe access in except handlers
    db = AsyncSessionLocal()
    graph = None  # type: ignore[assignment]
    resolved_thread_id: Optional[str] = None
    stream_started_at = time.monotonic()
    emitter = _SeqEmitter(
        trace_id=_request_trace_id(request),
        started_at=request_started_at,
    )
    client_disconnected = False
    # Set once an assistant row for this turn has been persisted/scheduled —
    # the error-path partial persist must never double-write the turn.
    assistant_persisted = False
    # Set just before a terminal frame (DONE/CONFIRMATION) is yielded — the
    # generic exception handler must never emit ERROR after a terminal
    # (audit S2-M3).
    terminal_frame_sent = False
    persist_partial_stop = None  # bound inside try once its inputs exist
    # Declared out here, not in the try: the CancelledError cleanup below reads
    # it to link the cancelled run to its stopped partial row, and that handler
    # can fire before the try body has run.
    persisted_assistant_id: Optional[str] = None
    event_stream_iter = None
    # Set once the accept transaction has COMMITTED (P0-C). Everything after
    # the accepted frame keys off this: the run's ledger, the outbox stamp and
    # the terminal status all address `acceptance.run_id`.
    acceptance: Optional[AcceptedSubmission] = None
    org_id = getattr(current_user, "organization_id", None)
    latest_user_message = next(
        (
            message
            for message in reversed(request_body.messages)
            if message.role == "user"
        ),
        None,
    )
    client_message_id = getattr(latest_user_message, "client_message_id", None)
    disconnect_canceller = _cancel_current_task_on_disconnect(request)
    try:
        # Resolve the thread and verify ownership BEFORE acknowledging anything
        # — the acknowledgment is now a claim about durable state, so it cannot
        # precede the write it describes.
        thread_obj, _conversation_id = await _resolve_thread(
            db, current_user, request_body
        )
        if thread_obj is not None:
            resolved_thread_id = str(thread_obj.id)
            if request_body.thread_id != resolved_thread_id:
                request_body.thread_id = resolved_thread_id
        elif request_body.thread_id:
            # Degraded path: `_resolve_thread` returned None, so the ownership
            # join matched nothing AND no live workspace existed to create a
            # thread under. The client's thread id was therefore never verified
            # — it may name another tenant's thread — and it must not survive
            # into the graph config below, where it becomes the CHECKPOINT KEY
            # (`configurable.thread_id`). `astream_events` against a
            # checkpointer merges this turn into whatever checkpoint that key
            # names: the other user's history would enter the model context and
            # stream back as tokens, this turn would be appended to their
            # checkpoint, and the checkpoint's `user_id` channel would be
            # overwritten with ours — locking them out of their own
            # `/stream/confirm` (see the ownership check in
            # `stream_confirm_event_generator`). Null it, exactly as `/execute`
            # already does on this same miss, so `stream_thread_id` falls back
            # to a fresh UUID and the turn runs in an ephemeral checkpoint.
            logger.warning(
                "Discarding unverified thread id on the degraded stream path: "
                "no owned thread and no workspace to create one",
                extra={"user_id": str(current_user.id)},
            )
            request_body.thread_id = None

        # ------------------------------------------------------------------
        # Atomic accept (P0-C). One transaction commits the user message, the
        # run row, its `run.created` event and the outbox dispatch record; the
        # accepted frame below is emitted only afterwards and carries the real
        # run id. A failure here raises into the error handler: no `accepted`,
        # an `error` frame, and the rollback guarantees nothing half-written.
        #
        # Degraded path: a user with no workspace has no durable thread
        # (`_resolve_thread` returns None), so there is nothing to make atomic.
        # That turn still streams an answer exactly as before, with a null run
        # id on the accepted frame — unchanged behaviour, not a new hole.
        # ------------------------------------------------------------------
        if _accept_eligible(thread_obj):
            acceptance = await accept_submission(
                db,
                current_user=current_user,
                request=request_body,
                thread=thread_obj,
            )
        else:
            logger.info(
                "Agent stream accepted without a durable run: no ownership-"
                "verified thread for this submission",
                extra={"thread_id": request_body.thread_id},
            )

        if acceptance is not None and acceptance.replayed:
            # Idempotency means execution-once, not merely row-once. Reattach
            # this retry to the immutable run-scoped buffer and return before
            # route selection, graph compilation, tools, or dispatch.
            replayed_run = await get_run(
                db,
                acceptance.run_id,
                organization_id=org_id,
                user_id=current_user.id,
            )
            if (
                replayed_run is None
                or str(replayed_run.thread_id) != resolved_thread_id
            ):
                yield await emitter.emit(
                    AgentStreamEvent.ERROR,
                    error_frame_payload(
                        "This retry does not belong to this thread.",
                        AgentErrorCategory.CONFLICT,
                    ),
                    buffer=False,
                )
                return

            # The accepting request commits before it opens the Redis buffer.
            # A duplicate can therefore observe a valid QUEUED row during that
            # short startup window. Give the original request the same bounded
            # five-second grace period used by the pre-audit replay path before
            # deciding that its dispatch was abandoned.
            replay_sid = None
            replay_deadline = time.monotonic() + _REPLAY_STREAM_START_GRACE_S
            while True:
                replay_sid = await _stream_buffer.stream_id_for_run(acceptance.run_id)
                if replay_sid is not None:
                    break
                if await request.is_disconnected():
                    return
                remaining = replay_deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(_REPLAY_STREAM_POLL_INTERVAL_S, remaining))

            if replay_sid is None:
                abandoned_message = "The original response did not start. Please retry."
                # Replay acceptance only read durable state. End that snapshot
                # before the compare-and-set so it observes a dispatcher that
                # committed during the bounded buffer grace period.
                await db.rollback()
                abandoned = await fail_queued_submission(
                    db,
                    run_id=acceptance.run_id,
                    thread_id=resolved_thread_id,
                    organization_id=org_id,
                    user_id=current_user.id,
                    error_code="stream_not_dispatched",
                    error=abandoned_message,
                    payload={
                        "code": "stream_not_dispatched",
                        "message": abandoned_message,
                    },
                )
                message = (
                    abandoned_message
                    if abandoned
                    else "The original response stream is unavailable."
                )
                yield await emitter.emit(
                    AgentStreamEvent.ERROR,
                    error_frame_payload(
                        message,
                        category=AgentErrorCategory.CONFLICT,
                    ),
                    buffer=False,
                )
                return
            async for frame in replay_buffered_stream(
                request,
                thread_id=acceptance.thread_id,
                stream_id=replay_sid,
                wait_for_start=True,
            ):
                yield frame
            return

        # Open the durable run's buffer before acknowledging it so a retry can
        # always attach through run_id, even if it races this generator.
        if acceptance is not None:
            await emitter.start(acceptance.thread_id, run_id=acceptance.run_id)

        # First server-sourced progress signal — now a post-commit fact. Durable
        # submissions buffer it; the no-thread degraded path remains live-only.
        yield await emitter.emit(
            AgentStreamEvent.STATUS,
            {
                "phase": "accepted",
                "detail": "Request accepted",
                "run_id": acceptance.run_id if acceptance is not None else None,
            },
            buffer=acceptance is not None,
        )

        from src.core.config import get_settings
        from src.services.agent.fast_path import classify_fast_path_turn

        settings = get_settings()
        page_context = _page_context_to_dict(request_body.page_context)
        fast_decision = classify_fast_path_turn(
            messages=request_body.messages,
            page_context=page_context,
            use_rag=request_body.use_rag,
            max_input_chars=settings.AGENT_FAST_PATH_MAX_INPUT_CHARS,
            has_attachments=bool(request_body.attachment_ids),
        )
        if (
            settings.AGENT_FAST_PATH_ENABLED
            and fast_decision.eligible
            and resolved_thread_id is not None
        ):
            from langsmith.run_helpers import trace

            emitter.set_context(route="luna")
            trace_metadata = build_trace_metadata(
                trace_source=TraceSource.NON_GRAPH,
                user_id=current_user.id,
                org_id=org_id,
                thread_id=(
                    acceptance.thread_id
                    if acceptance is not None
                    else resolved_thread_id
                ),
                request_id=emitter.trace_id,
                agent_run_id=(acceptance.run_id if acceptance is not None else None),
                user_message_id=(
                    acceptance.user_message_id if acceptance is not None else None
                ),
                client_message_id=client_message_id,
            )
            async with trace(
                "luna_fast_path",
                run_type="chain",
                metadata=trace_metadata,
                run_id=trace_run_id,
                exceptions_to_handle=(asyncio.CancelledError, GeneratorExit),
            ):
                fast_path_iter = _stream_luna_fast_path(
                    request_body=request_body,
                    request=request,
                    current_user=current_user,
                    db=db,
                    resolved_thread_id=resolved_thread_id,
                    emitter=emitter,
                    stream_started_at=stream_started_at,
                    trace_run_id=trace_run_id,
                    acceptance=acceptance,
                )
                try:
                    async for frame in fast_path_iter:
                        yield frame
                finally:
                    await _close_async_iterator(fast_path_iter)
            return

        # Edit-and-resend: whichever writer owned this turn's user row also
        # owned its tombstones, so the report is sourced from that writer —
        # the accept transaction (P0-C) when it ran, the guarded persist on the
        # degraded no-durable-run path.
        tombstones = TombstoneReport()
        if acceptance is not None:
            if acceptance.tombstoned:
                tombstones.mark()
        elif thread_obj is not None:
            # Degraded accept (no durable run): keep the pre-P0-C
            # durable-before-LLM guarantee for the user row. When the accept
            # committed, that row is already in it — writing it again here
            # would insert a SECOND row for a legacy client that sends no
            # client_message_id (nothing for ON CONFLICT to infer).
            await _persist_user_message_guarded(
                db,
                current_user,
                request_body,
                tombstoned_out=tombstones,
            )

        _bootstrap_langsmith()
        checkpointer = await get_checkpointer()
        store = await get_memory_store()
        graph = compile_agent_graph(checkpointer=checkpointer, store=store)

        # Edit-and-resend: rebuild the HEAD checkpoint from the post-edit DB
        # BEFORE assembling model input, so the model never sees the superseded
        # prompt or its answer. Never rewinds to an earlier checkpoint_id —
        # that forks the thread while every other writer targets head.
        if tombstones.any:
            await resync_thread_checkpoint(
                graph,
                thread_id=resolved_thread_id or stream_thread_id,
                user=current_user,
            )

        messages = None
        if get_settings().AGENT_SERVER_SIDE_HISTORY:
            # Option B: rebuild context from the checkpoint (seeding from the DB
            # when empty); ignore all but the newest turn in the request array.
            # Best-effort: a DB/checkpoint failure (or a newest turn lacking a
            # client_message_id -> None) falls back to the legacy path below so a
            # turn that works today is never aborted by the opt-in path.
            try:
                # Seed only from the ownership-verified thread id (set by
                # _resolve_thread); never the raw client-supplied thread_id.
                messages = await _jobs_mod.build_graph_input_messages(
                    db, graph, resolved_thread_id or "", request_body.messages
                )
            except Exception:
                logger.warning(
                    "Option B message assembly failed; using legacy history",
                    exc_info=True,
                )
                messages = None
        if messages is None:
            # Legacy path (flag off, or Option B declined/failed): B1's
            # deterministic-id rebuild of the resent history.
            messages = _jobs_mod.build_user_history_messages(
                request_body.messages, request_body.thread_id or ""
            )

        # Returns the ownership-verified project id (or None). R7-M2: gate
        # memory recall on that, not on the raw page_context value the client
        # supplied, so the check can never drift away from its consumer.
        _pm_project_id = await _resolve_and_bind_project(
            db, current_user, thread_obj, page_context
        )

        # Project-scoped memory recall (best-effort; never blocks a turn).
        project_memories: list = []
        if _pm_project_id:
            try:
                from src.services.research.project_memory_service import (
                    load_project_memories,
                )

                project_memories = await load_project_memories(
                    db,
                    str(_pm_project_id),
                    # Tenancy hard-stop (S2-M12): scope by org so a raw-id
                    # caller can never pull another tenant's memories, while
                    # project-shared memories from peers in the same org
                    # remain visible. Deliberately NOT user-filtered.
                    organization_id=getattr(current_user, "organization_id", None),
                )
            except Exception:
                logger.warning("project memory load failed", exc_info=True)

        from src.services.agent.runtime_snapshot import (
            create_runtime_snapshot,
            runtime_config_fields,
            runtime_state_fields,
        )

        runtime_snapshot = await create_runtime_snapshot(
            db,
            user_id=current_user.id,
            project_id=page_context.get("project_id"),
            thread_id=getattr(thread_obj, "id", None),
        )

        initial_state = {
            "messages": messages,
            "page_context": page_context,
            "retrieved_contexts": [],
            "attachment_ids": [
                str(document_id) for document_id in (request_body.attachment_ids or [])
            ],
            "attachment_status": [],
            "tool_executions": [],
            "thread_id": request_body.thread_id or "",
            "thread_persistence": (
                THREAD_PERSISTENCE_DURABLE
                if resolved_thread_id is not None
                else THREAD_PERSISTENCE_EPHEMERAL
            ),
            "turn_index": 0,
            "tool_loop_count": 0,
            "error_count": 0,
            "last_error": "",
            "pending_confirmation": {},
            "user_confirmed": False,
            "intent": "",
            "user_memories": [],
            "project_memories": project_memories,
            "plan": [],
            "plan_reasoning": "",
            "reflection_count": 0,
            "compaction_count": 0,
            "intent_confidence": 0.0,
            "last_error_info": {},
            "user_id": str(current_user.id),
            "model": request_body.model,
            "use_rag": request_body.use_rag,
            **runtime_state_fields(runtime_snapshot, page_context.get("project_id")),
        }

        stream_thread_id = request_body.thread_id or str(_uuid.uuid4())
        config = {
            "recursion_limit": RECURSION_LIMIT,
            "run_name": "agent:stream",
            "run_id": trace_run_id,
            # Ids only (audit B8): graph nodes/tools open their own
            # tool_session() and re-load the user org-scoped — never smuggle
            # the live AsyncSession / ORM User through LangGraph config.
            "configurable": {
                "thread_id": stream_thread_id,
                "user_id": str(current_user.id),
                "organization_id": str(
                    getattr(current_user, "organization_id", "") or ""
                ),
                "page_context": page_context,
                **runtime_config_fields(
                    runtime_snapshot.id, page_context.get("project_id")
                ),
            },
            # LangSmith run metadata — per-tenant/turn filterable traces.
            # Inherited by child runs; never carries secrets.
            "metadata": build_trace_metadata(
                trace_source=TraceSource.GRAPH,
                user_id=current_user.id,
                org_id=org_id,
                thread_id=(
                    acceptance.thread_id
                    if acceptance is not None
                    else resolved_thread_id
                ),
                request_id=emitter.trace_id,
                agent_run_id=(acceptance.run_id if acceptance is not None else None),
                user_message_id=(
                    acceptance.user_message_id if acceptance is not None else None
                ),
                client_message_id=client_message_id,
            ),
        }

        emitter.set_context(route="graph")
        await emitter.start(stream_thread_id)

        yield await emitter.emit(
            AgentStreamEvent.STATUS,
            {"phase": "routing", "detail": "Choosing the safest response path"},
        )

        yield await emitter.emit(
            AgentStreamEvent.TRACE,
            build_trace_payload(
                thread_id=config["configurable"]["thread_id"],
                cli_session_id="",
                langsmith_run_id=str(config["run_id"]),
            ),
        )

        if acceptance is not None:

            async def durable_stop_requested() -> bool:
                try:
                    return await is_run_cancellation_requested(
                        db,
                        acceptance.run_id,
                        organization_id=org_id,
                        user_id=current_user.id,
                    )
                except Exception:
                    logger.warning(
                        "Failed to poll durable stop marker for graph run %s",
                        acceptance.run_id,
                        exc_info=True,
                    )
                    return False

            # This compare-and-set is the execution claim. A replay may have
            # abandoned a still-queued run while this generator was paused at
            # an earlier SSE yield; losing the claim means no graph work may
            # start, even though this request originally accepted the turn.
            dispatch_claimed = await mark_submission_dispatched(
                db,
                run_id=acceptance.run_id,
                outbox_id=acceptance.outbox_id,
                organization_id=org_id,
                user_id=current_user.id,
            )
            if dispatch_claimed is False:
                if await durable_stop_requested():

                    async def finalize_queued_cancel() -> None:
                        await _finalize_run(
                            db,
                            acceptance,
                            current_user,
                            status=JobStatus.CANCELLED,
                            event_type=RunEventType.RUN_CANCELLED,
                            payload={
                                "reason": "user_requested",
                                "request_id": emitter.trace_id,
                            },
                        )

                    await _run_cancel_cleanup(
                        ("finish_emitter", emitter.finish),
                        ("finalize_cancelled", finalize_queued_cancel),
                    )
                    return
                yield await emitter.emit(
                    AgentStreamEvent.ERROR,
                    error_frame_payload(
                        "This response is no longer active. Please retry.",
                        AgentErrorCategory.CONFLICT,
                    ),
                )
                await emitter.finish()
                return

        # Per-turn token accounting. Aggregated across every chat model call
        # in the graph (planner, intent classifier, llm_node, reflection…)
        # and emitted as a single `usage` SSE event right before `done`.
        turn_input_tokens = 0
        turn_output_tokens = 0

        # Drop any stale HITL interrupt left over from a previous turn the
        # user abandoned (e.g. /new in the CLI). A fresh HumanMessage cannot
        # resume an interrupt, so re-firing it would block this turn.
        await _clear_stale_pending_confirmation(graph, config)

        # If the checkpointer's pgbouncer/Supabase connection was
        # idle-killed since the singleton was built, the first aget_tuple
        # inside astream_events raises psycopg.OperationalError("the
        # connection is closed"). Reset + rebuild + retry once before
        # failing the whole stream.
        from psycopg import OperationalError as _PgOpError

        async def _open_event_stream():
            return graph.astream_events(
                initial_state, config=config, version="v2"
            ).__aiter__()

        event_stream_iter = await _open_event_stream()
        first_event_yielded = False
        streamed_token = False
        context_fingerprint: Optional[str] = None
        # Canonical cumulative source snapshot corresponding to the latest
        # rag_context marker emitted on this stream. An explicit [] is state,
        # not absence: it clears sources emitted earlier in the turn.
        latest_retrieved_contexts: List[Dict[str, Any]] = []
        # persisted_assistant_id is hoisted to the function top (see there).
        completed_root_values: Optional[Dict[str, Any]] = None
        # Accumulated user-facing tokens, so a client abort can persist the
        # partial answer server-side (stopped=True) instead of losing it.
        streamed_parts: List[str] = []
        reasoning_summary = ""
        # Deterministic assistant-side idempotency key derived from the user
        # turn's client_message_id: an SSE retry of the same turn maps to the
        # same key, so the assistant-role partial unique index dedupes it.
        assistant_cmid: Optional[str] = None
        if client_message_id is not None:
            assistant_cmid = str(
                _uuid.uuid5(
                    _uuid.NAMESPACE_URL,
                    f"nous-assistant:{client_message_id}",
                )
            )

        async def persist_partial_stop(*, force_inline: bool = False) -> None:
            """Persist the accumulated partial answer with stopped=True.

            Shared by the legacy (no stream buffer) disconnect branch and the
            error path when a disconnected drain dies mid-run (e.g. the 300s
            timeout) — without it that partial would be silently lost.
            """
            nonlocal assistant_persisted, persisted_assistant_id
            partial = "".join(streamed_parts)
            if assistant_persisted:
                if persisted_assistant_id and resolved_thread_id is not None:
                    with contextlib.suppress(BaseException):
                        await _jobs_mod._mark_assistant_message_stopped_safe(
                            thread_id=resolved_thread_id,
                            message_id=persisted_assistant_id,
                        )
                return
            if resolved_thread_id is None or (not partial and not reasoning_summary):
                return
            assistant_persisted = True
            stop_kwargs = dict(
                thread_id=resolved_thread_id,
                content=partial,
                model_name=request_body.model,
                tool_executions_out=None,
                retrieved_contexts=latest_retrieved_contexts,
                latency_ms=int((time.monotonic() - stream_started_at) * 1000),
                ttft_ms=_ttft_ms(emitter, stream_started_at),
                stopped=True,
                client_message_id=assistant_cmid,
                reasoning_summary=reasoning_summary or None,
                progress_steps=emitter.progress_steps or None,
                # Tokens accumulated up to the abort; no plan here — it
                # would need a checkpoint read on a path that must stay
                # cheap (client already hung up).
                token_usage=(
                    {
                        "input_tokens": turn_input_tokens,
                        "output_tokens": turn_output_tokens,
                    }
                    if (turn_input_tokens or turn_output_tokens)
                    else None
                ),
            )
            if background_tasks is not None and not force_inline:
                # Deferred to a background task: no id to capture here. Callers
                # that need the run linked to its partial row (cancellation)
                # pass force_inline=True and take the awaited branch below.
                background_tasks.add_task(
                    _jobs_mod._persist_assistant_message_safe, **stop_kwargs
                )
            else:
                persisted_assistant_id = (
                    await _jobs_mod._persist_assistant_message_safe(**stop_kwargs)
                )

        async def cancel_current_stream(reason: str = "client_disconnected") -> None:
            """Durably clean up a stopped graph stream."""

            async def close_graph_events() -> None:
                if event_stream_iter is not None:
                    # ASGI may cancel the child iterator while it is already
                    # being closed. That is expected transport cleanup; the
                    # durable finalizer below is the ACK-bearing step.
                    with contextlib.suppress(asyncio.CancelledError, GeneratorExit):
                        await _close_async_iterator(event_stream_iter)

            async def finalize_cancelled() -> None:
                cancelled_payload: Dict[str, Any] = {
                    "reason": reason,
                    "request_id": emitter.trace_id,
                }
                # Link the run to the stopped partial row persisted just above,
                # so a cancelled run can still name the message holding its
                # output. Omitted when nothing streamed before the abort.
                if persisted_assistant_id:
                    cancelled_payload["assistant_message_id"] = persisted_assistant_id
                await _finalize_run(
                    db,
                    acceptance,
                    current_user,
                    status=JobStatus.CANCELLED,
                    event_type=RunEventType.RUN_CANCELLED,
                    payload=cancelled_payload,
                )

            await _run_cancel_cleanup(
                ("close_graph_events", close_graph_events),
                (
                    "persist_partial_stop",
                    lambda: persist_partial_stop(force_inline=True),
                ),
                ("finish_emitter", emitter.finish),
                ("finalize_cancelled", finalize_cancelled),
            )

        if acceptance is not None and await durable_stop_requested():
            await cancel_current_stream(reason="user_requested")
            return

        async with asyncio.timeout(300):  # 5 minutes
            while True:
                try:
                    async for item in _graph_events_with_keepalive(
                        event_stream_iter,
                        request,
                        stop_requested=(
                            durable_stop_requested if acceptance is not None else None
                        ),
                    ):
                        if item["type"] == "disconnect":
                            client_disconnected = True
                            break
                        if item["type"] == "stop":
                            await cancel_current_stream(reason="user_requested")
                            return
                        if item["type"] == "keepalive":
                            elapsed_ms = int(
                                (time.monotonic() - stream_started_at) * 1000
                            )
                            frame = await emitter.emit(
                                AgentStreamEvent.HEARTBEAT,
                                {"elapsed_ms": elapsed_ms},
                                buffer=False,
                            )
                            if not client_disconnected:
                                yield frame
                            continue

                        event = item["event"]
                        first_event_yielded = True
                        kind = event.get("event", "")
                        name = event.get("name", "")

                        progress_status = _progress_status_for_event(kind, name)
                        if progress_status is not None:
                            frame = await emitter.emit(
                                AgentStreamEvent.STATUS, progress_status
                            )
                            if not client_disconnected:
                                yield frame

                        context_snapshot, context_fingerprint = (
                            _changed_retrieved_context_snapshot(
                                event, context_fingerprint
                            )
                        )
                        if context_snapshot is not None:
                            latest_retrieved_contexts = context_snapshot
                            frame = await emitter.emit(
                                AgentStreamEvent.RAG_CONTEXT,
                                {"contexts": context_snapshot},
                            )
                            if not client_disconnected:
                                yield frame

                        if kind == "on_chain_end" and not event.get("parent_ids"):
                            output = event.get("data", {}).get("output")
                            if isinstance(output, dict):
                                messages_out = output.get("messages") or []
                                last_message = (
                                    messages_out[-1] if messages_out else None
                                )
                                if last_message is not None and not getattr(
                                    last_message, "tool_calls", None
                                ):
                                    completed_root_values = output

                        if kind == "on_chat_model_stream":
                            if not _is_user_facing_token_event(event):
                                continue
                            chunk = event.get("data", {}).get("chunk")
                            reasoning_delta = (
                                _chunk_reasoning_summary(chunk) if chunk else ""
                            )
                            if reasoning_delta:
                                reasoning_summary = _append_reasoning_summary(
                                    reasoning_summary, reasoning_delta
                                )
                                frame = await emitter.emit(
                                    AgentStreamEvent.REASONING_DELTA,
                                    {"content": reasoning_delta},
                                )
                                if not client_disconnected:
                                    yield frame
                            # _chunk_text, not chunk.content: on the Responses
                            # API content is a list of typed blocks, and the
                            # reasoning ones must not reach the wire.
                            chunk_text = _chunk_text(chunk) if chunk else ""
                            if chunk_text:
                                if not streamed_token:
                                    frame = await emitter.emit(
                                        AgentStreamEvent.STATUS,
                                        {
                                            "phase": "writing",
                                            "detail": "Drafting the response",
                                        },
                                    )
                                    if not client_disconnected:
                                        yield frame
                                streamed_token = True
                                # Buffer BEFORE recording for persistence.
                                # streamed_parts feeds the stopped partial row;
                                # emit() feeds the resumable buffer. Appending
                                # first meant a cancellation inside this await
                                # persisted a chunk no replay ever saw. This
                                # order can only lose the last chunk instead,
                                # keeping the persisted partial a prefix of the
                                # buffered stream.
                                frame = await emitter.emit(
                                    AgentStreamEvent.TOKEN, {"content": chunk_text}
                                )
                                streamed_parts.append(chunk_text)
                                if not client_disconnected:
                                    yield frame

                        elif kind == "on_chat_model_end":
                            inp, out = _extract_usage_tokens(event)
                            turn_input_tokens += inp
                            turn_output_tokens += out

                        elif kind == "on_tool_start":
                            tool_input = event.get("data", {}).get("input", {})
                            args_preview = _tool_args_preview(tool_input)
                            frame = await emitter.emit(
                                AgentStreamEvent.TOOL_START,
                                {
                                    "tool": name,
                                    "args": args_preview,
                                    **_tool_event_identity(event),
                                },
                            )
                            if not client_disconnected:
                                yield frame

                        elif kind == "on_tool_end":
                            output = event.get("data", {}).get("output", "")
                            is_error = (
                                isinstance(output, dict) and bool(output.get("isError"))
                            ) or (getattr(output, "status", None) == "error")
                            frame = await emitter.emit(
                                AgentStreamEvent.TOOL_END,
                                {
                                    "tool": name,
                                    "result": _encode_tool_result(output),
                                    "is_error": is_error,
                                    **_tool_event_identity(event),
                                },
                            )
                            if not client_disconnected:
                                yield frame

                        elif kind == "on_chain_end" and name in _PLANNER_CHAIN_NODES:
                            output = event.get("data", {}).get("output", {})
                            if isinstance(output, dict):
                                plan_steps = output.get("plan", [])
                                plan_reasoning = output.get("plan_reasoning") or ""
                                if plan_steps or plan_reasoning:
                                    frame = await emitter.emit(
                                        AgentStreamEvent.PLAN,
                                        {
                                            "steps": plan_steps,
                                            "reasoning": plan_reasoning,
                                        },
                                    )
                                    if not client_disconnected:
                                        yield frame

                        elif kind == "on_chain_end" and name == "reflection_gate":
                            output = event.get("data", {}).get("output", {})
                            if isinstance(output, dict):
                                reflection_result = output.get("_reflection_result")
                                if reflection_result is not None:
                                    passed = getattr(reflection_result, "passed", True)
                                    issues = getattr(reflection_result, "issues", [])
                                    round_num = output.get("reflection_count", 0)
                                    severity = getattr(
                                        reflection_result, "severity", "none"
                                    )
                                    revising = (
                                        (not passed)
                                        and severity == "major"
                                        and round_num < 2
                                    )
                                    frame = await emitter.emit(
                                        AgentStreamEvent.REFLECTION,
                                        {
                                            "passed": passed,
                                            "issues": issues,
                                            "round": round_num,
                                            "revising": revising,
                                        },
                                    )
                                    if not client_disconnected:
                                        yield frame
                    break
                except _PgOpError as op_err:
                    if first_event_yielded:
                        raise
                    logger.warning(
                        "stream: checkpointer connection dead (%s); "
                        "resetting and retrying",
                        op_err,
                    )
                    await reset_checkpointer()
                    checkpointer = await get_checkpointer()
                    graph = compile_agent_graph(checkpointer=checkpointer, store=store)
                    await _clear_stale_pending_confirmation(graph, config)
                    event_stream_iter = await _open_event_stream()
                    continue

        if acceptance is not None and await durable_stop_requested():
            await cancel_current_stream(reason="user_requested")
            return

        # Client hung up mid-stream (hit Stop / closed the tab). Cancel the
        # agent run by closing the graph iterator instead of letting it finish
        # generating into a dead socket. Persist the partial answer
        # server-side with stopped=True — server-canonical clients no longer
        # save their own copy, so without this an aborted turn would leave
        # the thread with a user message and no assistant row at all.
        # A browser Stop is terminal even when Redis buffering is active: close
        # the graph, keep the partial answer, clear the buffer pointer, and
        # persist run.cancelled before returning.
        if client_disconnected:
            logger.info(
                "SSE client disconnected; cancelled agent run for thread %s",
                stream_thread_id,
            )
            await cancel_current_stream()
            return

        # Check graph state after streaming completes
        tool_executions_out: Optional[list] = None
        try:
            final_snapshot = None
            if completed_root_values is not None:
                final_values = completed_root_values
            else:
                final_snapshot = await graph.aget_state(config)
                final_values = final_snapshot.values if final_snapshot else {}

            # Turn-scoped: the current turn's answer, or "" when the turn
            # produced none (parked on an interrupt, or genuinely empty).
            new_turn_text = _latest_turn_assistant_text(
                final_values.get("messages", [])
            )

            # The completed_root_values fast path carries no tasks view, so a
            # subgraph interrupt there looked exactly like "completed with no
            # output" — has_interrupt read False, the old whole-state scan
            # surfaced the PREVIOUS turn's answer, and the client got a
            # duplicated bubble + persisted row instead of the approval gate
            # (thread 014caf59, 2026-08-12). When the turn has no new answer
            # and no snapshot, fetch one so the interrupt check is real — and
            # adopt its values: the stale root dict is exactly what hid this
            # turn's writes, so tool_executions and the turn text must come
            # from the fresh view too (PR #1410 review).
            if final_snapshot is None and not new_turn_text:
                final_snapshot = await graph.aget_state(config)
                if final_snapshot is not None and final_snapshot.values:
                    final_values = final_snapshot.values
                    new_turn_text = _latest_turn_assistant_text(
                        final_values.get("messages", [])
                    )

            # Check for pending interrupts (HITL confirmation needed)
            pending_tasks = final_snapshot.tasks if final_snapshot else ()
            has_interrupt = any(getattr(t, "interrupts", None) for t in pending_tasks)

            if has_interrupt:
                if acceptance is not None and await durable_stop_requested():
                    await cancel_current_stream(reason="user_requested")
                    return
                # Extract confirmation details from the interrupt
                confirmation_details = {}
                for task in pending_tasks:
                    for intr in getattr(task, "interrupts", []):
                        confirmation_details = getattr(intr, "value", {})
                        break
                    if confirmation_details:
                        break

                thread_id = config["configurable"]["thread_id"]
                # Park the durable run before publishing the confirmation. A
                # Stop request can claim RUNNING -> STOPPING between the poll
                # above and this write; a guarded park then returns False. In
                # that race the producer is the only actor that can publish
                # the cancellation ACK, so clean it up here and never leave a
                # client-facing approval card for a run that is stopping.
                try:
                    parked = await _finalize_run(
                        db,
                        acceptance,
                        current_user,
                        status=JobStatus.AWAITING_CONFIRMATION,
                        run_metadata={
                            "progress_steps": emitter.progress_steps,
                            "reasoning_summary": reasoning_summary or None,
                        },
                    )
                except Exception:
                    parked = None
                    # Preserve the existing best-effort HITL behavior for a
                    # transient bookkeeping failure: the checkpoint remains
                    # recoverable and the confirmation can be retried. A
                    # guarded False is handled above because it is a known
                    # competing terminal transition, rather than an unknown
                    # database failure.
                    logger.error(
                        "Failed to park stream run AWAITING_CONFIRMATION",
                        exc_info=True,
                        extra={"thread_id": thread_id},
                    )
                if parked is False:
                    if acceptance is not None and await durable_stop_requested():
                        await cancel_current_stream(reason="user_requested")
                    else:
                        await emitter.finish()
                    return
                confirmation_payload = {
                    "thread_id": thread_id,
                    "confirmation": confirmation_details,
                }
                if acceptance is not None:
                    # Keep the parked run identity on the wire. A resumed
                    # browser must use this exact id when issuing Stop.
                    confirmation_payload["run_id"] = acceptance.run_id
                frame = await emitter.emit(
                    AgentStreamEvent.CONFIRMATION,
                    confirmation_payload,
                )
                terminal_frame_sent = True
                if not client_disconnected:
                    yield frame
                await emitter.finish()
                return

            # Turn-scoped (computed above): "" when this turn produced no AI
            # text — never the previous turn's answer.
            # Turn-scoped text; if the checkpoint view still lags a turn the
            # user WATCHED stream, fall back to the streamed tokens themselves
            # — never persist an empty row for a streamed answer.
            assistant_content = new_turn_text or (
                "".join(streamed_parts) if streamed_token else ""
            )

            # Surface a final answer that was produced WITHOUT streaming — the
            # greeting fast-path, a templated/degraded reply, or force_synthesis
            # set the AIMessage directly and emit no on_chat_model_stream chunks.
            # Without this the client receives zero `token` events and renders an
            # empty response ("stream completed without any tokens").
            if not streamed_token and assistant_content:
                if acceptance is not None and await durable_stop_requested():
                    await cancel_current_stream(reason="user_requested")
                    return
                frame = await emitter.emit(
                    AgentStreamEvent.STATUS,
                    {"phase": "writing", "detail": "Drafting the response"},
                )
                if not client_disconnected:
                    yield frame
                frame = await emitter.emit(
                    AgentStreamEvent.TOKEN, {"content": assistant_content}
                )
                if not client_disconnected:
                    yield frame

            tool_executions_out = [
                ToolExecutionResponse(**te)
                for te in final_values.get("tool_executions", [])
            ] or None

            if acceptance is not None and await durable_stop_requested():
                await cancel_current_stream(reason="user_requested")
                return
            frame = await emitter.emit(
                AgentStreamEvent.STATUS,
                {"phase": "finalizing", "detail": "Saving the response"},
            )
            if not client_disconnected:
                yield frame

            # User row was already persisted up-front (before the LLM call).
            # Defer the assistant-row commit to a FastAPI BackgroundTask so
            # the SSE `done` event releases the response without waiting on
            # one more DB roundtrip — Task 5 of
            # docs/plans/2026-05-13-agent-persist-perf.md. Resolved late
            # via the jobs module so tests can monkeypatch the safe
            # wrapper at runtime.
            #
            # Nothing-happened turns (no streamed tokens, no turn-scoped
            # answer, no tool executions) persist NO assistant row: writing
            # one would fabricate an empty transcript entry for a turn the
            # client already surfaces as "no response received".
            if resolved_thread_id is not None and (
                streamed_token
                or assistant_content
                or tool_executions_out
                or reasoning_summary
            ):
                if acceptance is not None and await durable_stop_requested():
                    await cancel_current_stream(reason="user_requested")
                    return
                persist_kwargs = dict(
                    thread_id=resolved_thread_id,
                    content=assistant_content,
                    model_name=request_body.model,
                    tool_executions_out=tool_executions_out,
                    retrieved_contexts=final_values.get("retrieved_contexts"),
                    latency_ms=int((time.monotonic() - stream_started_at) * 1000),
                    ttft_ms=_ttft_ms(emitter, stream_started_at),
                    stopped=False,
                    client_message_id=assistant_cmid,
                    reasoning_summary=reasoning_summary or None,
                    plan=final_values.get("plan") or None,
                    plan_reasoning=final_values.get("plan_reasoning") or None,
                    progress_steps=emitter.progress_steps or None,
                    token_usage=(
                        {
                            "input_tokens": turn_input_tokens,
                            "output_tokens": turn_output_tokens,
                        }
                        if (turn_input_tokens or turn_output_tokens)
                        else None
                    ),
                )
                # A terminal ``done`` is a durability claim: complete the
                # server-canonical row first in every rollout mode. This runs
                # after all answer tokens, so it does not delay TTFT or token
                # cadence; only the terminal acknowledgement waits for commit.
                persisted_assistant_id = (
                    await _jobs_mod._persist_assistant_message_safe(
                        **persist_kwargs, required=True
                    )
                )
                if persisted_assistant_id is None:
                    raise RuntimeError("Assistant message persistence returned no id")
                assistant_persisted = True
        except Exception as e:
            logger.warning("Failed to persist SSE thread messages", exc_info=e)
            raise

        if turn_input_tokens > 0 or turn_output_tokens > 0:
            # Server-side token cost metric (was previously SSE-only, so cost
            # never reached Prometheus). Model label drives per-model spend.
            try:
                record_token_usage(
                    getattr(request_body, "model", None) or "unknown",
                    turn_input_tokens,
                    turn_output_tokens,
                )
            except Exception:  # never let metrics break the stream
                logger.debug("record_token_usage failed", exc_info=True)
            frame = await emitter.emit(
                AgentStreamEvent.USAGE,
                {
                    "input_tokens": turn_input_tokens,
                    "output_tokens": turn_output_tokens,
                },
            )
            if not client_disconnected:
                yield frame

        # Carry the graph-final tool executions so the client's committed turn
        # renders them without a reload. Without this the in-memory turn's
        # tool strip depended entirely on every live tool_start/tool_end frame
        # surviving the wire — a dropped frame left the committed bubble
        # tool-less until a refresh re-read the persisted row. Args redacted
        # like every browser-visible copy. Mirrors the /stream/confirm payload.
        done_payload: Dict[str, Any] = {
            "status": "complete",
            "progress_steps": emitter.progress_steps,
            "tool_executions": (
                [
                    {**te.model_dump(), "args": redact_tool_args(te.args)}
                    for te in tool_executions_out
                ]
                if tool_executions_out
                else []
            ),
        }
        if reasoning_summary:
            done_payload["reasoning_summary"] = reasoning_summary
        if _canonical_persistence_enabled():
            # Ids let the client reconcile its optimistic bubbles with the
            # server-persisted rows instead of double-saving (server-canonical
            # mode: the frontend no longer writes messages itself).
            done_payload.update(
                {
                    "thread_id": resolved_thread_id,
                    "assistant_message_id": persisted_assistant_id,
                    "client_message_id": assistant_cmid,
                }
            )
        # Commit completion before exposing the terminal frame. A disconnect
        # immediately after ``done`` must not race into run.cancelled.
        finalized = await _finalize_run(
            db,
            acceptance,
            current_user,
            status=JobStatus.COMPLETED,
            event_type=RunEventType.RUN_COMPLETED,
            payload=(
                {"assistant_message_id": persisted_assistant_id}
                if persisted_assistant_id
                else {}
            ),
        )
        if finalized is False:
            # A user Stop can claim ``stopping`` after the pre-persist poll but
            # before this completion transition. The guarded finalizer then
            # refuses the late completion; reuse the cancellation path to mark
            # the already-written assistant row stopped and append exactly one
            # cancellation event.
            if await durable_stop_requested():
                await cancel_current_stream(reason="user_requested")
            else:
                # A different producer already closed the run. Keep its
                # terminal status authoritative instead of turning an ordinary
                # completed race into a false cancellation acknowledgement.
                await emitter.finish()
            return
        frame = await emitter.emit(AgentStreamEvent.DONE, done_payload)
        terminal_frame_sent = True
        if not client_disconnected:
            yield frame
        await emitter.finish()

    except (asyncio.CancelledError, GeneratorExit) as cancellation_exc:
        # Starlette cancels StreamingResponse's body iterator directly when
        # the client aborts the fetch. Shield the cleanup so that cancellation
        # cannot leave the graph running or the durable run non-terminal.
        async def cleanup_cancelled_response() -> None:
            async def close_graph_events() -> None:
                if event_stream_iter is not None:
                    with contextlib.suppress(asyncio.CancelledError, GeneratorExit):
                        await _close_async_iterator(event_stream_iter)

            async def persist_stopped() -> None:
                if persist_partial_stop is not None:
                    await persist_partial_stop(force_inline=True)

            async def finalize_cancelled() -> None:
                cancelled_payload: Dict[str, Any] = {
                    "reason": "client_disconnected",
                    "request_id": emitter.trace_id,
                }
                # Link the run to the stopped partial row persisted just above,
                # so a cancelled run can still name the message holding its
                # output. Omitted when nothing streamed before the abort.
                if persisted_assistant_id:
                    cancelled_payload["assistant_message_id"] = persisted_assistant_id
                await _finalize_run(
                    db,
                    acceptance,
                    current_user,
                    status=JobStatus.CANCELLED,
                    event_type=RunEventType.RUN_CANCELLED,
                    payload=cancelled_payload,
                )

            await _run_cancel_cleanup(
                ("close_graph_events", close_graph_events),
                ("persist_partial_stop", persist_stopped),
                ("finish_emitter", emitter.finish),
                ("finalize_cancelled", finalize_cancelled),
            )

        await _run_interrupted_cleanup(cleanup_cancelled_response)
        raise cancellation_exc

    except GraphInterrupt as exc:
        # Graph hit an interrupt mid-stream (HITL confirmation needed).
        # Verify the checkpoint was persisted before telling the CLI to confirm.
        confirmation_details = extract_interrupt_confirmation(exc)
        thread_id = (config.get("configurable") or {}).get(
            "thread_id"
        ) or stream_thread_id

        checkpoint_ok = False
        try:
            if graph is not None:
                verify_snapshot = await graph.aget_state(config)
                checkpoint_ok = bool(
                    verify_snapshot
                    and verify_snapshot.values
                    and any(
                        getattr(t, "interrupts", None)
                        for t in (verify_snapshot.tasks or ())
                    )
                )
        except Exception:
            logger.warning(
                "Failed to verify checkpoint after GraphInterrupt for thread %s",
                thread_id,
            )

        if not checkpoint_ok:
            logger.error(
                "GraphInterrupt raised but checkpoint not persisted for thread %s — "
                "cannot send confirmation event (client would get 'Thread not found' on resume)",
                thread_id,
            )
            frame = await emitter.emit(
                AgentStreamEvent.ERROR,
                error_frame_payload(
                    "Interrupt state could not be saved. Please retry.",
                    AgentErrorCategory.CHECKPOINT_UNAVAILABLE,
                ),
            )
            if not client_disconnected:
                yield frame
            await emitter.finish()
            try:
                failed = await _finalize_run(
                    db,
                    acceptance,
                    current_user,
                    status=JobStatus.FAILED,
                    event_type=RunEventType.RUN_FAILED,
                    payload={
                        "code": "interrupt_not_checkpointed",
                        "message": "Interrupt state could not be saved.",
                    },
                    error_code="interrupt_not_checkpointed",
                    error="Interrupt state could not be saved. Please retry.",
                )
                if (
                    failed is False
                    and acceptance is not None
                    and await durable_stop_requested()
                ):
                    await cancel_current_stream(reason="user_requested")
            except Exception:
                # Audit S2-M3: the wire outcome above is final; bookkeeping
                # failures must not cascade into a second terminal.
                logger.error("Failed to finalize drained interrupt run", exc_info=True)
            return

        # Park before publishing the confirmation. If a concurrent Stop has
        # already claimed STOPPING, the guarded update returns False and this
        # producer must publish the cancellation ACK instead of exiting with a
        # run that no longer has a worker to observe the stop marker.
        try:
            parked = await _finalize_run(
                db,
                acceptance,
                current_user,
                status=JobStatus.AWAITING_CONFIRMATION,
                run_metadata={
                    "progress_steps": emitter.progress_steps,
                    "reasoning_summary": reasoning_summary or None,
                },
            )
        except Exception:
            parked = None
            # Best-effort bookkeeping failure: retain the prior behavior and
            # publish the checkpoint-backed confirmation for retry/recovery.
            logger.error(
                "Failed to park stream run AWAITING_CONFIRMATION",
                exc_info=True,
                extra={"thread_id": thread_id},
            )
        if parked is False:
            if acceptance is not None and await durable_stop_requested():
                await cancel_current_stream(reason="user_requested")
            else:
                await emitter.finish()
            return

        confirmation_payload = {
            "thread_id": thread_id,
            "confirmation": confirmation_details,
        }
        if acceptance is not None:
            confirmation_payload["run_id"] = acceptance.run_id
        frame = await emitter.emit(
            AgentStreamEvent.CONFIRMATION,
            confirmation_payload,
        )
        terminal_frame_sent = True
        if not client_disconnected:
            yield frame
        await emitter.finish()

    except Exception as e:
        logger.error("SSE stream error", exc_info=e)
        # Persist whatever was streamed before the failure (stopped=True) so the
        # partial answer survives a reload. Covers both the disconnected drain
        # dying (e.g. the 300s timeout) and an error while the client is still
        # connected — in server-canonical mode the frontend saves nothing, so
        # without this an errored turn leaves a user row and no assistant row.
        # Idempotent: no-ops if nothing streamed or the row was already saved.
        if persist_partial_stop is not None:
            with contextlib.suppress(Exception):
                await persist_partial_stop()
        category = _stream_failure_category(e)
        wire_error: BaseException | str = (
            "A response is already in progress for this thread."
            if isinstance(e, ActiveRunConflict)
            else (
                _CONFIRM_NOT_FOUND_MESSAGE
                if isinstance(e, AgentThreadResolutionError)
                else e
            )
        )
        if terminal_frame_sent:
            # Audit S2-M3: a terminal already went out — ERROR after it
            # corrupts the client state machine, and an absorbing FAILED
            # write would race/overwrite the real terminal state.
            logger.error(
                "Exception after terminal frame; suppressing wire ERROR "
                "and FAILED finalize",
                exc_info=e,
                extra={"thread_id": stream_thread_id},
            )
        else:
            frame = await emitter.emit(
                AgentStreamEvent.ERROR, error_frame_payload(wire_error, category)
            )
            if not client_disconnected:
                yield frame
            await emitter.finish()
            # `acceptance is None` here covers the case that matters most:
            # the accept transaction itself failed, so there is nothing to
            # finalize — and no `accepted` frame was ever emitted.
            failed = await _finalize_run(
                db,
                acceptance,
                current_user,
                status=JobStatus.FAILED,
                event_type=RunEventType.RUN_FAILED,
                payload={
                    "code": "stream_failed",
                    "message": client_safe_error(e),
                },
                error_code="stream_failed",
                error=client_safe_error(e),
            )
            if (
                failed is False
                and acceptance is not None
                and await durable_stop_requested()
            ):
                await cancel_current_stream(reason="user_requested")

    finally:
        if disconnect_canceller is not None:
            disconnect_canceller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await disconnect_canceller
        await db.close()
        logger.info("SSE stream ended for thread %s", stream_thread_id)


# R2-H4: per-process confirm-claim fallback for Redis outages. Partial by
# design — it cannot see claims in OTHER workers (gunicorn multi-worker) —
# but it turns "no protection at all" into "protected within a worker",
# which closes the common single-worker dev/most-traffic case. The Redis
# claim remains authoritative when available.
_local_confirm_claims: Dict[str, float] = {}
_local_confirm_claims_lock = threading.Lock()
_LOCAL_CONFIRM_TTL_S = 330.0


def _acquire_local_confirm_claim(key: str, now: Optional[float] = None) -> bool:
    """True if this process may proceed with the confirm; False if a live
    claim for the same key exists. TTL mirrors the Redis claim's 330s."""
    current = time.monotonic() if now is None else now
    with _local_confirm_claims_lock:
        expiry = _local_confirm_claims.get(key)
        if expiry is not None and expiry > current:
            return False
        _local_confirm_claims[key] = current + _LOCAL_CONFIRM_TTL_S
        # Opportunistic sweep so the dict can't grow unbounded.
        if len(_local_confirm_claims) > 512:
            for stale_key in [
                k for k, v in _local_confirm_claims.items() if v <= current
            ]:
                del _local_confirm_claims[stale_key]
        return True


def _release_local_confirm_claim(key: str) -> None:
    """Release a locally-held confirm claim (pop; no-op if absent/expired)."""
    with _local_confirm_claims_lock:
        _local_confirm_claims.pop(key, None)


async def stream_confirm_event_generator(
    request_body: Any,  # StreamConfirmRequest
    request: Any,  # FastAPI Request
    current_user: User,
    *,
    background_tasks: Any = None,  # fastapi.BackgroundTasks (optional for tests)
):
    """SSE event generator for the /stream/confirm endpoint.

    Resumes a graph interrupted by HITL and streams the remaining events.
    """
    from langgraph.types import Command

    from src.services.agent.checkpointer import get_checkpointer, reset_checkpointer
    from src.services.agent.graph import compile_agent_graph
    from src.services.agent.memory import get_memory_store

    # Lazy import schemas
    from .execute import (
        AgentExecuteRequest,
        AgentMessage,
        PageContextRequest,
        ToolExecutionResponse,
    )

    db = AsyncSessionLocal()
    trace_run_id = _uuid.uuid4()
    emitter = _SeqEmitter(trace_id=_request_trace_id(request))
    client_disconnected = False
    # Set once an assistant row for this turn has been persisted/scheduled —
    # the error-path partial persist must never double-write the turn.
    assistant_persisted = False
    persist_partial_stop = None  # bound inside try once its inputs exist
    # Set before a terminal CONFIRMATION/DONE is yielded (audit S2-M3).
    terminal_frame_sent = False
    # CX1 claim state — pre-declared so the except handler can reference
    # them even when an exception fires before the claim block runs.
    confirm_claim_key: Optional[str] = None
    redis_client = None
    claim_is_local = False  # R2-H4: True when the in-process claim is held
    claim_is_redis = False  # True when the Redis claim is ALSO held
    durable_claimed = False
    events_started = False
    confirm_event_iter = None
    active_run = None
    # Declared out here, not in the try: the CancelledError cleanup below reads
    # it to link the cancelled run to its stopped partial row, and that handler
    # can fire before the try body has run.
    persisted_assistant_id: Optional[str] = None
    disconnect_canceller = _cancel_current_task_on_disconnect(request)
    try:
        _bootstrap_langsmith()
        checkpointer = await get_checkpointer()
        store = await get_memory_store()
        graph = compile_agent_graph(checkpointer=checkpointer, store=store)

        # Ids only in configurable (audit B8) — checkpoint lookup needs the
        # thread_id; the ids keep parity with the run config shape.
        snapshot_config = {
            "configurable": {
                "thread_id": request_body.thread_id,
                "user_id": str(current_user.id),
                "organization_id": str(
                    getattr(current_user, "organization_id", "") or ""
                ),
            }
        }
        current_snapshot = await graph.aget_state(snapshot_config)

        # Retry once with a fresh connection if checkpoint not found — the
        # pooler may have dropped the idle connection during HITL wait time.
        if not current_snapshot or not current_snapshot.values:
            logger.warning(
                "Checkpoint not found for thread %s on first attempt, retrying with fresh connection",
                request_body.thread_id,
            )
            await reset_checkpointer()
            checkpointer = await get_checkpointer()
            store = await get_memory_store()
            graph = compile_agent_graph(checkpointer=checkpointer, store=store)
            snapshot_config = {
                "configurable": {
                    "thread_id": request_body.thread_id,
                    "user_id": str(current_user.id),
                    "organization_id": str(
                        getattr(current_user, "organization_id", "") or ""
                    ),
                }
            }
            current_snapshot = await graph.aget_state(snapshot_config)

        # Verify thread exists
        if not current_snapshot or not current_snapshot.values:
            yield await emitter.emit(
                AgentStreamEvent.ERROR,
                error_frame_payload(
                    _CONFIRM_NOT_FOUND_MESSAGE, _CONFIRM_NOT_FOUND_CATEGORY
                ),
            )
            return

        # Verify thread ownership — checkpoints without an owner predate the
        # ownership field and cannot be safely resumed from a public thread id.
        snapshot_user_id = current_snapshot.values.get("user_id")
        if not snapshot_user_id or snapshot_user_id != str(current_user.id):
            logger.warning(
                "HITL ownership mismatch: thread %s owned by %s, requested by %s",
                request_body.thread_id,
                snapshot_user_id,
                current_user.id,
            )
            yield await emitter.emit(
                AgentStreamEvent.ERROR,
                error_frame_payload(
                    _CONFIRM_NOT_FOUND_MESSAGE, _CONFIRM_NOT_FOUND_CATEGORY
                ),
            )
            return

        # Only a checkpoint authored by the threadless initial-state path may
        # continue without a durable Thread. Pair the explicit provenance value
        # with that path's empty state thread id so a durable checkpoint with a
        # missing/unknown/accidentally copied marker never bypasses current
        # workspace access.
        checkpoint_is_ephemeral = (
            current_snapshot.values.get("thread_persistence")
            == THREAD_PERSISTENCE_EPHEMERAL
            and current_snapshot.values.get("thread_id") == ""
        )

        # Resume idempotency/claim anchor. Ephemeral runs have no durable
        # AgentRun CAS, so both a live interrupt and a checkpoint-scoped claim
        # are mandatory before issuing Command(resume=...).
        try:
            resume_ckpt_id = (current_snapshot.config or {})["configurable"][
                "checkpoint_id"
            ]
        except Exception:
            resume_ckpt_id = None
        if checkpoint_is_ephemeral:
            has_pending_interrupt = any(
                getattr(task, "interrupts", None)
                for task in getattr(current_snapshot, "tasks", ())
            )
            if not has_pending_interrupt or not resume_ckpt_id:
                yield await emitter.emit(
                    AgentStreamEvent.ERROR,
                    error_frame_payload(
                        "Run is not awaiting confirmation",
                        AgentErrorCategory.CONFLICT,
                    ),
                )
                return
        else:
            # A checkpoint owner is historical provenance, not current durable
            # authorization. Missing/unknown/durable markers must still resolve
            # the live thread so deletion, ancestor deletion, membership
            # removal, and viewer downgrade all revoke confirmation.
            try:
                await _resolve_thread(
                    db,
                    current_user,
                    AgentExecuteRequest.model_construct(
                        messages=[AgentMessage(role="user", content="confirmation")],
                        thread_id=request_body.thread_id,
                        page_context=PageContextRequest(),
                    ),
                    create_if_missing=False,
                )
            except (AgentThreadResolutionError, ValueError):
                yield await emitter.emit(
                    AgentStreamEvent.ERROR,
                    error_frame_payload(
                        _CONFIRM_NOT_FOUND_MESSAGE, _CONFIRM_NOT_FOUND_CATEGORY
                    ),
                )
                await emitter.finish()
                return

            # The tenant-scoped durable run is the shared Stop/Confirm
            # authority for persisted threads.
            active_run = await get_active_run_for_thread(
                db,
                request_body.thread_id,
                organization_id=getattr(current_user, "organization_id", None),
                user_id=current_user.id,
            )
            if asyncio.iscoroutine(active_run):  # fail closed on a malformed DB adapter
                active_run.close()
                active_run = None
            if active_run is None:
                yield await emitter.emit(
                    AgentStreamEvent.ERROR,
                    error_frame_payload(
                        "Run is not awaiting confirmation",
                        AgentErrorCategory.CONFLICT,
                    ),
                )
                return

        run_metadata = getattr(active_run, "run_metadata", None)
        emitter.seed_progress(
            run_metadata.get("progress_steps")
            if isinstance(run_metadata, dict)
            else None
        )
        reasoning_summary = (
            str(run_metadata.get("reasoning_summary") or "")[
                :_MAX_REASONING_SUMMARY_CHARS
            ]
            if isinstance(run_metadata, dict)
            else ""
        )

        page_context = _page_context_to_dict(
            current_snapshot.values.get("page_context", {})
        )
        from src.services.agent.runtime_snapshot import resume_runtime_config_fields

        runtime_context = resume_runtime_config_fields(current_snapshot.values)

        # Resume idempotency key is anchored to the interrupt CHECKPOINT — not
        # the thread's latest user client_message_id. A user can send a new
        # turn in the same thread while the resume streams; the latest-cmid
        # derivation then re-pointed the key at the NEW turn and the dedup
        # index silently dropped that turn's real answer (round-3 M6).
        # Concurrent double-confirms read the same pre-resume snapshot →
        # same key → still dedupe.

        # CX1: atomically claim this interrupt before resuming. The job
        # confirm endpoint has a CAS (execute.py compare_and_set_status);
        # this SSE path had none — two concurrent confirms both issued
        # Command(resume=...) and a destructive tool could run twice.
        # Claim key is anchored to the interrupt checkpoint (same anchor as
        # the #1065 resume-dedup cmid): a nested confirm re-parks on a NEW
        # checkpoint, so its key rotates and the next confirm still works.
        if resume_ckpt_id:
            from src.core.caching import _acquire_lock
            from src.services.agent.job_store import get_redis

            redis_client = await get_redis()
            confirm_claim_key = (
                f"hitl-confirm-claim:{request_body.thread_id}:{resume_ckpt_id}"
            )
            if (
                redis_client is None
                and not process_local_confirmation_coordination_allowed()
            ):
                yield await emitter.emit(
                    AgentStreamEvent.ERROR,
                    error_frame_payload(
                        "Confirmation is temporarily unavailable; please retry",
                        AgentErrorCategory.INTERNAL,
                    ),
                )
                return
            # Take the local claim first for same-process exclusion. Redis then
            # adds the required cross-worker claim in shared deployments; only
            # disposable single-process environments may proceed without it.
            claim_is_local = True
            if not _acquire_local_confirm_claim(confirm_claim_key):
                yield await emitter.emit(
                    AgentStreamEvent.ERROR,
                    error_frame_payload(
                        "Confirmation already in progress",
                        AgentErrorCategory.CONFLICT,
                    ),
                )
                return
            if redis_client is not None:
                # TTL > the 300s stream timeout so a live winner can't lose
                # its claim mid-run; a crashed winner unblocks after TTL.
                try:
                    redis_claimed = await _acquire_lock(
                        redis_client, confirm_claim_key, ttl=330
                    )
                except Exception:
                    # A local claim is sufficient only for disposable,
                    # single-process environments. Shared deployments must
                    # fail closed or another pod can resume the same tool.
                    logger.warning(
                        "Confirm claim: Redis lock errored",
                        exc_info=True,
                    )
                    if not process_local_confirmation_coordination_allowed():
                        _release_local_confirm_claim(confirm_claim_key)
                        claim_is_local = False
                        yield await emitter.emit(
                            AgentStreamEvent.ERROR,
                            error_frame_payload(
                                "Confirmation is temporarily unavailable; please retry",
                                AgentErrorCategory.INTERNAL,
                            ),
                        )
                        return
                    redis_claimed = None  # not held — don't release later
                if redis_claimed is False:
                    # Another worker holds the claim. Release our local one
                    # (taken above) so a later legit confirm in THIS worker
                    # isn't blocked for the full local TTL.
                    _release_local_confirm_claim(confirm_claim_key)
                    yield await emitter.emit(
                        AgentStreamEvent.ERROR,
                        error_frame_payload(
                            "Confirmation already in progress",
                            AgentErrorCategory.CONFLICT,
                        ),
                    )
                    return
                claim_is_redis = redis_claimed is True
        else:
            if not process_local_confirmation_coordination_allowed():
                yield await emitter.emit(
                    AgentStreamEvent.ERROR,
                    error_frame_payload(
                        "Confirmation is temporarily unavailable; please retry",
                        AgentErrorCategory.INTERNAL,
                    ),
                )
                return
            logger.warning(
                "No checkpoint id for resumed thread %s; confirm proceeds "
                "unclaimed (matches the cmid fallback philosophy)",
                request_body.thread_id,
            )

        if not checkpoint_is_ephemeral:
            # The durable branch above returned on a missing run. Keep this
            # invariant explicit for both runtime readers and static analysis.
            assert active_run is not None
            durable_claimed = await claim_awaiting_run_for_confirmation(
                db,
                str(active_run.job_id),
                organization_id=getattr(current_user, "organization_id", None),
                user_id=current_user.id,
            )
            if not durable_claimed:
                if claim_is_local and confirm_claim_key:
                    with contextlib.suppress(Exception):
                        _release_local_confirm_claim(confirm_claim_key)
                if claim_is_redis and confirm_claim_key:
                    from src.core.caching import _release_lock

                    with contextlib.suppress(Exception):
                        await _release_lock(redis_client, confirm_claim_key)
                yield await emitter.emit(
                    AgentStreamEvent.ERROR,
                    error_frame_payload(
                        "Run is not awaiting confirmation",
                        AgentErrorCategory.CONFLICT,
                    ),
                )
                return

        async def _resume_assistant_cmid() -> Optional[str]:
            if resume_ckpt_id:
                return str(
                    _uuid.uuid5(
                        _uuid.NAMESPACE_URL,
                        f"nous-assistant-resume:{request_body.thread_id}:{resume_ckpt_id}",
                    )
                )
            # Fallback (checkpoint id missing): the original latest-user-cmid
            # derivation — imperfect but better than a non-idempotent row.
            try:
                user_cmid = await _latest_user_client_message_id(
                    db, request_body.thread_id
                )
                if user_cmid is not None:
                    return str(
                        _uuid.uuid5(_uuid.NAMESPACE_URL, f"nous-assistant:{user_cmid}")
                    )
            except Exception:
                logger.warning(
                    "Failed to derive assistant client_message_id for resumed "
                    "thread %s; assistant row will not be idempotent",
                    request_body.thread_id,
                    exc_info=True,
                )
            return None

        config = {
            "recursion_limit": RECURSION_LIMIT,
            "run_name": "agent:stream:resume",
            "run_id": trace_run_id,
            # Ids only (audit B8) — see stream_event_generator's run config.
            "configurable": {
                "thread_id": request_body.thread_id,
                "user_id": str(current_user.id),
                "organization_id": str(
                    getattr(current_user, "organization_id", "") or ""
                ),
                "page_context": page_context,
                **runtime_context,
            },
            "metadata": build_trace_metadata(
                trace_source=TraceSource.GRAPH,
                user_id=current_user.id,
                org_id=getattr(current_user, "organization_id", None),
                thread_id=request_body.thread_id,
                request_id=emitter.trace_id,
                agent_run_id=(active_run.job_id if active_run is not None else None),
                user_message_id=(
                    active_run.user_message_id if active_run is not None else None
                ),
                client_message_id=(
                    active_run.client_message_id if active_run is not None else None
                ),
            ),
        }

        resume_input = Command(resume={"confirmed": request_body.confirmed})

        emitter.set_context(route="graph")
        # Bind the resumed stream to the durable run (audit S2-H1) so a
        # reconnecting client can address this buffer via stream_id_for_run.
        active_run_id = getattr(active_run, "job_id", None)
        await emitter.start(
            request_body.thread_id,
            run_id=str(active_run_id) if active_run_id is not None else None,
            continue_sequence=True,
        )

        yield await emitter.emit(
            AgentStreamEvent.TRACE,
            build_trace_payload(
                thread_id=request_body.thread_id,
                cli_session_id="",
                langsmith_run_id=str(config["run_id"]),
            ),
        )

        # Per-turn token accounting for the confirm/resume stream.
        turn_input_tokens = 0
        turn_output_tokens = 0
        tokens_emitted = False
        context_fingerprint: Optional[str] = None

        # A confirmation is the continuation of the interrupted turn. Publish
        # its complete carried source snapshot before resumed work begins;
        # later node snapshots replace it only when their cumulative content
        # changes, avoiding the old carried-plus-resume concatenation bug.
        carried_contexts = current_snapshot.values.get("retrieved_contexts")
        latest_retrieved_contexts: List[Dict[str, Any]] = (
            carried_contexts if isinstance(carried_contexts, list) else []
        )
        if isinstance(carried_contexts, list):
            try:
                context_fingerprint = _json.dumps(
                    carried_contexts, sort_keys=True, default=str
                )
            except (TypeError, ValueError):
                context_fingerprint = None
            frame = await emitter.emit(
                AgentStreamEvent.RAG_CONTEXT,
                {"contexts": carried_contexts},
            )
            if not client_disconnected:
                yield frame

        # Accumulate user-facing tokens so a mid-resume disconnect can persist
        # the partial answer server-side (a resumed turn may have already
        # committed a destructive tool — losing the assistant row entirely
        # leaves the thread with a confirm action and no record of the result).
        streamed_parts: list[str] = []

        async def persist_partial_stop(*, force_inline: bool = False) -> None:
            """Persist the accumulated partial answer with stopped=True.

            Shared by the legacy (no stream buffer) disconnect branch and the
            error path when a disconnected drain dies mid-run (e.g. the 300s
            timeout) — without it that partial would be silently lost.
            """
            nonlocal assistant_persisted, persisted_assistant_id
            partial = "".join(streamed_parts)
            if checkpoint_is_ephemeral:
                return
            if assistant_persisted:
                if persisted_assistant_id:
                    with contextlib.suppress(BaseException):
                        await _jobs_mod._mark_assistant_message_stopped_safe(
                            thread_id=request_body.thread_id,
                            message_id=persisted_assistant_id,
                        )
                return
            if not partial and not reasoning_summary:
                return
            assistant_persisted = True
            # Checkpoint-anchored idempotency key (see _resume_assistant_cmid),
            # which itself falls back to the latest-user-cmid derivation when
            # the checkpoint id is missing — so a retried/duplicated confirm
            # dedupes on the assistant partial unique index instead of leaving
            # a duplicate row.
            disconnect_cmid: Optional[str] = await _resume_assistant_cmid()
            stop_kwargs = dict(
                thread_id=request_body.thread_id,
                content=partial,
                model_name=getattr(request_body, "model", "") or None,
                # ponytail: tool_executions/plan omitted — reading them needs
                # a checkpoint fetch on a path that must stay cheap (client
                # already hung up), same tradeoff as the main stream.
                tool_executions_out=None,
                retrieved_contexts=latest_retrieved_contexts,
                latency_ms=None,
                stopped=True,
                client_message_id=disconnect_cmid,
                reasoning_summary=reasoning_summary or None,
                progress_steps=emitter.progress_steps or None,
                token_usage=(
                    {
                        "input_tokens": turn_input_tokens,
                        "output_tokens": turn_output_tokens,
                    }
                    if (turn_input_tokens or turn_output_tokens)
                    else None
                ),
            )
            if background_tasks is not None and not force_inline:
                # Deferred to a background task: no id to capture here. Callers
                # that need the run linked to its partial row (cancellation)
                # pass force_inline=True and take the awaited branch below.
                background_tasks.add_task(
                    _jobs_mod._persist_assistant_message_safe, **stop_kwargs
                )
            else:
                persisted_assistant_id = (
                    await _jobs_mod._persist_assistant_message_safe(**stop_kwargs)
                )

        async def cancel_confirm_stream(
            reason: str = "client_disconnected",
        ) -> None:
            async def close_confirm_events() -> None:
                if confirm_event_iter is not None:
                    with contextlib.suppress(asyncio.CancelledError, GeneratorExit):
                        await _close_async_iterator(confirm_event_iter)

            async def finalize_cancelled() -> None:
                cancelled_payload: Dict[str, Any] = {
                    "reason": reason,
                    "request_id": emitter.trace_id,
                }
                # Link the run to the stopped partial row persisted just above,
                # so a cancelled run can still name the message holding its
                # output. Omitted when nothing streamed before the abort.
                if persisted_assistant_id:
                    cancelled_payload["assistant_message_id"] = persisted_assistant_id
                await _finalize_run_id(
                    db,
                    str(active_run.job_id) if active_run is not None else None,
                    current_user,
                    status=JobStatus.CANCELLED,
                    event_type=RunEventType.RUN_CANCELLED,
                    payload=cancelled_payload,
                )

            await _run_cancel_cleanup(
                ("close_confirm_events", close_confirm_events),
                (
                    "persist_partial_stop",
                    lambda: persist_partial_stop(force_inline=True),
                ),
                ("finish_emitter", emitter.finish),
                ("finalize_cancelled", finalize_cancelled),
            )

        async def durable_stop_requested() -> bool:
            if active_run is None:
                return False
            try:
                return await is_run_cancellation_requested(
                    db,
                    str(active_run.job_id),
                    organization_id=getattr(current_user, "organization_id", None),
                    user_id=current_user.id,
                )
            except Exception:
                logger.warning(
                    "Failed to poll durable stop marker for confirmation run %s",
                    getattr(active_run, "job_id", None),
                    exc_info=True,
                )
                return False

        # Named iterator so a mid-stream client disconnect can aclose() it and
        # cancel the resumed graph run, instead of leaving it executing into a
        # dead socket (a resumed turn may run destructive tools).
        confirm_event_iter = graph.astream_events(
            resume_input, config=config, version="v2"
        ).__aiter__()
        # Route through the keepalive helper (mirrors the main /stream loop) so
        # a long silent resume phase emits `heartbeat` frames instead of going
        # quiet until a proxy idle-timeout cuts the connection with no
        # done/error. The helper also owns the disconnect check + sentinel.
        stream_started_at = time.monotonic()
        async with asyncio.timeout(300):
            async for item in _graph_events_with_keepalive(
                confirm_event_iter,
                request,
                stop_requested=durable_stop_requested,
            ):
                # CX1: the resumed graph is now making real progress — a
                # failure from here on must NOT release the claim (the
                # winner may already have run a destructive tool; TTL
                # handles cleanup instead of letting a racing retry in).
                events_started = True
                if item["type"] == "disconnect":
                    client_disconnected = True
                    break
                if item["type"] == "stop":
                    await cancel_confirm_stream(reason="user_requested")
                    return
                if item["type"] == "keepalive":
                    elapsed_ms = int((time.monotonic() - stream_started_at) * 1000)
                    frame = await emitter.emit(
                        AgentStreamEvent.HEARTBEAT,
                        {"elapsed_ms": elapsed_ms},
                        buffer=False,
                    )
                    if not client_disconnected:
                        yield frame
                    continue
                event = item["event"]

                kind = event.get("event", "")
                name = event.get("name", "")

                progress_status = _progress_status_for_event(kind, name)
                if progress_status is not None:
                    frame = await emitter.emit(AgentStreamEvent.STATUS, progress_status)
                    if not client_disconnected:
                        yield frame

                context_snapshot, context_fingerprint = (
                    _changed_retrieved_context_snapshot(event, context_fingerprint)
                )
                if context_snapshot is not None:
                    latest_retrieved_contexts = context_snapshot
                    frame = await emitter.emit(
                        AgentStreamEvent.RAG_CONTEXT,
                        {"contexts": context_snapshot},
                    )
                    if not client_disconnected:
                        yield frame

                if kind == "on_chat_model_stream":
                    if not _is_user_facing_token_event(event):
                        continue
                    chunk = event.get("data", {}).get("chunk")
                    reasoning_delta = _chunk_reasoning_summary(chunk) if chunk else ""
                    if reasoning_delta:
                        reasoning_summary = _append_reasoning_summary(
                            reasoning_summary, reasoning_delta
                        )
                        frame = await emitter.emit(
                            AgentStreamEvent.REASONING_DELTA,
                            {"content": reasoning_delta},
                        )
                        if not client_disconnected:
                            yield frame
                    # See the note on the main stream: Responses-API chunks
                    # carry typed blocks, not a bare string.
                    chunk_text = _chunk_text(chunk) if chunk else ""
                    if chunk_text:
                        if not tokens_emitted:
                            frame = await emitter.emit(
                                AgentStreamEvent.STATUS,
                                {
                                    "phase": "writing",
                                    "detail": "Drafting the response",
                                },
                            )
                            if not client_disconnected:
                                yield frame
                        # Buffer BEFORE recording for persistence — same
                        # ordering invariant as the main stream: the stopped
                        # partial must stay a prefix of the buffered stream, so
                        # a cancellation inside this await can only lose the
                        # last chunk, never invent one.
                        frame = await emitter.emit(
                            AgentStreamEvent.TOKEN, {"content": chunk_text}
                        )
                        streamed_parts.append(chunk_text)
                        if not client_disconnected:
                            yield frame
                        tokens_emitted = True

                elif kind == "on_chat_model_end":
                    inp, out = _extract_usage_tokens(event)
                    turn_input_tokens += inp
                    turn_output_tokens += out

                elif kind == "on_tool_start":
                    tool_input = event.get("data", {}).get("input", {})
                    args_preview = _tool_args_preview(tool_input)
                    frame = await emitter.emit(
                        AgentStreamEvent.TOOL_START,
                        {
                            "tool": name,
                            "args": args_preview,
                            **_tool_event_identity(event),
                        },
                    )
                    if not client_disconnected:
                        yield frame

                elif kind == "on_tool_end":
                    output = event.get("data", {}).get("output", "")
                    is_error = (
                        isinstance(output, dict) and bool(output.get("isError"))
                    ) or (getattr(output, "status", None) == "error")
                    frame = await emitter.emit(
                        AgentStreamEvent.TOOL_END,
                        {
                            "tool": name,
                            "result": _encode_tool_result(output),
                            "is_error": is_error,
                            **_tool_event_identity(event),
                        },
                    )
                    if not client_disconnected:
                        yield frame

                elif kind == "on_chain_end" and name in _PLANNER_CHAIN_NODES:
                    output = event.get("data", {}).get("output", {})
                    if isinstance(output, dict):
                        plan_steps = output.get("plan", [])
                        plan_reasoning = output.get("plan_reasoning") or ""
                        if plan_steps or plan_reasoning:
                            frame = await emitter.emit(
                                AgentStreamEvent.PLAN,
                                {
                                    "steps": plan_steps,
                                    "reasoning": plan_reasoning,
                                },
                            )
                            if not client_disconnected:
                                yield frame

                elif kind == "on_chain_end" and name == "reflection_gate":
                    output = event.get("data", {}).get("output", {})
                    if isinstance(output, dict):
                        reflection_result = output.get("_reflection_result")
                        if reflection_result is not None:
                            passed = getattr(reflection_result, "passed", True)
                            issues = getattr(reflection_result, "issues", [])
                            round_num = output.get("reflection_count", 0)
                            severity = getattr(reflection_result, "severity", "none")
                            revising = (
                                (not passed) and severity == "major" and round_num < 2
                            )
                            frame = await emitter.emit(
                                AgentStreamEvent.REFLECTION,
                                {
                                    "passed": passed,
                                    "issues": issues,
                                    "round": round_num,
                                    "revising": revising,
                                },
                            )
                            if not client_disconnected:
                                yield frame

        if await durable_stop_requested():
            await cancel_confirm_stream(reason="user_requested")
            return

        # Client hung up mid-resume — cancel the run by closing the graph
        # iterator instead of letting it finish into a dead socket, then persist
        # the partial answer server-side with stopped=True (mirrors
        # stream_event_generator's disconnect branch). A resumed turn may have
        # already committed a destructive tool; without this the thread is left
        # with the confirm action and no assistant row recording the result.
        if client_disconnected:
            logger.info(
                "SSE confirm client disconnected; cancelled resumed run for thread %s",
                request_body.thread_id,
            )
            await cancel_confirm_stream()
            return

        # Check for nested interrupts (e.g. ingest confirmed -> add needs confirm)
        final_snapshot = await graph.aget_state(config)
        pending_tasks = final_snapshot.tasks if final_snapshot else ()
        has_interrupt = any(getattr(t, "interrupts", None) for t in pending_tasks)

        if has_interrupt:
            if await durable_stop_requested():
                await cancel_confirm_stream(reason="user_requested")
                return
            confirmation_details = {}
            for task in pending_tasks:
                for intr in getattr(task, "interrupts", []):
                    confirmation_details = getattr(intr, "value", {})
                    break
                if confirmation_details:
                    break
            # Park before publishing a nested confirmation. A Stop can win
            # between the poll above and the guarded AWAITING transition; when
            # that happens this resumed producer must own the cancellation ACK
            # rather than leave STOPPING with no worker behind it.
            try:
                parked = await _finalize_run_id(
                    db,
                    str(active_run.job_id) if active_run is not None else None,
                    current_user,
                    status=JobStatus.AWAITING_CONFIRMATION,
                    run_metadata={
                        "progress_steps": emitter.progress_steps,
                        "reasoning_summary": reasoning_summary or None,
                    },
                )
            except Exception:
                parked = None
                # Preserve the checkpoint-backed retry behavior for an
                # unknown/transient bookkeeping failure. A guarded False is
                # handled above as the competing Stop/terminal transition.
                logger.error(
                    "Failed to park resumed run AWAITING_CONFIRMATION",
                    exc_info=True,
                    extra={"thread_id": request_body.thread_id},
                )
            if parked is False:
                if await durable_stop_requested():
                    await cancel_confirm_stream(reason="user_requested")
                else:
                    await emitter.finish()
                return
            confirmation_payload = {
                "thread_id": request_body.thread_id,
                "confirmation": confirmation_details,
            }
            if active_run is not None:
                # Confirm streams do not emit a fresh accepted status. Carry
                # the parked AgentRun id through nested confirmation frames
                # so Stop remains fenced to this producer.
                confirmation_payload["run_id"] = str(active_run.job_id)
            frame = await emitter.emit(
                AgentStreamEvent.CONFIRMATION,
                confirmation_payload,
            )
            terminal_frame_sent = True
            if not client_disconnected:
                yield frame
            # The run is parked awaiting confirmation — no longer producing,
            # so clear the active pointer; the buffered frames stay until TTL.
            await emitter.finish()
            return

        final_values = final_snapshot.values if final_snapshot else {}
        tool_executions_out = [
            ToolExecutionResponse(**te)
            for te in final_values.get("tool_executions", [])
        ] or None

        # Turn-scoped: the resumed turn's messages sit after the newest human
        # turn, so this finds the post-confirm answer — and returns "" (never
        # a stale prior answer) if the resume produced no AI text.
        assistant_content = _latest_turn_assistant_text(
            final_values.get("messages", [])
        )

        # Persist ONLY the assistant row for the resumed turn. The user row
        # that started this turn was already written up-front by the original
        # /stream request (stream_event_generator → _persist_user_message),
        # so re-persisting it here would insert a SECOND bare
        # user row every confirm (no client_message_id → no dedup), inflated
        # thread.message_count, and confused context assembly.
        #
        # The resumed turn carries no fresh idempotency key (the frontend only
        # sends {thread_id, confirmed}) — use the checkpoint-anchored key so a
        # double-confirm dedupes without colliding with a concurrent new turn
        # (see _resume_assistant_cmid).
        assistant_cmid = (
            None if checkpoint_is_ephemeral else await _resume_assistant_cmid()
        )

        token_usage_payload = (
            {
                "input_tokens": turn_input_tokens,
                "output_tokens": turn_output_tokens,
            }
            if (turn_input_tokens or turn_output_tokens)
            else None
        )
        if not tokens_emitted and assistant_content:
            if await durable_stop_requested():
                await cancel_confirm_stream(reason="user_requested")
                return
            frame = await emitter.emit(
                AgentStreamEvent.STATUS,
                {"phase": "writing", "detail": "Drafting the response"},
            )
            if not client_disconnected:
                yield frame
        frame = await emitter.emit(
            AgentStreamEvent.STATUS,
            {"phase": "finalizing", "detail": "Saving the response"},
        )
        if not client_disconnected:
            yield frame
        # persisted_assistant_id is hoisted to the function top (see there).
        # A proven threadless checkpoint has nowhere durable to write; it keeps
        # the same ephemeral contract as its initial stream. Every other path
        # must commit before ``done``.
        if not checkpoint_is_ephemeral:
            if await durable_stop_requested():
                await cancel_confirm_stream(reason="user_requested")
                return
            try:
                persist_kwargs = dict(
                    thread_id=request_body.thread_id,
                    content=assistant_content,
                    model_name=getattr(request_body, "model", "") or None,
                    tool_executions_out=tool_executions_out,
                    retrieved_contexts=final_values.get("retrieved_contexts"),
                    plan=final_values.get("plan") or None,
                    plan_reasoning=final_values.get("plan_reasoning") or None,
                    reasoning_summary=reasoning_summary or None,
                    progress_steps=emitter.progress_steps or None,
                    token_usage=token_usage_payload,
                    client_message_id=assistant_cmid,
                    latency_ms=int((time.monotonic() - stream_started_at) * 1000),
                    ttft_ms=_ttft_ms(emitter, stream_started_at),
                )
                # ``done`` is emitted only after the server-canonical row exists;
                # rollout mode controls reconciliation ids, not durability.
                persisted_assistant_id = (
                    await _jobs_mod._persist_assistant_message_safe(
                        **persist_kwargs, required=True
                    )
                )
                if persisted_assistant_id is None:
                    raise RuntimeError("Assistant message persistence returned no id")
                assistant_persisted = True
            except Exception as e:
                logger.warning(
                    "Failed to persist SSE confirmation thread messages",
                    exc_info=e,
                )
                raise

        if turn_input_tokens > 0 or turn_output_tokens > 0:
            # Server-side token cost metric (was previously SSE-only, so cost
            # never reached Prometheus). Model label drives per-model spend.
            try:
                record_token_usage(
                    getattr(request_body, "model", None) or "unknown",
                    turn_input_tokens,
                    turn_output_tokens,
                )
            except Exception:  # never let metrics break the stream
                logger.debug("record_token_usage failed", exc_info=True)
            frame = await emitter.emit(
                AgentStreamEvent.USAGE,
                {
                    "input_tokens": turn_input_tokens,
                    "output_tokens": turn_output_tokens,
                },
            )
            if not client_disconnected:
                yield frame

        if not tokens_emitted and assistant_content:
            # Resume produced a final answer without streaming (templated /
            # degraded / non-streamed node) — surface it so the client isn't
            # left with an empty response.
            tokens_emitted = True
            frame = await emitter.emit(
                AgentStreamEvent.TOKEN, {"content": assistant_content}
            )
            if not client_disconnected:
                yield frame

        if not tokens_emitted and tool_executions_out:
            names = ", ".join(
                getattr(te, "tool_name", str(te)) for te in tool_executions_out
            )
            frame = await emitter.emit(
                AgentStreamEvent.TOKEN, {"content": f"Done — completed: {names}."}
            )
            if not client_disconnected:
                yield frame

        # Canonical mode carries the persisted ids so the client can reconcile
        # its optimistic bubble with the server row (mirrors the main /stream
        # done payload). tool_executions stays for legacy CLI clients — args
        # redacted like every other browser-visible copy (the live tool_start
        # preview was redacted in #1046 but this payload kept raw args).
        done_payload: Dict[str, Any] = {
            "status": "complete",
            "progress_steps": emitter.progress_steps,
            "tool_executions": (
                [
                    {**te.model_dump(), "args": redact_tool_args(te.args)}
                    for te in tool_executions_out
                ]
                if tool_executions_out
                else []
            ),
        }
        if reasoning_summary:
            done_payload["reasoning_summary"] = reasoning_summary
        if _canonical_persistence_enabled():
            done_payload.update(
                {
                    "thread_id": request_body.thread_id,
                    "assistant_message_id": persisted_assistant_id,
                    "client_message_id": assistant_cmid,
                }
            )
        # Commit completion before exposing the terminal frame. A disconnect
        # immediately after ``done`` must not race into run.cancelled.
        if await durable_stop_requested():
            await cancel_confirm_stream(reason="user_requested")
            return
        finalized = await _finalize_run_id(
            db,
            str(active_run.job_id) if active_run is not None else None,
            current_user,
            status=JobStatus.COMPLETED,
            event_type=RunEventType.RUN_COMPLETED,
            payload=(
                {"assistant_message_id": persisted_assistant_id}
                if persisted_assistant_id
                else {}
            ),
        )
        if finalized is False:
            if await durable_stop_requested():
                await cancel_confirm_stream(reason="user_requested")
            else:
                await emitter.finish()
            return
        frame = await emitter.emit(AgentStreamEvent.DONE, done_payload)
        terminal_frame_sent = True
        if not client_disconnected:
            yield frame
        await emitter.finish()

    except (asyncio.CancelledError, GeneratorExit) as cancellation_exc:

        async def cleanup_cancelled_confirm_response() -> None:
            async def close_confirm_events() -> None:
                if confirm_event_iter is not None:
                    with contextlib.suppress(asyncio.CancelledError, GeneratorExit):
                        await _close_async_iterator(confirm_event_iter)

            async def persist_stopped() -> None:
                if persist_partial_stop is not None:
                    await persist_partial_stop(force_inline=True)

            async def finalize_cancelled() -> None:
                cancelled_payload: Dict[str, Any] = {
                    "reason": "client_disconnected",
                    "request_id": emitter.trace_id,
                }
                # Link the run to the stopped partial row persisted just above,
                # so a cancelled run can still name the message holding its
                # output. Omitted when nothing streamed before the abort.
                if persisted_assistant_id:
                    cancelled_payload["assistant_message_id"] = persisted_assistant_id
                await _finalize_run_id(
                    db,
                    str(active_run.job_id) if active_run is not None else None,
                    current_user,
                    status=JobStatus.CANCELLED,
                    event_type=RunEventType.RUN_CANCELLED,
                    payload=cancelled_payload,
                )

            await _run_cancel_cleanup(
                ("close_confirm_events", close_confirm_events),
                ("persist_partial_stop", persist_stopped),
                ("finish_emitter", emitter.finish),
                ("finalize_cancelled", finalize_cancelled),
            )

        await _run_interrupted_cleanup(cleanup_cancelled_confirm_response)
        raise cancellation_exc

    except Exception as e:
        logger.error("SSE stream confirm error", exc_info=e)
        # CX1: the winner failed before the resumed graph produced any
        # event — release the claim so a legit retry is not locked out for
        # the full TTL. Once events_started is True the resume may have run
        # a destructive tool already, so the claim is left for TTL cleanup.
        if confirm_claim_key and not events_started:
            # Both domains may be held (local always, Redis on top —
            # R2-H4 review follow-up): release each independently.
            if claim_is_local:
                with contextlib.suppress(Exception):
                    _release_local_confirm_claim(confirm_claim_key)
            if claim_is_redis:
                from src.core.caching import _release_lock

                with contextlib.suppress(Exception):
                    await _release_lock(redis_client, confirm_claim_key)
        if durable_claimed and not events_started and active_run is not None:
            with contextlib.suppress(Exception):
                await db.rollback()
                await release_confirmation_claim(
                    db,
                    str(active_run.job_id),
                    organization_id=getattr(current_user, "organization_id", None),
                    user_id=current_user.id,
                )
        # Persist whatever was streamed before the failure (stopped=True) so the
        # partial answer survives a reload. Covers both the disconnected drain
        # dying (e.g. the 300s timeout) and an error while the client is still
        # connected — in server-canonical mode the frontend saves nothing, so
        # without this an errored turn leaves a user row and no assistant row.
        # Idempotent: no-ops if nothing streamed or the row was already saved.
        if persist_partial_stop is not None:
            with contextlib.suppress(Exception):
                await persist_partial_stop()
        # R2-H1: an error at confirm time previously left the durable run
        # AWAITING_CONFIRMATION forever. Review follow-ups (codex on #1413):
        # - finalize ONLY once the resume produced events — a pre-resume
        #   failure released the claim above precisely so the confirm can be
        #   RETRIED, and a terminal FAILED run would strand that retry
        #   (terminal states are absorbing; get_active_run_for_thread skips
        #   them). Pre-resume the run correctly stays awaiting_confirmation.
        # - RunFailedPayload requires {code, message} and forbids extras —
        #   the original {reason, error, request_id} payload failed
        #   validation inside append_event, rolled back the status write,
        #   and silently defeated the whole fix.
        if events_started:
            try:
                failed = await _finalize_run_id(
                    db,
                    str(active_run.job_id) if active_run is not None else None,
                    current_user,
                    status=JobStatus.FAILED,
                    event_type=RunEventType.RUN_FAILED,
                    payload={
                        "code": "confirm_error",
                        "message": client_safe_error(e),
                    },
                )
                if failed is False and await durable_stop_requested():
                    await cancel_confirm_stream(reason="user_requested")
                    return
            except Exception:
                logger.error(
                    "Failed to finalize errored confirmation run", exc_info=True
                )
        if terminal_frame_sent:
            # Audit S2-M3: never emit ERROR after CONFIRMATION/DONE — a
            # same-chunk ERROR destroys the pending approval card.
            logger.error(
                "Confirm exception after terminal frame; suppressing "
                "post-terminal ERROR frame",
                exc_info=e,
                extra={"thread_id": request_body.thread_id},
            )
        else:
            frame = await emitter.emit(AgentStreamEvent.ERROR, error_frame_payload(e))
            if not client_disconnected:
                yield frame
            await emitter.finish()

    finally:
        if disconnect_canceller is not None:
            disconnect_canceller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await disconnect_canceller
        await db.close()
        logger.info("SSE confirm stream ended for thread %s", request_body.thread_id)
