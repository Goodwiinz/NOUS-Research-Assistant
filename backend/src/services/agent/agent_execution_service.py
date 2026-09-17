"""Agent execution service — job storage access and background graph execution.

Extracted verbatim from ``src.api.agent.jobs`` (audit B5 + C4-fold): the job
runner is an application service, not router code. Manages the async job
lifecycle for agent execution:

- Job creation/retrieval/cleanup (L1 + Redis via ``job_store``)
- Thread resolution / project binding / message persistence
- Server-side history seeding (Option B) + dual-store divergence guard
- Background graph invocation via ``_run_agent_graph``
- Graph resume after human-in-the-loop confirmation via ``_resume_agent_graph``

``src.api.agent.jobs`` remains as a thin re-export seam for legacy import
paths; new code must import from this module directly. The ``agent_runs``
Postgres projection stays in ``agent_run_service`` (its single owner).
"""

import asyncio
import contextlib
import logging
import random
import time
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, cast

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import AsyncSessionLocal
from src.models.user import User

from ._errors import client_safe_error, extract_interrupt_confirmation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job storage — in-memory L1 + Redis L2
# ---------------------------------------------------------------------------
#
# The in-memory ``OrderedDict`` lives in ``job_store._l1`` (the single
# source of truth for L1).  ``_jobs`` is an alias so the confirm endpoint's
# ``with _jobs_lock: _jobs.get(job_id)`` pattern keeps working without
# changes.  ``_jobs_lock`` is a compatibility alias for the same lock.
# All writes go through ``job_store.set_job()`` (async) or the
# ``_set_job`` sync wrapper which sprays to both L1 and Redis.

from src.services.agent import job_store as _job_store
from src.services.agent._builders import RECURSION_LIMIT
from src.services.agent._sanitize import sanitize_page_context
from src.services.agent.agent_run_service import get_run
from src.services.agent.job_store import _is_newer_or_equal
from src.services.agent.job_store import _l1 as _jobs
from src.services.agent.job_store import _l1_lock as _jobs_lock
from src.services.agent.job_store import _write_to_redis_only
from src.services.agent.job_store import delete_job as _delete_job_async
from src.services.agent.job_store import get_job as _get_job_async
from src.services.agent.job_store import (
    schedule_run_projection as _schedule_run_projection,
)
from src.services.agent.job_store import set_job as _set_job_async
from src.services.agent.state import (
    THREAD_PERSISTENCE_DURABLE,
    THREAD_PERSISTENCE_EPHEMERAL,
)
from src.services.agent.trace_metadata import TraceSource, build_trace_metadata
from src.shared.enums import JobStatus

MAX_JOBS = 500


def _sum_message_usage(messages: list) -> tuple[int, int]:
    """Sum (input, output) token usage for THIS turn's assistant messages.

    ``graph.ainvoke`` (the job path) runs on a checkpointed thread, so
    ``final_state["messages"]`` includes every prior turn's ``AIMessage`` —
    each still carrying ``usage_metadata``. Summing all of them would compound
    the monotonic token counter and over-report ``usage`` on any multi-turn
    conversation. Scope to messages after the last ``HumanMessage`` (the
    current turn's model output, including the tool-loop AIMessages). The SSE
    path counts via per-turn ``on_chat_model_end`` events and needs no slicing.

    Reads ``usage_metadata`` (provider-normalized) first, then raw
    ``response_metadata.token_usage``. Returns ``(0, 0)`` when unreported.
    """
    msgs = messages or []
    last_human = -1
    for i, msg in enumerate(msgs):
        if getattr(msg, "type", None) == "human":
            last_human = i
    turn_msgs = msgs[last_human + 1 :] if last_human >= 0 else msgs
    in_tok = out_tok = 0
    for msg in turn_msgs:
        usage = getattr(msg, "usage_metadata", None)
        if isinstance(usage, dict):
            in_tok += int(usage.get("input_tokens", 0) or 0)
            out_tok += int(usage.get("output_tokens", 0) or 0)
            continue
        meta = getattr(msg, "response_metadata", None)
        if isinstance(meta, dict):
            tu = meta.get("token_usage") or {}
            if isinstance(tu, dict):
                in_tok += int(tu.get("prompt_tokens") or tu.get("input_tokens") or 0)
                out_tok += int(
                    tu.get("completion_tokens") or tu.get("output_tokens") or 0
                )
    return in_tok, out_tok


# Strong references to in-flight fire-and-forget Redis write tasks. Without
# this the event loop keeps only a weak reference and the task can be garbage
# collected mid-write (per the CPython asyncio docs). Cleared in the
# done-callback below, which also surfaces any write failure.
_background_tasks: set = set()


def _cleanup_jobs():
    """No-op — Redis TTL handles expiry; L1 cleanup is in job_store."""
    pass


def _maybe_cleanup_jobs():
    """No-op — L1 cleanup is in job_store."""
    pass


def _set_job(job_id: str, data: dict, *, project: bool = True):
    """Persist a job — writes L1 immediately, then Redis via fire-and-forget.

    The fire-and-forget task uses ``_write_to_redis_only`` so it never
    touches the L1 cache — the L1 write was already done synchronously
    above.  This avoids a race where the background task overwrites a
    later L1 update from the main async flow (e.g. idempotency guard
    changing status from "running" to "error").
    """
    import asyncio as _asyncio

    data["created_at"] = time.time()
    with _jobs_lock:
        # Same owner carry-forward as job_store.set_job: replacement writes
        # that omit user_id must not orphan the record (the GET ownership
        # check fails closed on a missing user_id).
        existing = _jobs.get(job_id)
        if "user_id" not in data and existing is not None and existing.get("user_id"):
            data["user_id"] = existing["user_id"]
        if (
            "organization_id" not in data
            and existing is not None
            and existing.get("organization_id")
        ):
            data["organization_id"] = existing["organization_id"]
        # Monotonic guard (L11): callers of this sync path (execute.py) only
        # ever write the FIRST record for a job, so `existing` is normally
        # None and this is a no-op — but without it, this write is an
        # unconditional stomp of whatever L1 currently holds, including a
        # newer record another worker wrote to Redis and this process just
        # folded in (job_store.get_job/get_job_fresh, under this same guard).
        # `_seq` must be job_store's own counter, not a local copy: `from
        # job_store import _seq` binds the value at import time and would
        # never see later increments, so this writer and job_store.set_job
        # would each hand out colliding sequence numbers instead of sharing
        # one order.
        _job_store._seq += 1
        data["_seq"] = _job_store._seq
        if existing is None or _is_newer_or_equal(data, existing):
            _jobs[job_id] = data

    # Durable projection (fire-and-forget; Redis stays authoritative). The
    # Celery dispatch path already committed its QUEUED row synchronously and
    # disables this delayed copy so it cannot regress a worker's later atomic
    # QUEUED -> RUNNING claim.
    if project:
        _schedule_run_projection(job_id, data)

    try:
        loop = _asyncio.get_running_loop()
        task = loop.create_task(_write_to_redis_only(job_id, data))
        _background_tasks.add(task)
        task.add_done_callback(_on_redis_write_done)
    except RuntimeError:
        pass


def _on_redis_write_done(task) -> None:
    """Drop a finished Redis-write task from the tracking set and surface errors.

    Runs when the fire-and-forget write completes. Logs (but swallows) any
    exception so a background Redis failure is observable without crashing the
    worker; cancellation on loop shutdown is expected and stays quiet.
    """
    _background_tasks.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Background Redis write task failed for a job")


def _actor_fields(current_user: User) -> dict:
    """Owner + tenancy stamps for a job payload.

    ``user_id`` drives the poll ownership check (fails closed when missing);
    ``organization_id`` scopes the durable ``agent_runs`` projection so the
    Postgres fallback read stays tenant-filtered.
    """
    org = getattr(current_user, "organization_id", None)
    return {
        "user_id": str(current_user.id),
        "organization_id": str(org) if org else None,
    }


def _get_job(job_id: str) -> dict | None:
    """Retrieve a job from the shared L1 cache.

    The confirm endpoint's synchronous ``with _jobs_lock`` pattern only
    needs the L1 cache.  Async pollers should prefer ``_get_job_async``
    which falls back to Redis L2.
    """
    with _jobs_lock:
        return _jobs.get(job_id)


def _coerce_citation_document_id(document_id: Any) -> Optional[_uuid.UUID]:
    """Return a UUID for database-backed citations, else ``None``.

    Some retrieval providers expose opaque storage keys as document IDs. Those
    are useful as external references, but must not abort the assistant-message
    transaction by being parsed as UUID foreign keys.
    """
    if not document_id:
        return None
    if isinstance(document_id, _uuid.UUID):
        return document_id
    try:
        return _uuid.UUID(str(document_id))
    except (AttributeError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Schemas used by job runner (canonical home: src.services.agent.schemas)
# ---------------------------------------------------------------------------

# Kept as a lazy accessor to preserve the historical call shape of the graph
# runner (schemas were once defined in execute.py and imported lazily to
# avoid circular imports; the indirection is now just deferred loading).


def _get_schemas():
    """Lazily import schema classes (historical circular-import guard)."""
    from .schemas import (
        AgentExecuteRequest,
        AgentExecuteResponse,
        AgentMessage,
        PageContextRequest,
        RetrievedContextResponse,
        ToolExecutionResponse,
    )

    return {
        "AgentExecuteRequest": AgentExecuteRequest,
        "AgentExecuteResponse": AgentExecuteResponse,
        "AgentMessage": AgentMessage,
        "PageContextRequest": PageContextRequest,
        "RetrievedContextResponse": RetrievedContextResponse,
        "ToolExecutionResponse": ToolExecutionResponse,
    }


# ---------------------------------------------------------------------------
# Helpers shared with endpoints
# ---------------------------------------------------------------------------


def _page_context_to_dict(
    page_context: Any,
) -> Dict[str, Any]:
    """Normalize page context so every execution path forwards the same shape.

    This is also the single trust boundary for client-supplied page context
    (R7-H1): every string leaf — including the unconstrained ``metadata``
    dict — is sanitized here, once, so no downstream prompt renderer
    (llm_node, planner, classifier) is ever handed raw newlines or forged
    ``##`` headings.
    """
    if hasattr(page_context, "model_dump"):
        raw = page_context.model_dump()
    else:
        raw = page_context or {}

    return sanitize_page_context(
        {
            "type": raw.get("type", "unknown"),
            "project_id": raw.get("project_id"),
            "project_name": raw.get("project_name"),
            "label": raw.get("label"),
            "metadata": raw.get("metadata"),
        }
    )


def _get_latest_user_content(messages: List[Any]) -> Optional[str]:
    """Return the latest user or human message content from graph state."""
    for message in reversed(messages):
        msg_type = getattr(message, "type", None)
        if msg_type in {"human", "user"} and getattr(message, "content", None):
            return message.content
    return None


# --- Option B: server-side history rebuild (AGENT_SERVER_SIDE_HISTORY) -------
#
# The LangGraph checkpoint (keyed by thread_id) is the model-context source of
# truth. The request array is used only for its newest user turn. The checkpoint
# is NOT a guaranteed-complete mirror (dev MemorySaver loss on restart, legacy /
# non-agent threads, job_id-fallback ids), so when it has no history we rebuild
# it from the DB. add_messages is an id-keyed upsert, so deterministic ids make
# reseeds and resends converge instead of duplicating.


def _seed_message_id(thread_id: str, row: Any) -> str:
    """Deterministic, reorder/edit-proof id for a seeded message.

    Anchored to the client idempotency key when present, else the immutable
    chat_message PK — so re-seeding a thread yields byte-identical ids and the
    add_messages reducer upserts (never duplicates) on repeat.
    """
    cmid = getattr(row, "client_message_id", None)
    if cmid:
        return str(cmid)
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{thread_id}:{row.id}"))


_THREAD_SEED_MESSAGE_LIMIT = 40


async def build_thread_seed_messages(db: AsyncSession, thread_id: str) -> List[Any]:
    """Rebuild a thread's conversation from the DB as LangGraph messages.

    Seeds the latest 40 rows when the checkpoint has no history of its own.
    User rows become HumanMessages and assistant rows become plain-content
    AIMessages (no tool_call replay, which the prompt path does not need).
    Ordered by created_at; deterministic ids make later reseeds converge.
    """
    from uuid import UUID

    from langchain_core.messages import AIMessage, HumanMessage
    from sqlalchemy import select

    from src.models.chat_message import ChatMessage, MessageRole

    try:
        tid = UUID(thread_id)
    except (ValueError, TypeError, AttributeError):
        return []

    recent_message_ids = (
        select(ChatMessage.id)
        .where(
            ChatMessage.thread_id == tid,
            ChatMessage.superseded_by_message_id.is_(None),
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(_THREAD_SEED_MESSAGE_LIMIT)
    )
    rows = (
        (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.id.in_(recent_message_ids),
                )
                .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            )
        )
        .scalars()
        .all()
    )

    out: List[Any] = []
    for r in rows:
        mid = _seed_message_id(thread_id, r)
        if r.role == MessageRole.USER:
            out.append(HumanMessage(content=r.content, id=mid))
        elif r.role == MessageRole.ASSISTANT:
            out.append(AIMessage(content=r.content, id=mid))
        # system / tool rows are not model-context turns; skip.
    return out


def _newest_user_message(messages: List[Any]) -> Optional[Any]:
    """The current turn as a HumanMessage keyed on its client_message_id.

    Option B needs a stable per-turn idempotency key: the id must be unique
    across distinct turns (so two same-content turns don't collide and overwrite
    under the id-keyed reducer) AND identical across a retry of the SAME turn (so
    an SSE retry is a no-op). Only client_message_id satisfies both — a
    content-derived id fails the first, a row-derived id fails the second. So
    when the newest turn carries no client_message_id we return None, and the
    caller falls back to the legacy path rather than fabricate an unsafe id.
    """
    from langchain_core.messages import HumanMessage

    last = next(
        (m for m in reversed(messages) if getattr(m, "role", None) == "user"), None
    )
    if last is None:
        return None
    cmid = getattr(last, "client_message_id", None)
    if not cmid:
        return None
    return HumanMessage(content=last.content, id=str(cmid))


async def _checkpoint_human_count(graph: Any, thread_id: str) -> Optional[int]:
    """HumanMessages already in the thread's checkpoint.

    Returns 0 when the checkpoint truly holds no conversation (fresh / lost /
    legacy — the case that must be seeded from the DB); a positive count when it
    has history; and ``None`` when the read FAILED. The None case matters: a
    transient read failure must NOT be mistaken for 'empty', because seeding a
    checkpoint that actually has history would duplicate its assistant turns
    (their live ids can't match rebuilt ids). Humans are never compacted (the
    compactor only removes ToolMessages), so a real 0 reliably means empty.
    """
    from langchain_core.messages import HumanMessage

    if not thread_id:
        return 0
    try:
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        values = snapshot.values if snapshot else None
        if not values:
            return 0
        return sum(1 for m in values.get("messages", []) if isinstance(m, HumanMessage))
    except Exception:
        logger.warning(
            "Option B: checkpoint read failed for thread %s; appending newest "
            "turn only (will NOT seed, to avoid duplicating a live checkpoint)",
            thread_id,
            exc_info=True,
        )
        return None


def _seed_has_id(seed: List[Any], msg_id: str) -> bool:
    """True when a message with ``msg_id`` is already in the seed.

    The just-persisted newest turn is normally in the seed under its
    client_message_id; this id-based check (not content) decides whether we must
    append it because its persist was swallowed — with no false positive when a
    different turn happens to share content.
    """
    return any(getattr(m, "id", None) == msg_id for m in seed)


# ---------------------------------------------------------------------------
# Dual-store divergence guard (detection only — audit D3 / P2.6)
# ---------------------------------------------------------------------------
#
# The chat_messages table and the LangGraph checkpoint are two independent
# stores of the same conversation. Once BOTH are non-empty they can drift
# permanently (a swallowed user-turn persist, a lost/rebuilt checkpoint), and
# the drift is invisible: the agent answers from the checkpoint while the user
# reads chat_messages. This guard *detects and logs* that drift at turn start;
# it never repairs (re-seed repair is a follow-up) and never blocks a turn.

# Healthy delta = chat_user_rows - checkpoint_human_count. The newest turn is
# persisted (counted in chat_user_rows) BEFORE it is appended to the checkpoint,
# so 1 is normal (DB has the pending turn the checkpoint hasn't accumulated yet)
# and 0 is a benign idempotent retry (the turn is already in both). Anything
# else is drift: delta < 0 → the checkpoint has turns chat_messages is missing
# (the agent remembers what the user can't see); delta > 1 → the checkpoint
# dropped persisted turns without reseeding.
_DUALSTORE_MIN_HEALTHY_DELTA = 0
_DUALSTORE_MAX_HEALTHY_DELTA = 1

# Sampling: always run while a thread is short (drift is cheapest to catch early
# and the COUNT is tiny), then 1-in-N so an established, hot thread pays the
# extra COUNT only occasionally. Divergence is monotonic once it appears, so a
# 1-in-N sample still surfaces it within a few turns.
_DUALSTORE_DIVERGENCE_ALWAYS_BELOW = 3
_DUALSTORE_DIVERGENCE_SAMPLE_N = 10


def _should_check_divergence(checkpoint_human_count: int) -> bool:
    """Sampling gate for the dual-store divergence check.

    Always True for the first couple of populated-checkpoint turns; past that,
    True 1-in-``_DUALSTORE_DIVERGENCE_SAMPLE_N`` of the time.
    """
    if checkpoint_human_count < _DUALSTORE_DIVERGENCE_ALWAYS_BELOW:
        return True
    return random.randrange(_DUALSTORE_DIVERGENCE_SAMPLE_N) == 0


async def _chat_user_row_count(
    db: AsyncSession, thread_id: str, *, owner_id: Optional[Any] = None
) -> Optional[int]:
    """Count persisted user-role rows for one thread (tenant-scoped).

    Scoped to a single thread whose ownership was already verified upstream
    (``build_graph_input_messages`` is only ever called with the resolved,
    ownership-verified thread id), so the count is inherently tenant-safe. When
    ``owner_id`` is supplied it additionally JOINs the workspace owner as
    defense-in-depth. Returns ``None`` on a malformed thread id.
    """
    from uuid import UUID

    from sqlalchemy import func, select

    from src.models.chat_message import ChatMessage, MessageRole

    try:
        tid = UUID(thread_id)
    except (ValueError, TypeError, AttributeError):
        return None

    stmt = (
        select(func.count())
        .select_from(ChatMessage)
        .where(
            ChatMessage.thread_id == tid,
            ChatMessage.role == MessageRole.USER,
            # MUST match ``build_thread_seed_messages``: counting superseded
            # rows the seed excludes makes every edited thread trip the
            # dual-store divergence WARN.
            ChatMessage.superseded_by_message_id.is_(None),
        )
    )
    if owner_id is not None:
        from src.models.conversation import Conversation
        from src.models.thread import Thread
        from src.models.workspace import Workspace

        stmt = (
            stmt.join(Thread, ChatMessage.thread_id == Thread.id)
            .join(Conversation, Thread.conversation_id == Conversation.id)
            .join(Workspace, Conversation.workspace_id == Workspace.id)
            .where(Workspace.owner_id == owner_id)
        )
    return int((await db.execute(stmt)).scalar_one())


async def _detect_dualstore_divergence(
    db: AsyncSession,
    thread_id: str,
    *,
    checkpoint_human_count: int,
    owner_id: Optional[Any] = None,
) -> None:
    """Log (never repair) chat/checkpoint divergence for a thread.

    Compares the checkpoint's HumanMessage count against the persisted
    ``chat_messages`` user-row count; a delta outside the healthy window
    (see ``_DUALSTORE_*_HEALTHY_DELTA``) is logged as a structured WARN with
    both counts and bumps ``agent_dualstore_divergence_detected_total``.
    Detection only; never raises.
    """
    try:
        user_rows = await _chat_user_row_count(db, thread_id, owner_id=owner_id)
    except Exception:
        logger.debug("dual-store divergence count query failed", exc_info=True)
        return
    if user_rows is None:
        return

    delta = user_rows - checkpoint_human_count
    if _DUALSTORE_MIN_HEALTHY_DELTA <= delta <= _DUALSTORE_MAX_HEALTHY_DELTA:
        return  # stores agree (within the expected pending-turn offset)

    logger.warning(
        "Agent dual-store divergence detected: LangGraph checkpoint human-count "
        "and chat_messages user-row count disagree for this thread",
        extra={
            "thread_id": thread_id,
            "checkpoint_human_count": checkpoint_human_count,
            "chat_user_rows": user_rows,
            "delta": delta,
        },
    )
    try:
        from src.services.agent.observability import (
            agent_dualstore_divergence_detected_total,
        )

        agent_dualstore_divergence_detected_total.inc()
    except Exception:
        pass


# Seed lock (codex audit CX2): SET NX with a TTL well above a seed's couple of
# DB reads; the TTL bounds the harm of a crashed winner. Plain DELETE release —
# best-effort correctness only, the graph itself stays the source of truth.
_SEED_LOCK_TTL_MS = 15_000


def _seed_lock_key(thread_id: str) -> str:
    return f"agent:seed:lock:{thread_id}"


async def _acquire_seed_lock(thread_id: str) -> bool:
    """Best-effort cross-worker seed lock. False = held elsewhere or no Redis."""
    try:
        from src.services.agent.job_store import get_redis

        redis = await get_redis()
        if redis is None:
            return True  # no Redis → cannot coordinate; pre-lock behavior
        return bool(
            await redis.set(
                _seed_lock_key(thread_id), "1", nx=True, px=_SEED_LOCK_TTL_MS
            )
        )
    except Exception:
        return True  # lock must never block a turn


async def _release_seed_lock(thread_id: str) -> None:
    try:
        from src.services.agent.job_store import get_redis

        redis = await get_redis()
        if redis is not None:
            await redis.delete(_seed_lock_key(thread_id))
    except Exception:
        pass  # TTL reaps it


async def build_graph_input_messages(
    db: AsyncSession,
    graph: Any,
    thread_id: str,
    request_messages: List[Any],
    *,
    current_user: Optional[User] = None,
) -> Optional[List[Any]]:
    """Assemble ``initial_state['messages']`` checkpoint-authoritatively.

    If the checkpoint already holds the thread's history, append ONLY the newest
    user turn and let the reducer accumulate. If the checkpoint is empty, seed
    the whole conversation from the DB (which already includes the just-persisted
    newest turn). Never seeds into a non-empty checkpoint — that would duplicate
    prior assistant turns, whose live LLM-assigned ids can't match a rebuilt id.

    Returns ``None`` when the newest turn has no client_message_id — Option B
    can't assign a safe idempotency id, so the caller uses the legacy path.
    """
    newest = _newest_user_message(request_messages)
    if newest is None:
        return None

    count = await _checkpoint_human_count(graph, thread_id)

    # Dual-store divergence guard (detection only — audit D3 / P2.6). Only a
    # *populated* checkpoint (count > 0) can drift permanently; the empty case
    # below self-heals by reseeding from the DB, so checking it would only emit
    # false positives. Sampled to keep the extra COUNT off the hot path.
    if count is not None and count > 0 and _should_check_divergence(count):
        await _detect_dualstore_divergence(
            db,
            thread_id,
            checkpoint_human_count=count,
            owner_id=getattr(current_user, "id", None),
        )

    if count is None or count > 0:
        # Populated checkpoint (append) OR a failed read (do NOT seed a possibly
        # live checkpoint — that would duplicate its turns). Either way, add only
        # the newest turn and let the reducer accumulate onto existing state.
        return [newest]

    # count == 0: the checkpoint is genuinely empty → rebuild from the DB.
    # Serialize concurrent seeders (codex audit CX2): two simultaneous
    # first-turns can both read count == 0 and both seed. The Redis lock is
    # best-effort — the loser waits for the winner, re-reads the count, and
    # appends only its newest turn once the checkpoint is populated. No Redis
    # (or lock timeout) degrades to the old race, never to a lost turn.
    got_lock = await _acquire_seed_lock(thread_id)
    try:
        if not got_lock:
            for _ in range(20):  # ≤ ~2s — seeding is a couple of DB reads
                await asyncio.sleep(0.1)
                recheck = await _checkpoint_human_count(graph, thread_id)
                if recheck is not None and recheck > 0:
                    return [newest]
                if await _acquire_seed_lock(thread_id):
                    got_lock = True
                    break
            # Winner died or is slow: proceed unlocked (pre-lock behavior).

        seed = await build_thread_seed_messages(db, thread_id)
        if not seed:
            # Empty checkpoint AND empty DB (brand-new / unresolved thread, or
            # a swallowed persist): fall back to the request's newest turn.
            return [newest]

        # Ensure the newest turn survives even if its persist was swallowed;
        # normally it's already in the seed (same client_message_id), so
        # append only if missing.
        if not _seed_has_id(seed, newest.id):
            seed.append(newest)
        return seed
    finally:
        if got_lock:
            await _release_seed_lock(thread_id)


def build_user_history_messages(messages: List[Any], thread_id: str) -> List[Any]:
    """LAST-RESORT fallback: rebuild resent request history into HumanMessages
    with deterministic-where-possible ids.

    Audit review on PR #1395 (Codex, live trace e3c56cef 2026-08-11) confirmed
    a v1 of this fix that derived every id from (thread_id, position) is
    UNSOUND: the ``/chat`` client resends only a windowed page of a long
    thread (e.g. the initial 50-message page), not the full conversation from
    turn 1 — so the index of a resent message is the index WITHIN THE WINDOW,
    not its absolute position in the thread. A later window (turns 11-60)
    replays the same indices (0-49) as an earlier one (turns 1-50), so a
    purely positional id silently REPLACES an unrelated older checkpoint
    message in place (``add_messages`` is an id-keyed upsert) — worse than the
    2x duplication this was meant to fix. This exact failure mode was already
    identified and rejected as "B1" in prior research (see memory
    project_langgraph_history_pattern.md): "adopt Option B ... supersedes B1's
    position-id scheme" — position-based ids are only sound for a client that
    resends the FULL untrimmed history, which this one does not.

    ``build_graph_input_messages`` (Option B, above) is therefore the primary,
    checkpoint-authoritative path and runs unconditionally (see call sites) —
    it never re-derives ids for resent history at all: a populated checkpoint
    gets only the newest turn (keyed on its client_message_id) appended, and
    an empty one is seeded from the DB by row identity. This function is only
    reached when Option B declines (the newest turn carries no
    client_message_id) or a DB/checkpoint read fails — restored to its
    original, pre-audit behavior: prefer client_message_id when present, else
    a (thread_id, position) fallback. That positional fallback still carries
    the same windowing risk described above, but only in this now-rare
    fallback; the alternative (inventing a safer id here) needs checkpoint
    access this sync helper doesn't have. Per review guidance, a bounded
    duplicate (the pre-audit 2x growth) is accepted here over risking silent
    in-place corruption of unrelated history — closing this fallback for good
    needs either a frontend change (resend client_message_id per history
    item, not just the newest) or leaning fully on Option B.
    """
    from langchain_core.messages import HumanMessage

    out: List[Any] = []
    idx = 0
    for m in messages:
        if getattr(m, "role", None) != "user":
            continue
        cmid = getattr(m, "client_message_id", None)
        msg_id = (
            str(cmid)
            if cmid is not None
            else str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{thread_id}:user:{idx}"))
        )
        out.append(HumanMessage(content=m.content, id=msg_id))
        idx += 1
    return out


async def _clear_stale_pending_confirmation(
    graph: Any, config: Dict[str, Any]
) -> Optional[List[str]]:
    """Wipe a stale HITL interrupt from the checkpoint before a fresh turn.

    A pending confirmation can only be answered with ``Command(resume=...)``.
    If the next request is a fresh ``HumanMessage`` instead, the user has
    abandoned the interrupt — re-firing it would block the new turn forever.

    Also resets the per-turn ephemeral counters (``tool_loop_count``,
    ``error_count``, ``reflection_count``). ``preprocessing_node`` already
    resets these on a normal turn entry, but clearing them here makes the
    invariant local to this function so future graph refactors that bypass
    ``preprocessing_node`` cannot silently inherit a stale counter from the
    abandoned turn.

    Returns the list of dropped tool name(s) when state was cleared, or None
    when nothing needed clearing, so callers can observe the drop.
    """
    try:
        snapshot = await graph.aget_state(config)
    except Exception:
        return None
    if not snapshot or not snapshot.values:
        return None
    # A live interrupt is a pending task carrying `.interrupts` — NOT a truthy
    # `pending_confirmation` value (that key is only ever written back as `{}`).
    # The old `if not pending_confirmation` predicate was inverted, so this
    # cleanup never ran and an abandoned interrupt could re-fire on the next turn.
    active_tasks = [t for t in (snapshot.tasks or ()) if getattr(t, "interrupts", None)]
    if not active_tasks:
        return None

    # Extract tool names from the interrupt values so the drop is observable.
    dropped_tools: List[str] = []
    for task in active_tasks:
        for interrupt in task.interrupts:
            value = getattr(interrupt, "value", {}) or {}
            # Guard against a future interrupt site passing a non-dict value
            # (e.g. a bare string) — observability must never raise here.
            if not isinstance(value, dict):
                continue
            for tool in value.get("tools", []):
                name = tool.get("name") if isinstance(tool, dict) else None
                if name:
                    dropped_tools.append(name)

    thread_id = config.get("configurable", {}).get("thread_id")
    try:
        await graph.aupdate_state(
            config,
            {
                "pending_confirmation": {},
                "user_confirmed": False,
                "tool_loop_count": 0,
                "error_count": 0,
                "reflection_count": 0,
            },
        )
    except Exception:
        logger.exception("Failed to clear stale pending_confirmation")
        return None
    logger.warning(
        "Abandoned HITL interrupt silently dropped for thread %s — tools: %s",
        thread_id,
        dropped_tools or "<unknown>",
    )
    return dropped_tools if dropped_tools else ["<unknown>"]


# ---------------------------------------------------------------------------
# Thread persistence helper
# ---------------------------------------------------------------------------


async def _resolve_project_for_thread(
    db: AsyncSession,
    thread_obj: Any,
) -> tuple[Optional[str], Optional[str]]:
    """Return (project_id, project_name) for a thread, using scalar or join fallback.

    If ``source_project_id`` is set, the scalar path is used (fast).  If it is
    NULL, the newest ``project_threads`` row is queried as a fallback so a
    half-linked thread (row exists but column is NULL) still gets correct RAG
    scoping.
    """
    if thread_obj is None:
        return None, None

    from sqlalchemy import select

    from src.models import Collection, ProjectThread

    scalar = getattr(thread_obj, "source_project_id", None)
    if scalar:
        # Resolve the name via an explicit query rather than the
        # ``source_project`` relationship: not every caller eager-loads it,
        # and an implicit lazy-load raises MissingGreenlet in async context.
        row = (
            await db.execute(
                select(Collection.id, Collection.name).where(
                    Collection.id == scalar, Collection.is_deleted.is_(False)
                )
            )
        ).first()
        # Filtering only the *name* would still return the soft-deleted id,
        # which _resolve_and_bind_project writes into page_context and every
        # write tool then resolves — rejected as "Project not found or access
        # denied". Return nothing so the caller falls through to the live-link
        # fallback and, failing that, runs unscoped rather than bound to a
        # dead project.
        if row is None:
            return None, None
        return str(row[0]), row[1]

    # Fallback: newest live project_threads row
    stmt = (
        select(ProjectThread, Collection)
        .join(Collection, ProjectThread.project_id == Collection.id)
        .where(
            ProjectThread.thread_id == thread_obj.id,
            ProjectThread.is_deleted == False,  # noqa: E712
            Collection.is_deleted.is_(False),
        )
        .order_by(ProjectThread.linked_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    row = result.first()
    if row:
        pt, coll = row
        return str(pt.project_id), coll.name
    return None, None


async def _resolve_and_bind_project(
    db: AsyncSession,
    current_user: User,
    thread_obj: Any,
    page_context: Dict[str, Any],
) -> Optional[str]:
    """Fill ``page_context`` project fields and durably bind the agent thread.

    Returns the *verified* project id (or ``None``). Callers that scope
    anything to the project — project-memory recall in particular (R7-M2) —
    must gate on this return value rather than re-reading
    ``page_context["project_id"]``, so the ownership check can never be
    silently decoupled from its consumers by a later refactor.

    The chat UI binds a project to its *workspace* thread, but agent runs
    execute on a separate auto-created agent thread, and the only other
    bridge (``?projectId=`` → ``page_context.project_id``) is dropped by
    thread navigation. Without this resolver the agent forgets the attached
    project on the very next turn / reload.

    Resolution order:
      1. Client-sent ``page_context.project_id`` — ownership-verified; junk
         or foreign IDs are dropped rather than trusted.
      2. The agent thread's own link (scalar column or join row).
      3. The workspace thread named in
         ``page_context.metadata.workspace_thread_id`` (ownership-verified).

    Whenever a project is resolved and the agent thread is not yet linked to
    it, attach it via the idempotent single writer so every future turn
    resolves through path 2 with no client state required. The attach is
    best-effort: a failure there never blocks the turn.
    """
    if thread_obj is None and not page_context.get("project_id"):
        return None

    from uuid import UUID as _UUID

    from sqlalchemy import and_, select

    from src.models import Collection, Conversation, Thread, Workspace

    project_id: Optional[str] = None
    project_name: Optional[str] = None

    # Path 1: client-sent project — verify the caller owns it.
    raw_pid = page_context.get("project_id")
    if raw_pid:
        try:
            pid = _UUID(str(raw_pid))
        except (ValueError, TypeError):
            pid = None
        if pid is not None:
            row = (
                await db.execute(
                    select(Collection.id, Collection.name)
                    .join(Workspace, Collection.workspace_id == Workspace.id)
                    .where(
                        and_(
                            Collection.id == pid,
                            Workspace.owner_id == current_user.id,
                            Collection.is_deleted.is_(False),
                        )
                    )
                )
            ).first()
            if row:
                project_id, project_name = str(row[0]), row[1]
        if project_id is None:
            # Unowned/invalid client value: drop it so it can't scope
            # memory recall or RAG, then fall through to the thread paths.
            page_context["project_id"] = None
            page_context["project_name"] = None

    # Path 2: the agent thread's own link.
    if project_id is None and thread_obj is not None:
        project_id, project_name = await _resolve_project_for_thread(db, thread_obj)

    # Path 3: the workspace thread the chat UI actually binds projects to.
    if project_id is None and thread_obj is not None:
        ws_tid = (page_context.get("metadata") or {}).get("workspace_thread_id")
        if ws_tid and str(ws_tid) != str(thread_obj.id):
            try:
                ws_uuid = _UUID(str(ws_tid))
            except (ValueError, TypeError):
                ws_uuid = None
            if ws_uuid is not None:
                ws_thread = (
                    await db.execute(
                        select(Thread)
                        .join(Conversation, Thread.conversation_id == Conversation.id)
                        .join(Workspace, Conversation.workspace_id == Workspace.id)
                        .where(
                            and_(
                                Thread.id == ws_uuid,
                                Workspace.owner_id == current_user.id,
                                Thread.is_deleted == False,  # noqa: E712
                            )
                        )
                    )
                ).scalar_one_or_none()
                if ws_thread is not None:
                    project_id, project_name = await _resolve_project_for_thread(
                        db, ws_thread
                    )

    if project_id is None:
        return None

    page_context["project_id"] = project_id
    if project_name and not page_context.get("project_name"):
        page_context["project_name"] = project_name
    if not page_context.get("type") or page_context.get("type") == "chat":
        page_context["type"] = "project"

    # Durably bind the agent thread so paths 1/3 are only ever needed once.
    if (
        thread_obj is not None
        and str(getattr(thread_obj, "source_project_id", None) or "") != project_id
    ):
        try:
            from src.models import ProjectThreadLinkType
            from src.services.research.project_thread_service import (
                attach_thread_to_project,
            )

            await attach_thread_to_project(
                db,
                thread_obj,
                _UUID(project_id),
                link_type=ProjectThreadLinkType.FROM_CHAT.value,
                linked_by_id=current_user.id,
                context_note="Auto-bound from chat project context",
            )
            await db.commit()
        except Exception:
            logger.warning(
                "agent_thread_project_bind_failed",
                exc_info=True,
            )
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass

    return project_id


class AgentThreadResolutionError(LookupError):
    """A requested durable thread or workspace cannot accept an agent write."""


async def _resolve_thread(
    db: AsyncSession,
    current_user: User,
    request: Any,  # AgentExecuteRequest
    create_if_missing: bool = True,
) -> tuple[Optional[Any], str]:
    """Resolve an authoritative thread or create one in a deterministic parent."""
    from uuid import UUID

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from src.models.conversation import Conversation
    from src.models.thread import Thread, ThreadStatus
    from src.models.workspace import Workspace
    from src.services.threads import workspace_access

    AGENT_THREAD_MARKER = {"source": "agent"}

    if request.thread_id:
        requested_thread_id = UUID(str(request.thread_id))
        thread = await workspace_access.get_thread(
            db,
            requested_thread_id,
            current_user.id,
            include_messages=False,
        )
        if thread is None or not thread.conversation.workspace.can_user_edit(
            str(current_user.id)
        ):
            raise AgentThreadResolutionError("Thread not found")
        return thread, str(thread.conversation_id)

    if not create_if_missing:
        return None, ""

    requested_workspace_id = getattr(
        getattr(request, "page_context", None), "workspace_id", None
    )
    if requested_workspace_id is not None:
        workspace = await workspace_access.get_workspace(
            db,
            UUID(str(requested_workspace_id)),
            current_user.id,
            load_conversations=False,
            load_collections=False,
        )
        if workspace is None or not workspace.can_user_edit(str(current_user.id)):
            raise AgentThreadResolutionError("Workspace not found")
        workspace_id = workspace.id
        lock_stmt = (
            select(Workspace)
            .options(selectinload(Workspace.members))
            .where(
                Workspace.id == workspace_id,
                Workspace.is_deleted == False,  # noqa: E712
            )
            .with_for_update()
        )
        workspace = (await db.execute(lock_stmt)).scalar_one_or_none()
        if workspace is None or not workspace.can_user_edit(str(current_user.id)):
            raise AgentThreadResolutionError("Workspace not found")
    else:
        ws_stmt = (
            select(Workspace)
            .options(selectinload(Workspace.members))
            .where(
                Workspace.owner_id == current_user.id,
                Workspace.is_deleted == False,  # noqa: E712
            )
            .order_by(Workspace.created_at.asc(), Workspace.id.asc())
            .limit(1)
            .with_for_update()
        )
        workspace = (await db.execute(ws_stmt)).scalar_one_or_none()

    if workspace is None:
        return None, ""

    conversation_stmt = (
        select(Conversation)
        .where(
            Conversation.workspace_id == workspace.id,
            Conversation.is_deleted == False,  # noqa: E712
        )
        .order_by(Conversation.created_at.asc(), Conversation.id.asc())
        .limit(1)
    )
    conv = (await db.execute(conversation_stmt)).scalar_one_or_none()
    if conv is None:
        conv = Conversation(
            id=_uuid.uuid4(),
            workspace_id=workspace.id,
            title="Agent Chat",
            created_by_id=current_user.id,
        )
        db.add(conv)
        await db.flush()

    first_msg = next((m.content for m in request.messages if m.role == "user"), "")
    title = first_msg[:80] if first_msg else "Agent Chat"
    thread = Thread(
        id=_uuid.uuid4(),
        conversation_id=conv.id,
        title=title,
        status=ThreadStatus.ACTIVE,
        created_by_id=current_user.id,
        rag_document_scope=AGENT_THREAD_MARKER,
        message_count=0,
    )
    db.add(thread)
    await db.commit()
    await db.refresh(thread)

    return thread, str(thread.conversation_id)


async def resync_thread_checkpoint(
    graph: Any,
    *,
    thread_id: str,
    user: Any,
) -> bool:
    """Converge a thread's HEAD checkpoint on the DB's post-edit view.

    Edit-and-resend tombstones rows in ``chat_messages``; the LangGraph
    checkpoint is a second store that must stop showing the model the turns the
    user replaced. The obvious implementation — map each tombstoned ROW to a
    checkpoint id and ``RemoveMessage`` it — cannot work: assistant messages in
    the checkpoint carry ids the MODEL generated (``run-...``, provider ids),
    which no row-derived mapping predicts, so the superseded ANSWER survived
    every removal while its question vanished.

    So this does not map. It reads what is actually present, removes ALL of it,
    and re-adds ``build_thread_seed_messages`` (which already excludes
    superseded rows) in ONE ``aupdate_state`` — the same
    ``as_node="memory_save_node"`` seam the fast path appends through, always
    against HEAD (an update carrying an earlier ``checkpoint_id`` forks the
    thread and every other writer then misses the fork). ``add_messages``
    applies the removals and the additions in list order, and an id appearing in
    both is replaced in place — verified against langgraph 1.2.4 /
    langchain-core 1.4.1 in ``test_agent_edit_resend_checkpoint.py``.

    ``add_messages`` RAISES ``ValueError`` on a ``RemoveMessage`` whose id is
    not in state. That is exactly the concurrent-writer TOCTOU: another writer
    can drop a message between our read and our write. We retry ONCE from a
    fresh read, then give up with a WARN — the DB tombstones are already
    committed and every reader filters on them, so a stale checkpoint degrades
    the model's context for one turn rather than failing the turn.

    Residual: this is not serialised against other writers to the same thread.
    A user has one live turn per thread in practice (the UI blocks a second
    send while streaming), so the single-writer assumption holds outside of
    deliberate concurrent-tab abuse; the retry covers the rest.

    Returns ``True`` when the checkpoint was rewritten, ``False`` otherwise
    (nothing to do, or a failure that was warned about). Never raises.
    """
    from langchain_core.messages import RemoveMessage

    config = {
        "configurable": {
            "thread_id": str(thread_id),
            "user_id": str(getattr(user, "id", "") or ""),
            "organization_id": str(getattr(user, "organization_id", "") or ""),
        }
    }

    for attempt in (1, 2):
        try:
            snapshot = await graph.aget_state(config)
            values = getattr(snapshot, "values", None) or {}
            present = [
                str(getattr(m, "id", "") or "") for m in (values.get("messages") or [])
            ]
            # dict.fromkeys: dedupe while preserving order. Two RemoveMessages
            # for one id would make the second one an unknown-id ValueError.
            present_ids = [mid for mid in dict.fromkeys(present) if mid]

            async with AsyncSessionLocal() as db:
                seed = await build_thread_seed_messages(db, str(thread_id))

            if not present_ids and not seed:
                return False

            await graph.aupdate_state(
                config,
                {
                    "messages": [RemoveMessage(id=mid) for mid in present_ids]
                    + list(seed)
                },
                as_node="memory_save_node",
            )
            return True
        except ValueError:
            if attempt == 1:
                logger.info(
                    "edit_resend_checkpoint_resync_retry — a concurrent writer "
                    "changed the head message set; re-reading",
                    extra={"thread_id": str(thread_id)},
                )
                continue
            logger.warning(
                "edit_resend_checkpoint_resync_failed — chat_messages tombstones "
                "are committed but the LangGraph checkpoint may still hold the "
                "superseded turns for this thread",
                extra={"thread_id": str(thread_id)},
                exc_info=True,
            )
            return False
        except Exception:
            logger.warning(
                "edit_resend_checkpoint_resync_failed — chat_messages tombstones "
                "are committed but the LangGraph checkpoint may still hold the "
                "superseded turns for this thread",
                extra={"thread_id": str(thread_id)},
                exc_info=True,
            )
            return False

    return False


class TombstoneReport:
    """Mutable out-parameter for the edit-and-resend tombstone pass.

    Carries two independent facts, because they answer different questions:

    * ``count`` — how many rows this attempt actually UPDATEd. Only that number
      may be subtracted from ``Thread.message_count``.
    * ``any`` — whether the thread now HAS superseded rows attributable to this
      turn, including the ambiguous-commit case where the replacement row was
      already inserted (and its tombstones already committed) by a previous
      attempt whose response was lost. ``count`` is 0 there but the checkpoint
      still needs a resync, so the two must not be collapsed into one integer.

    They also have different lifetimes under retry. ``_persist_user_message_
    guarded`` resets BOTH between attempts (the retry re-derives its outcome
    from a clean transaction), but on final failure it re-marks ``any`` if any
    attempt had seen tombstones: a committed-then-unacknowledged attempt leaves
    durable tombstones that no later failure undoes. ``count`` is never carried
    that way — only rows an attempt actually UPDATEd may adjust
    ``Thread.message_count``.
    """

    __slots__ = ("count", "any")

    def __init__(self) -> None:
        self.count = 0
        self.any = False

    def reset(self) -> None:
        self.count = 0
        self.any = False

    def record(self, count: int) -> None:
        self.count += count
        if count:
            self.any = True

    def mark(self) -> None:
        """Tombstones exist but were not written by this attempt."""
        self.any = True

    def __bool__(self) -> bool:
        return self.any

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TombstoneReport(count={self.count}, any={self.any})"


async def _tombstone_superseded_turns(
    db: AsyncSession,
    current_user: User,
    *,
    thread_id: str,
    supersedes_cmid: Any,
    replacement_row_id: Any,
) -> int:
    """Tombstone an edited user turn and everything after it in the thread.

    Called through ``apply_edit_resend_tombstones`` by whichever writer owns
    this turn's user row — ``_persist_user_message`` or ``accept_submission``'s
    accept transaction — always BEFORE that writer's commit, so the tombstones
    and the replacement user row land in one transaction: a reader never sees a
    thread where the old answer is gone but the new question isn't there yet.

    Marks the target user row *and* every not-already-superseded row ordered
    after it by ``(created_at, id)``. The replacement row is explicitly excluded
    (it is itself the newest row in the thread, so the range predicate would
    otherwise swallow it).

    Returns the NUMBER of rows updated. The caller does not need their ids:
    checkpoint convergence is done by ``resync_thread_checkpoint``, which
    rewrites the whole head message list from the DB rather than mapping
    per-row ids onto checkpoint ids (assistant messages carry model-generated
    ids that no row-derived mapping can predict). Returns ``0`` when the target
    ``client_message_id`` is not in this thread; that is a real race (the fast
    path persists the original turn in a background task,
    ``fast_path``/``stream_fast_path_chunks``), and losing a tombstone must not
    fail the turn.
    """
    from uuid import UUID

    from sqlalchemy import and_, exists, or_, select, update

    from src.models.chat_message import ChatMessage, MessageRole
    from src.models.conversation import Conversation
    from src.models.thread import Thread
    from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole

    try:
        tid = UUID(str(thread_id))
        target_cmid = UUID(str(supersedes_cmid))
    except (ValueError, TypeError, AttributeError):
        logger.warning(
            "edit_resend_tombstone_invalid_ids",
            extra={"thread_id": str(thread_id)},
        )
        return 0

    # Defense in depth: mirror the actual write authorization accepted by
    # ``_resolve_thread`` (owner or live owner/admin/editor membership), and
    # re-check every soft-deletable ancestor. A public workspace grants read
    # access, never edit access, and a stale upstream ORM object must not let a
    # revoked member or deleted ancestor mutate message history.
    editable_membership = exists().where(
        WorkspaceMember.workspace_id == Workspace.id,
        WorkspaceMember.user_id == current_user.id,
        WorkspaceMember.is_deleted.is_(False),
        WorkspaceMember.role.in_(
            (WorkspaceRole.OWNER, WorkspaceRole.ADMIN, WorkspaceRole.EDITOR)
        ),
    )
    editable_thread = (
        select(Thread.id)
        .join(Conversation, Thread.conversation_id == Conversation.id)
        .join(Workspace, Conversation.workspace_id == Workspace.id)
        .where(
            Thread.id == tid,
            Thread.is_deleted.is_(False),
            Conversation.is_deleted.is_(False),
            Workspace.is_deleted.is_(False),
            or_(Workspace.owner_id == current_user.id, editable_membership),
        )
    ).scalar_subquery()

    target = (
        await db.execute(
            select(ChatMessage.id, ChatMessage.created_at).where(
                ChatMessage.thread_id == tid,
                ChatMessage.thread_id.in_(editable_thread),
                ChatMessage.client_message_id == target_cmid,
                ChatMessage.role == MessageRole.USER,
            )
        )
    ).first()

    if target is None:
        logger.warning(
            "edit_resend_supersede_target_not_found",
            extra={
                "thread_id": str(tid),
                "supersedes_client_message_id": str(target_cmid),
            },
        )
        return 0

    target_id, target_created_at = target.id, target.created_at

    # (created_at, id) tie-break, matching the message pagination order.
    # LIMITATION (deliberate): ``created_at`` is app-generated with microsecond
    # precision, so an exact tie means the two rows were written in the same
    # batch — and every reader in the system sorts by ``created_at`` ALONE, so
    # their relative order is already arbitrary there. Breaking the tie on the
    # UUID PK is therefore not "correct ordering", it is only a deterministic,
    # inclusive boundary: the target row itself is always inside the range, and
    # a same-microsecond neighbour may fall on either side of a cut the reader
    # could not have rendered in a stable order anyway.
    at_or_after = or_(
        ChatMessage.created_at > target_created_at,
        and_(
            ChatMessage.created_at == target_created_at,
            ChatMessage.id >= target_id,
        ),
    )

    # ONE statement: a separate SELECT then UPDATE let a concurrent writer land
    # a row between the two, so the returned "tombstoned" set and the rows the
    # UPDATE actually touched could differ. UPDATE ... RETURNING reports exactly
    # what it changed, atomically.
    #
    # Out of scope (by construction, not oversight): rows INSERTed after this
    # statement's snapshot are not tombstoned — the streaming answer to the
    # superseded turn can still be racing us. The next edit of this thread
    # sweeps them, and every reader already filters on the flag.
    result = await db.execute(
        update(ChatMessage)
        .where(
            ChatMessage.thread_id == tid,
            ChatMessage.superseded_by_message_id.is_(None),
            ChatMessage.id != replacement_row_id,
            at_or_after,
        )
        .values(superseded_by_message_id=replacement_row_id)
        .returning(ChatMessage.id)
        .execution_options(synchronize_session=False)
    )
    return len(result.scalars().all())


async def _resolve_deduped_replacement_row(
    db: AsyncSession,
    *,
    thread_id: Any,
    cmid_value: str,
    supersedes: Any,
) -> Any:
    """The already-durable replacement row this edit may legitimately reuse.

    Reached only when the replacement INSERT deduped: this exact replacement
    ``client_message_id`` is already a user row in the thread. Either the client
    resent the same edit, or a previous attempt committed and lost its response
    (ambiguous commit). Reusing that row makes the pass idempotent — but only
    after proving it is a LEGITIMATE replacement, because a recycled cmid must
    never be allowed to tombstone a live turn.

    Returns the row id to tombstone against, or ``None`` when the reuse is
    refused (each refusal logs its reason).
    """
    from uuid import UUID

    from sqlalchemy import select

    from src.models.chat_message import ChatMessage, MessageRole

    existing = (
        await db.execute(
            select(ChatMessage.id, ChatMessage.superseded_by_message_id).where(
                ChatMessage.thread_id == UUID(str(thread_id)),
                ChatMessage.client_message_id == cmid_value,
                ChatMessage.role == MessageRole.USER,
            )
        )
    ).first()
    if existing is None:
        return None

    conflict = (
        # The row we would "replace with" IS the row being superseded (the
        # schema validator rejects this at the edge; a request that reached
        # here another way must not tombstone its own replacement).
        str(cmid_value) == str(supersedes)
        # ...or it has already been superseded by a LATER edit; resurrecting it
        # as a tombstone target would rewrite history backwards.
        or existing.superseded_by_message_id is not None
    )
    if conflict:
        logger.warning(
            "edit_resend_dedup_target_conflict",
            extra={
                "thread_id": str(thread_id),
                "client_message_id": str(cmid_value),
                "supersedes_client_message_id": str(supersedes),
                "already_superseded": existing.superseded_by_message_id is not None,
            },
        )
        return None

    # Bind the existing replacement row to the target THIS request claims. A
    # replacement cmid names exactly one edit; reusing it against a different
    # target is not an idempotent resend, it is a second, unrelated edit riding
    # a row whose content we would silently keep. Accept only when the claimed
    # target is already superseded BY this very row — i.e. this exact edit
    # demonstrably happened.
    claimed = (
        await db.execute(
            select(ChatMessage.superseded_by_message_id).where(
                ChatMessage.thread_id == UUID(str(thread_id)),
                ChatMessage.client_message_id == str(supersedes),
                ChatMessage.role == MessageRole.USER,
            )
        )
    ).first()
    if claimed is None or claimed.superseded_by_message_id != existing.id:
        # Target missing, still active, or superseded by some OTHER row: refuse.
        # Tombstoning here would kill a live turn on the strength of a recycled
        # cmid.
        logger.warning(
            "edit_resend_dedup_target_mismatch",
            extra={
                "thread_id": str(thread_id),
                "client_message_id": str(cmid_value),
                "supersedes_client_message_id": str(supersedes),
                "target_found": claimed is not None,
                "target_active": (
                    claimed is not None and claimed.superseded_by_message_id is None
                ),
            },
        )
        return None
    return existing.id


async def apply_edit_resend_tombstones(
    db: AsyncSession,
    current_user: User,
    *,
    thread_id: Any,
    cmid_value: Optional[str],
    supersedes: Any,
    inserted_row_id: Any,
    tombstoned_out: Optional["TombstoneReport"] = None,
) -> int:
    """Tombstone the edited turn's tail against the replacement row. No commit.

    The one place edit-and-resend semantics live, shared by the two writers of
    a user turn: ``_persist_user_message`` (``/execute`` and the degraded
    ``/stream`` paths) and ``accept_submission``'s atomic accept transaction
    (P0-C, the primary ``/stream`` path). Both call it BEFORE their commit, so
    the tombstones and the replacement row land together: a reader never sees a
    thread where the old answer is gone but the new question isn't there yet.

    ``inserted_row_id`` is the id of a FRESHLY inserted replacement row, or
    ``None`` when the insert deduped — the dedup case is validated by
    ``_resolve_deduped_replacement_row`` and, when accepted, reports through
    ``TombstoneReport.mark()`` rather than ``record()`` (the UPDATE finds
    nothing, because a previous attempt already committed those tombstones,
    but the checkpoint still needs a resync).

    Returns the number of rows this call UPDATEd — the only number that may be
    subtracted from ``Thread.message_count``.
    """
    row_id = inserted_row_id
    if row_id is None:
        if cmid_value is None:
            return 0
        row_id = await _resolve_deduped_replacement_row(
            db,
            thread_id=thread_id,
            cmid_value=cmid_value,
            supersedes=supersedes,
        )
        if row_id is None:
            return 0
        if tombstoned_out is not None:
            tombstoned_out.mark()

    count = await _tombstone_superseded_turns(
        db,
        current_user,
        thread_id=str(thread_id),
        supersedes_cmid=supersedes,
        replacement_row_id=row_id,
    )
    if tombstoned_out is not None:
        tombstoned_out.record(count)
    return count


async def _persist_user_message(
    db: AsyncSession,
    current_user: User,
    request: Any,  # AgentExecuteRequest
    *,
    tombstoned_out: Optional["TombstoneReport"] = None,
) -> bool:
    """Insert the latest user message idempotently.

    Uses ``INSERT ... ON CONFLICT DO NOTHING`` against the partial unique
    index on ``chat_messages (thread_id, client_message_id) WHERE
    client_message_id IS NOT NULL AND role = 'user'`` (Alembic revision
    v0a1b2c3d4e5). The ``index_where`` clause passed here mirrors the
    index predicate exactly so Postgres can infer the index.

    Returns ``True`` if a new row was inserted, ``False`` if a duplicate
    was silently dropped or there is nothing to insert (no user message
    in the request or no ``request.thread_id``).

    Edit-and-resend: when ``request.supersedes_client_message_id`` is set, the
    named user turn and everything after it in the thread are tombstoned in the
    SAME transaction as this insert (see ``_tombstone_superseded_turns``), and
    ``Thread.message_count`` is adjusted by the net delta. The outcome is
    recorded on ``tombstoned_out`` when the caller supplies a
    ``TombstoneReport`` — an out-parameter rather than a changed return type
    because the ``bool`` contract is asserted identity-wise (``result is True``
    / ``is False``) across the guarded wrapper's tests and eight call sites.

    Commits independently of ``_persist_assistant_message``; callers that
    rely on a single all-or-nothing commit must adapt — a partial commit
    (user row durable, assistant row missing) is possible if the
    assistant write later fails.
    """
    from uuid import UUID

    from sqlalchemy import text
    from sqlalchemy.dialects.postgresql import insert

    from src.models.chat_message import ChatMessage, MessageRole
    from src.models.message_attachment import MessageAttachment
    from src.models.thread import Thread
    from src.services.threads import workspace_access

    if request.thread_id is None:
        return False

    last = next((m for m in reversed(request.messages) if m.role == "user"), None)
    if last is None:
        return False

    cmid = getattr(last, "client_message_id", None)
    cmid_value = str(cmid) if cmid is not None else None
    supersedes = getattr(request, "supersedes_client_message_id", None)

    stmt = (
        insert(ChatMessage)
        .values(
            thread_id=UUID(request.thread_id),
            user_id=current_user.id,
            role=MessageRole.USER,
            content=last.content,
            client_message_id=cmid_value,
        )
        .on_conflict_do_nothing(
            index_elements=["thread_id", "client_message_id"],
            # Keep the enum value literal. A SQLAlchemy enum comparison becomes
            # a bind parameter under asyncpg, and PostgreSQL cannot infer a
            # partial unique index from ``role = $n``.
            index_where=text("client_message_id IS NOT NULL AND role = 'user'"),
        )
    )
    attachment_ids = list(getattr(request, "attachment_ids", None) or [])
    # RETURNING is added only for the paths that need the row's PK: the
    # tombstone UPDATE needs the replacement's id, and attachments need a
    # parent to hang off. ON CONFLICT DO NOTHING ... RETURNING yields exactly
    # one row when inserted and zero when deduped — the same signal
    # ``rowcount`` carries, without disturbing the hot path.
    needs_row_id = supersedes is not None or bool(attachment_ids)
    if needs_row_id:
        stmt = stmt.returning(ChatMessage.id)

    result = await db.execute(stmt)
    tombstoned_count = 0
    new_row_id = None
    if needs_row_id:
        new_row_id = result.scalar_one_or_none()
        inserted = new_row_id is not None
    else:
        inserted = result.rowcount == 1
    if supersedes is not None:
        tombstoned_count = await apply_edit_resend_tombstones(
            db,
            current_user,
            thread_id=request.thread_id,
            cmid_value=cmid_value,
            supersedes=supersedes,
            inserted_row_id=new_row_id,
            tombstoned_out=tombstoned_out,
        )
    # Attach only to a row this call actually inserted. On an SSE retry the
    # insert dedups and new_row_id is None, so the attachments are not written
    # twice against the turn that already owns them.
    if attachment_ids and new_row_id is not None:
        owned_ids = await workspace_access.filter_owned_document_ids(
            db, attachment_ids, current_user.id
        )
        for doc_id in owned_ids:
            db.add(MessageAttachment(message_id=new_row_id, document_id=doc_id))
    if inserted or tombstoned_count:
        thread = await db.get(Thread, UUID(request.thread_id))
        if thread is not None:
            # Net delta in the SAME transaction as the tombstone UPDATE: +1 for
            # the replacement row, -N for the rows it superseded. Floored at 0
            # because message_count is a denormalised counter that historically
            # drifts, and a negative count renders as nonsense in the UI.
            delta = (1 if inserted else 0) - tombstoned_count
            # cast: on a loaded instance this attribute is a plain int; the
            # Column[int] descriptor type only confuses max()'s type var.
            current = cast(int, thread.message_count or 0)
            thread.message_count = max(0, current + delta)
            if inserted:
                thread.last_message_at = datetime.now(timezone.utc)
    await db.commit()
    return inserted


async def _persist_user_message_guarded(
    db: AsyncSession,
    current_user: User,
    request: Any,  # AgentExecuteRequest
    *,
    tombstoned_out: Optional["TombstoneReport"] = None,
) -> bool:
    """Persist the user turn, retrying once, then loudly marking a failure.

    The user row is one idempotent INSERT written BEFORE the LLM call so the
    turn survives a later graph/stream failure. Historically its failure was
    swallowed (warn-and-continue), so the LangGraph checkpoint could accumulate
    a turn the ``chat_messages`` store never recorded — a permanent divergence
    the user can't see (audit D3 / P2.6). This wrapper makes the persist
    effectively non-optional: one retry, and on final failure a structured WARN
    (thread_id + client_message_id) plus a
    ``agent_dualstore_user_turn_persist_failures_total`` bump so the drop is
    observable instead of silent.

    ``tombstoned_out``, when given, records the edit-and-resend tombstone
    outcome (untouched on every non-edit turn and when the superseded target
    could not be found). It is reset before each attempt so a retry cannot
    double-count.

    Never raises — the caller still continues the turn (a durable-persist
    failure must not abort a chat that can still stream an answer). Returns the
    underlying insert result on success (``True`` inserted / ``False`` duplicate
    or nothing to insert), or ``False`` when both attempts failed.
    """
    last_exc: Optional[Exception] = None
    # ``any`` is STICKY across attempts, ``count`` is not. An attempt can commit
    # its tombstone UPDATEs and still raise on the way out (ambiguous commit) —
    # those rows are durable no matter what the retry does, so the resync signal
    # must survive the between-attempt reset and the final-failure reset. The
    # count cannot: only rows this pass actually UPDATEd may be subtracted from
    # ``Thread.message_count``. A false-positive resync is harmless (it is an
    # idempotent reseed from the DB); a missed one leaves the model answering
    # the turn the user edited away.
    sticky_any = False
    for attempt in (1, 2):
        try:
            if tombstoned_out is None:
                return await _persist_user_message(db, current_user, request)
            # A failed first attempt may have recorded before raising; the
            # retry re-derives the outcome from a clean transaction.
            tombstoned_out.reset()
            return await _persist_user_message(
                db, current_user, request, tombstoned_out=tombstoned_out
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if tombstoned_out is not None:
                sticky_any = sticky_any or tombstoned_out.any
            # Roll the failed INSERT back so the retry (and the rest of the
            # turn) runs on a clean session rather than an aborted transaction.
            try:
                await db.rollback()
            except Exception:
                logger.debug(
                    "rollback after user-turn persist failure failed",
                    exc_info=True,
                )
            if attempt == 1:
                logger.info(
                    "User-turn persist failed; retrying once",
                    extra={"thread_id": getattr(request, "thread_id", None)},
                )

    # Both attempts failed. The COUNT is zeroed — nothing this pass wrote is
    # committed, so nothing may be subtracted from ``Thread.message_count``.
    # ``any`` is restored when ANY attempt saw tombstones: an attempt that
    # committed and then lost its acknowledgement left durable tombstones in the
    # DB, and dropping the signal would skip the checkpoint resync against a
    # thread that HAS been edited.
    if tombstoned_out is not None:
        tombstoned_out.reset()
        if sticky_any:
            tombstoned_out.mark()

    # Stamp an observable divergence marker so the lost turn surfaces on
    # dashboards instead of vanishing silently.
    cmid: Optional[str] = None
    try:
        last = next((m for m in reversed(request.messages) if m.role == "user"), None)
        raw_cmid = getattr(last, "client_message_id", None) if last else None
        cmid = str(raw_cmid) if raw_cmid is not None else None
    except Exception:
        cmid = None
    logger.warning(
        "User-turn persist failed after retry — chat_messages and the "
        "LangGraph checkpoint may diverge for this thread",
        extra={
            "thread_id": getattr(request, "thread_id", None),
            "client_message_id": cmid,
        },
        exc_info=last_exc,
    )
    try:
        from src.services.agent.observability import (
            agent_dualstore_user_turn_persist_failures_total,
        )

        agent_dualstore_user_turn_persist_failures_total.inc()
    except Exception:
        # Metrics are best-effort: never let a bookkeeping failure mask the
        # real error (already logged above).
        pass
    return False


async def _latest_user_client_message_id(
    db: AsyncSession,
    thread_id: str,
) -> Optional[str]:
    """Return the ``client_message_id`` of the thread's latest user row.

    The HITL confirm/resume path can't carry a fresh idempotency key (the
    frontend only sends ``{thread_id, confirmed}``), so the resumed turn's
    assistant row derives its key from the user row that started the turn —
    a double-confirm then hits the assistant partial unique index and dedupes
    instead of leaving a duplicate. Returns ``None`` when the user row
    predates the idempotency column (legacy) or has no cmid.
    """
    from uuid import UUID

    from sqlalchemy import select

    from src.models.chat_message import ChatMessage, MessageRole

    try:
        tid = UUID(thread_id)
    except (ValueError, TypeError, AttributeError):
        return None

    stmt = (
        select(ChatMessage.client_message_id)
        .where(
            ChatMessage.thread_id == tid,
            ChatMessage.role == MessageRole.USER,
            ChatMessage.client_message_id.isnot(None),
            # Resume keys off the latest NON-superseded user row; an edited
            # turn is no longer the turn being resumed.
            ChatMessage.superseded_by_message_id.is_(None),
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    cmid = result.scalar_one_or_none()
    return str(cmid) if cmid is not None else None


async def _persist_assistant_message(
    db: AsyncSession,
    *,
    thread_id: str,
    content: str,
    model_name: Optional[str],
    tool_executions_out: Optional[list],
    retrieved_contexts: Optional[list] = None,
    latency_ms: Optional[int] = None,
    ttft_ms: Optional[int] = None,
    stopped: bool = False,
    client_message_id: Optional[str] = None,
    plan: Optional[list] = None,
    plan_reasoning: Optional[str] = None,
    reasoning_summary: Optional[str] = None,
    token_usage: Optional[dict] = None,
    progress_steps: Optional[list] = None,
) -> Optional[str]:
    """Insert the assistant turn and bump ``thread.message_count`` by 1.

    ``plan`` (planner steps), ``plan_reasoning`` (the planner's top-level
    rationale for ``plan``), and ``token_usage``
    ({input_tokens, output_tokens}) are per-turn provenance persisted as
    JSONB/text so a page reload can rehydrate them; pass ``None`` when the
    turn produced none of them (they stay NULL, not empty containers).

    Commits independently of ``_persist_user_message``. A failure here
    after a successful user-row commit leaves the user message durable
    without its assistant counterpart — callers that depend on the old
    single-commit behavior must handle this.

    When ``client_message_id`` is provided the insert is idempotent
    against the assistant-role partial unique index (mirror of the
    user-row index from v0a1b2c3d4e5) so an SSE retry/reconnect for the
    same turn cannot duplicate the assistant row. On a dedup hit the
    existing row id is returned and the thread stats are NOT re-bumped.

    Returns the persisted (or pre-existing) message id, or ``None`` when
    nothing was written.
    """
    from uuid import UUID

    from sqlalchemy import select, text
    from sqlalchemy.dialects.postgresql import insert

    from src.models.chat_message import ChatMessage, MessageRole
    from src.models.citation import Citation as CitationModel
    from src.models.thread import Thread

    tool_exec_data = None
    if tool_executions_out:
        tool_exec_data = [
            {
                "id": te.id,
                "tool_name": te.tool_name,
                "tool_display_name": te.tool_display_name,
                "args": te.args,
                "status": te.status,
                "result": te.result,
                "error": te.error,
                "duration_ms": te.duration_ms,
            }
            for te in tool_executions_out
        ]

    values = dict(
        thread_id=UUID(thread_id),
        role=MessageRole.ASSISTANT,
        content=content,
        model_name=model_name,
        tool_executions=tool_exec_data,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
        stopped=stopped,
        client_message_id=client_message_id,
        plan=plan,
        plan_reasoning=plan_reasoning,
        reasoning_summary=reasoning_summary,
        token_usage=token_usage,
        progress_steps=progress_steps,
    )

    if client_message_id is not None:
        stmt = (
            insert(ChatMessage)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["thread_id", "client_message_id"],
                # See the user-row upsert above: this must compile as a literal
                # predicate for PostgreSQL partial-index inference.
                index_where=text(
                    "client_message_id IS NOT NULL AND role = 'assistant'"
                ),
            )
            .returning(ChatMessage.id)
        )
        inserted_id = (await db.execute(stmt)).scalar_one_or_none()
        if inserted_id is None:
            # Dedup hit: a retry of an already-persisted turn. Fetch the
            # existing row id and leave thread stats/citations untouched.
            # Deliberately NOT filtered on ``superseded_by_message_id``: this is
            # an idempotency read, and a tombstoned row still occupies the
            # unique index slot this retry collided with.
            existing = await db.execute(
                select(ChatMessage.id).where(
                    ChatMessage.thread_id == UUID(thread_id),
                    ChatMessage.client_message_id == client_message_id,
                    ChatMessage.role == MessageRole.ASSISTANT,
                )
            )
            await db.commit()
            existing_id = existing.scalar_one_or_none()
            return str(existing_id) if existing_id is not None else None
        msg_id = inserted_id
    else:
        msg = ChatMessage(**values)
        db.add(msg)
        await db.flush()
        msg_id = msg.id

    if retrieved_contexts:
        for source_position, ctx in enumerate(retrieved_contexts, 1):
            doc_id = ctx.get("document_id")
            db.add(
                CitationModel(
                    message_id=msg_id,
                    document_id=_coerce_citation_document_id(doc_id),
                    external_reference_id=ctx.get("external_reference_id"),
                    document_title=ctx.get("title"),
                    snippet=ctx.get("content", "")[:2000],
                    source_position=source_position,
                    chunk_id=ctx.get("chunk_id"),
                    chunk_index=ctx.get("chunk_index"),
                    page_number=ctx.get("page_number"),
                    score=ctx.get("score"),
                    rerank_score=ctx.get("rerank_score"),
                )
            )

    thread = await db.get(Thread, _uuid.UUID(thread_id))
    if thread is not None:
        thread.message_count = (thread.message_count or 0) + 1
        thread.last_message_at = datetime.now(timezone.utc)
    await db.commit()

    # Mirror chat_service.create_message's summarization trigger so
    # server-canonical /chat threads get titles/summaries too. Celery-only
    # (the task drives the sync ThreadSummarizationService in the worker);
    # never let a broker hiccup break persistence.
    if thread is not None and (thread.message_count or 0) >= 3:
        try:
            from src.services.threads.thread_summarization_service import (
                enqueue_summarization,
            )

            enqueue_summarization(thread_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to queue summarization for thread %s: %s", thread_id, exc
            )

    return str(msg_id)


async def _persist_assistant_message_safe(
    *,
    thread_id: str,
    content: str,
    model_name: Optional[str],
    tool_executions_out: Optional[list],
    retrieved_contexts: Optional[list] = None,
    latency_ms: Optional[int] = None,
    ttft_ms: Optional[int] = None,
    stopped: bool = False,
    client_message_id: Optional[str] = None,
    plan: Optional[list] = None,
    plan_reasoning: Optional[str] = None,
    reasoning_summary: Optional[str] = None,
    token_usage: Optional[dict] = None,
    progress_steps: Optional[list] = None,
    required: bool = False,
) -> Optional[str]:
    """Background-task-safe wrapper around ``_persist_assistant_message``.

    Opens its own ``AsyncSessionLocal()`` so it doesn't depend on the
    request session being alive — by the time FastAPI runs background
    tasks the original streaming session has already been closed.
    Swallows + logs exceptions for best-effort background writes. Callers that
    cannot report success without the row pass ``required=True`` and receive
    the failure after the metric is recorded. Bumps
    ``agent_assistant_persist_failures_total`` on failure so dashboards
    surface silently-lost assistant rows.
    """
    try:
        async with AsyncSessionLocal() as db:
            persisted_id = await _persist_assistant_message(
                db,
                thread_id=thread_id,
                content=content,
                model_name=model_name,
                tool_executions_out=tool_executions_out,
                retrieved_contexts=retrieved_contexts,
                latency_ms=latency_ms,
                ttft_ms=ttft_ms,
                stopped=stopped,
                client_message_id=client_message_id,
                plan=plan,
                plan_reasoning=plan_reasoning,
                reasoning_summary=reasoning_summary,
                token_usage=token_usage,
                progress_steps=progress_steps,
            )
            if required and persisted_id is None:
                raise RuntimeError("Assistant message persistence returned no id")
            return persisted_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Background assistant persist failed for thread %s: %s",
            thread_id,
            exc,
            exc_info=exc,
        )
        try:
            from src.services.agent.observability import (
                agent_assistant_persist_failures_total,
            )

            agent_assistant_persist_failures_total.inc()
        except Exception:
            # Metrics path is best-effort: never let a bookkeeping
            # failure mask the real error (already logged above).
            pass
        if required:
            raise
        return None


async def _mark_assistant_message_stopped_safe(
    *,
    thread_id: str,
    message_id: str,
) -> bool:
    """Mark an already-written assistant row as the stopped partial.

    A durable Stop can win in the small window after a producer writes the
    answer row but before it commits ``run.completed``. Reusing the insert
    helper would dedupe and leave ``stopped=false``; this focused projection
    keeps the backend as the sole writer while preserving the row identity.
    """
    from uuid import UUID

    from sqlalchemy import update

    from src.models.chat_message import ChatMessage, MessageRole

    try:
        message_uuid = UUID(message_id)
        thread_uuid = UUID(thread_id)
    except (ValueError, AttributeError, TypeError):
        # Unit doubles and degraded, threadless streams can carry opaque ids;
        # there is no valid database row to project onto in that case.
        return False

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                update(ChatMessage)
                .where(
                    ChatMessage.id == message_uuid,
                    ChatMessage.thread_id == thread_uuid,
                    ChatMessage.role == MessageRole.ASSISTANT,
                )
                .values(stopped=True)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            return bool(result.rowcount)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to mark assistant row stopped for thread %s: %s",
            thread_id,
            exc,
            exc_info=exc,
        )
        return False


# ---------------------------------------------------------------------------
# Background graph runner
# ---------------------------------------------------------------------------


def _extract_pending_interrupt(snapshot: Any) -> Optional[Dict[str, Any]]:
    """Return the first pending interrupt's confirmation payload, or None.

    With a checkpointer attached (this graph always has one), LangGraph's
    ``interrupt()`` does NOT raise ``GraphInterrupt`` to an ``ainvoke()``
    caller — it pauses the graph and persists the pause to the checkpoint.
    ``except GraphInterrupt`` around a plain ``ainvoke()`` call is therefore a
    defensive fallback, not the reliable detection path (proven in
    ``tests/unit/agent/test_interrupt_ainvoke_semantics.py``; a LangSmith
    trace audit found create_project/ingest silently never triggered it).
    The real signal is a pending task carrying ``.interrupts`` on the
    checkpoint snapshot — the same mechanism streaming.py's SSE path
    (its primary, working detection) and this module's pre-resume
    ownership check already use.
    """
    pending_tasks = snapshot.tasks if snapshot else ()
    for task in pending_tasks:
        for intr in getattr(task, "interrupts", None) or ():
            return getattr(intr, "value", {}) or {}
    return None


@contextlib.asynccontextmanager
async def _run_heartbeat(job_id: str, *, interval_seconds: Optional[float] = None):
    """Live-run heartbeat (audit S2-M15).

    In-process (SSE / background-dispatch) executions stamp
    ``agent_runs.updated_at`` once and then run silently for up to the graph's
    hard timeout — their only sweeper protection was a config margin. While
    the guarded block runs, this bumps ``updated_at`` every
    ``AGENT_RUN_HEARTBEAT_SECONDS`` so ``sweep_stale_agent_runs`` sees real
    liveness instead of hoping STALE_AFTER > timeout holds.

    Each beat uses its own short-lived session (the runner's session must not
    be pinned by the heartbeat); failures are logged, never raised — a broken
    heartbeat degrades to the pre-fix margin, never kills the run. The touch
    is guarded to non-terminal rows, so a beat that races the final write
    cannot resurrect a finished run.
    """
    if interval_seconds is None:
        from src.core.config import get_settings

        interval_seconds = float(
            getattr(get_settings(), "AGENT_RUN_HEARTBEAT_SECONDS", 60)
        )
    if interval_seconds <= 0:
        # Disabled explicitly — behave exactly as before the heartbeat existed.
        yield
        return

    from src.services.agent import agent_run_service

    async def _beat() -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                async with AsyncSessionLocal() as db:
                    await agent_run_service.touch_run_updated_at(db, job_id)
                    await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "agent-run heartbeat failed for %s", job_id, exc_info=True
                )

    beat_task = asyncio.create_task(_beat())
    try:
        yield
    finally:
        beat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat_task


async def _run_agent_graph(
    job_id: str,
    request: Any,  # AgentExecuteRequest
    current_user: User,
):
    """Run the LangGraph agent graph in the background and update job status."""
    from langgraph.errors import GraphInterrupt

    from src.services.agent.checkpointer import get_checkpointer
    from src.services.agent.graph import compile_agent_graph
    from src.services.agent.memory import get_memory_store

    schemas = _get_schemas()
    ToolExecutionResponse = schemas["ToolExecutionResponse"]
    AgentExecuteResponse = schemas["AgentExecuteResponse"]
    AgentMessage = schemas["AgentMessage"]
    RetrievedContextResponse = schemas["RetrievedContextResponse"]
    latest_user_message = next(
        (message for message in reversed(request.messages) if message.role == "user"),
        None,
    )
    client_message_id = getattr(latest_user_message, "client_message_id", None)

    async with AsyncSessionLocal() as db:
        try:
            # Persist the user turn BEFORE the LLM call so a graph failure or
            # client cancellation still leaves the user row durable. The
            # assistant row continues to be written after the graph
            # finishes — Task 4 of docs/plans/2026-05-13-agent-persist-perf.md.
            resolved_thread_id: Optional[str] = None
            thread_obj = None
            tombstones = TombstoneReport()
            try:
                thread_obj, _conversation_id = await _resolve_thread(
                    db, current_user, request
                )
                if thread_obj is not None:
                    resolved_thread_id = str(thread_obj.id)
                    if request.thread_id != resolved_thread_id:
                        request.thread_id = resolved_thread_id
                    # Retry-once + observable-on-failure so a swallowed persist
                    # can't silently diverge the two stores (audit D3 / P2.6).
                    await _persist_user_message_guarded(
                        db, current_user, request, tombstoned_out=tombstones
                    )
            except AgentThreadResolutionError:
                # An explicit thread/workspace is authoritative. Access loss
                # between edge validation and worker dispatch must fail the
                # run, never continue against an ephemeral checkpoint.
                raise
            except Exception:
                logger.warning(
                    "Failed to persist user turn before agent graph run",
                    exc_info=True,
                )

            # Configure LangSmith tracing if available
            try:
                from src.services.agent.observability import configure_langsmith

                configure_langsmith()
            except Exception:
                pass

            checkpointer = await get_checkpointer()
            store = await get_memory_store()
            graph = compile_agent_graph(checkpointer=checkpointer, store=store)

            # Edit-and-resend: converge HEAD on the post-edit DB before any
            # model input is assembled, exactly as the SSE graph route does.
            if tombstones.any:
                await resync_thread_checkpoint(
                    graph,
                    thread_id=resolved_thread_id or (request.thread_id or ""),
                    user=current_user,
                )

            from src.core.config import get_settings

            messages = None
            if get_settings().AGENT_SERVER_SIDE_HISTORY:
                # Option B: rebuild context from the checkpoint (seeding from the
                # DB when empty); ignore all but the newest turn in the request.
                # Best-effort: a DB/checkpoint failure (or a newest turn lacking a
                # client_message_id -> None) falls back to the legacy path so a
                # turn that works today is never aborted by the opt-in path.
                try:
                    # Seed only from the ownership-verified thread id (set by
                    # _resolve_thread); never the raw client-supplied thread_id.
                    messages = await build_graph_input_messages(
                        db,
                        graph,
                        resolved_thread_id or "",
                        request.messages,
                        current_user=current_user,
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
                messages = build_user_history_messages(
                    request.messages, request.thread_id or job_id
                )

            page_context = _page_context_to_dict(request.page_context)
            _pm_project_id = await _resolve_and_bind_project(
                db, current_user, thread_obj, page_context
            )

            # Project-scoped memory: durable facts the user saved for this
            # project, recalled across every thread. Best-effort; never blocks
            # a turn. Gated on the ownership-verified id returned above, and
            # org-scoped (R7-M2) so a raw client-supplied project_id can never
            # pull another tenant's memories into the prompt.
            project_memories: list = []
            if _pm_project_id:
                try:
                    from src.services.research.project_memory_service import (
                        load_project_memories,
                    )

                    project_memories = await load_project_memories(
                        db,
                        str(_pm_project_id),
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
                job_id=job_id,
            )

            initial_state = {
                "messages": messages,
                "page_context": page_context,
                "retrieved_contexts": [],
                "tool_executions": [],
                "thread_id": request.thread_id or "",
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
                "model": request.model,
                "use_rag": request.use_rag,
                **runtime_state_fields(
                    runtime_snapshot, page_context.get("project_id")
                ),
            }

            config = {
                "recursion_limit": RECURSION_LIMIT,
                "run_name": "agent:background",
                # Ids only (audit B8): graph nodes/tools open their own
                # tool_session() and re-load the user org-scoped — never
                # smuggle the live AsyncSession / ORM User through config.
                "configurable": {
                    "thread_id": request.thread_id or job_id,
                    "user_id": str(current_user.id),
                    "organization_id": str(
                        getattr(current_user, "organization_id", "") or ""
                    ),
                    "page_context": page_context,
                    **runtime_config_fields(
                        runtime_snapshot.id, page_context.get("project_id")
                    ),
                },
                # LangSmith run metadata — makes traces filterable per
                # tenant/turn (saved views by user_id / org_id / thread_id).
                # Inherited by child runs; never carries secrets.
                "metadata": build_trace_metadata(
                    trace_source=TraceSource.GRAPH,
                    user_id=current_user.id,
                    org_id=getattr(current_user, "organization_id", None),
                    thread_id=resolved_thread_id,
                    agent_run_id=job_id,
                    client_message_id=client_message_id,
                ),
            }

            try:
                # Drop any stale HITL interrupt left over from a previous turn
                # the user abandoned (e.g. /new in the CLI). A fresh
                # HumanMessage cannot resume an interrupt, so re-firing the
                # old one would block this turn forever.
                await _clear_stale_pending_confirmation(graph, config)

                # Everything the graph needs is already materialized into
                # initial_state/config above — commit now (cheap: a read-only
                # txn end) so the up-to-360s ainvoke below doesn't pin this
                # session's pooled connection for the run's duration (audit
                # M9). `db` stays open (not closed) because the post-ainvoke
                # block below still uses it; safe because AsyncSessionLocal is
                # expire_on_commit=False (database.py), so thread_obj and
                # other already-loaded attributes stay readable without a
                # fresh round-trip.
                await db.commit()

                # S2-M15: heartbeat updated_at while the graph runs so the
                # staleness sweeper can never mistake a live run for dead.
                async with _run_heartbeat(job_id), asyncio.timeout(360):
                    final_state = await graph.ainvoke(initial_state, config=config)

                # Primary interrupt detection — see _extract_pending_interrupt.
                # ainvoke() returning without raising does NOT mean the turn
                # completed; the graph may have paused at interrupt_node.
                confirmation_details = _extract_pending_interrupt(
                    await graph.aget_state(config)
                )
                if confirmation_details is not None:
                    await _set_job_async(
                        job_id,
                        {
                            "status": JobStatus.AWAITING_CONFIRMATION,
                            "confirmation": confirmation_details,
                            "tool_executions": [],
                            **_actor_fields(current_user),
                            "request": request.model_dump(mode="json"),
                        },
                    )
                    return
            except GraphInterrupt as exc:
                # Defensive fallback — see _extract_pending_interrupt docstring.
                confirmation_details = extract_interrupt_confirmation(exc)
                await _set_job_async(
                    job_id,
                    {
                        "status": JobStatus.AWAITING_CONFIRMATION,
                        "confirmation": confirmation_details,
                        "tool_executions": [],
                        **_actor_fields(current_user),
                        "request": request.model_dump(mode="json"),
                    },
                )
                return

            # Extract assistant content from the last AI message
            assistant_content = ""
            for msg in reversed(final_state["messages"]):
                if hasattr(msg, "type") and msg.type == "ai" and msg.content:
                    assistant_content = msg.content
                    break

            # User row was already persisted up-front (before the graph ran).
            # Only write the assistant row here — Task 4 of
            # docs/plans/2026-05-13-agent-persist-perf.md.
            thread_id, conversation_id = "", ""
            try:
                tool_executions_out = [
                    ToolExecutionResponse(**te)
                    for te in final_state.get("tool_executions", [])
                ] or None
                if resolved_thread_id is not None:
                    thread_id = resolved_thread_id
                    # Re-fetch conversation_id for the response payload. The
                    # _resolve_thread call already returned it but the local
                    # variable was scoped to the up-front block; fetch from
                    # the thread row to avoid threading an extra variable.
                    from uuid import UUID as _UUID

                    from src.models.thread import Thread as _Thread

                    thread_row = await db.get(_Thread, _UUID(thread_id))
                    if thread_row is not None:
                        conversation_id = str(thread_row.conversation_id)
                    # Run inline (no HTTP response to release here) but
                    # route through the safe wrapper so background and
                    # worker paths share the failure-metric bump on a
                    # bad commit — Task 5 of
                    # docs/plans/2026-05-13-agent-persist-perf.md.
                    _job_in_tok, _job_out_tok = _sum_message_usage(
                        final_state.get("messages")
                    )
                    persisted_assistant_id = await _persist_assistant_message_safe(
                        thread_id=thread_id,
                        content=assistant_content,
                        model_name=request.model,
                        tool_executions_out=tool_executions_out,
                        retrieved_contexts=final_state.get("retrieved_contexts"),
                        plan=final_state.get("plan") or None,
                        plan_reasoning=final_state.get("plan_reasoning") or None,
                        token_usage=(
                            {
                                "input_tokens": _job_in_tok,
                                "output_tokens": _job_out_tok,
                            }
                            if (_job_in_tok or _job_out_tok)
                            else None
                        ),
                        required=True,
                    )
                    if persisted_assistant_id is None:
                        raise RuntimeError(
                            "Assistant message persistence returned no id"
                        )
            except Exception as e:
                logger.warning("Failed to persist thread", exc_info=e)
                raise

            response_model_name: str = getattr(request, "model", "") or ""
            # Token cost on the job path (the SSE path records its own). Reads
            # usage off the final messages since ainvoke doesn't stream events.
            in_tok, out_tok = _sum_message_usage(final_state.get("messages"))
            if in_tok or out_tok:
                try:
                    from src.services.agent.observability import record_token_usage

                    record_token_usage(
                        response_model_name or "unknown", in_tok, out_tok
                    )
                except Exception:
                    logger.debug("record_token_usage failed", exc_info=True)
            result = AgentExecuteResponse(
                message=AgentMessage(role="assistant", content=assistant_content),
                model=response_model_name,
                usage={"input_tokens": in_tok, "output_tokens": out_tok},
                finish_reason="stop",
                timestamp=datetime.now(timezone.utc).isoformat(),
                rag_enabled=request.use_rag,
                retrieved_contexts=[
                    RetrievedContextResponse(**rc)
                    for rc in final_state.get("retrieved_contexts", [])
                ]
                or None,
                tool_executions=[
                    ToolExecutionResponse(**te)
                    for te in final_state.get("tool_executions", [])
                ]
                or None,
                thread_id=thread_id,
                conversation_id=conversation_id,
            )

            await _set_job_async(
                job_id,
                {
                    "status": JobStatus.COMPLETED,
                    "result": result.model_dump(),
                    "tool_executions": list(final_state.get("tool_executions", [])),
                    **_actor_fields(current_user),
                },
            )
        except asyncio.CancelledError:
            # CancelledError inherits from BaseException (since Python 3.8),
            # so the broader ``except Exception`` below would NOT catch it
            # and the job would stay stuck in ``"running"`` forever. Mark it
            # cancelled first, then re-raise so the task tears down cleanly.
            logger.warning("Agent graph execution cancelled", extra={"job_id": job_id})
            try:
                await _set_job_async(
                    job_id,
                    {
                        "status": JobStatus.CANCELLED,
                        "error": "execution cancelled",
                        **_actor_fields(current_user),
                    },
                )
            except Exception:
                logger.exception("Failed to mark cancelled job %s", job_id)
            raise
        except asyncio.TimeoutError:
            logger.error("Agent graph execution timed out", extra={"job_id": job_id})
            await _set_job_async(
                job_id,
                {
                    "status": JobStatus.FAILED,
                    "error": "Agent execution timed out after 360s",
                    **_actor_fields(current_user),
                },
            )
        except Exception as e:
            logger.error("Agent graph execution failed", exc_info=e)
            await _set_job_async(
                job_id,
                {
                    "status": JobStatus.FAILED,
                    "error": client_safe_error(e),
                    **_actor_fields(current_user),
                },
            )


async def _resume_agent_graph(
    job_id: str,
    confirmed: bool,
    current_user: User,
):
    """Resume the agent graph after human confirmation."""
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Command

    from src.services.agent.checkpointer import get_checkpointer
    from src.services.agent.graph import compile_agent_graph
    from src.services.agent.memory import get_memory_store

    schemas = _get_schemas()
    AgentExecuteRequest = schemas["AgentExecuteRequest"]
    ToolExecutionResponse = schemas["ToolExecutionResponse"]
    AgentExecuteResponse = schemas["AgentExecuteResponse"]
    AgentMessage = schemas["AgentMessage"]

    async with AsyncSessionLocal() as db:
        try:
            # L1 first, then Redis: in Celery dispatch mode (or behind a
            # multi-replica API) the pod resuming the confirm may not be the
            # pod that dispatched, so the request payload only exists in
            # Redis. Without it the resume falls back to thread_id=job_id and
            # can never find the interrupt.
            job = _get_job(job_id) or await _get_job_async(job_id)
            original_request = None
            if job and job.get("request"):
                original_request = AgentExecuteRequest(**job["request"])

            resume_thread_id = original_request.thread_id if original_request else None
            if not resume_thread_id and original_request is not None:
                # Legitimate no-thread run: a user with no workspace gets
                # ``_resolve_thread -> None``, so ``_run_agent_graph``
                # checkpointed under ``thread_id=job_id``. The payload is
                # present and simply carries no thread_id — resume where the
                # run actually wrote, or such users could never confirm.
                resume_thread_id = job_id
            if not resume_thread_id:
                # R7-L13: the job payload is gone, so job_id is NOT a thread
                # id here. Using it read an empty checkpoint, which skipped the
                # ownership and interrupt-consumed guards below and failed the
                # run anyway. Fail loudly instead; /confirm refuses this case
                # up front.
                logger.warning(
                    "Resume aborted for %s: no job payload (expired)", job_id
                )
                await _set_job_async(
                    job_id,
                    {
                        "status": JobStatus.FAILED,
                        "error": (
                            "This confirmation has expired. "
                            "Please start the request again."
                        ),
                        **_actor_fields(current_user),
                    },
                )
                return

            if original_request is not None and original_request.thread_id:
                try:
                    await _resolve_thread(
                        db,
                        current_user,
                        original_request,
                        create_if_missing=False,
                    )
                except (AgentThreadResolutionError, ValueError):
                    await _set_job_async(
                        job_id,
                        {
                            "status": JobStatus.FAILED,
                            "error": "Thread not found",
                            **_actor_fields(current_user),
                        },
                    )
                    return

            checkpointer = await get_checkpointer()
            store = await get_memory_store()
            graph = compile_agent_graph(checkpointer=checkpointer, store=store)

            config = {
                "recursion_limit": RECURSION_LIMIT,
                "run_name": "agent:background:resume",
                # Ids only (audit B8) — see _run_agent_graph's run config.
                "configurable": {
                    "thread_id": resume_thread_id,
                    "user_id": str(current_user.id),
                    "organization_id": str(
                        getattr(current_user, "organization_id", "") or ""
                    ),
                    "page_context": (
                        _page_context_to_dict(original_request.page_context)
                        if original_request
                        else {}
                    ),
                },
            }

            # Verify thread ownership before resuming. Checkpoints without an
            # owner predate the ownership field and cannot be safely resumed.
            snapshot = await graph.aget_state(config)
            if snapshot and snapshot.values:
                from src.services.agent.runtime_snapshot import (
                    resume_runtime_config_fields,
                )

                config["configurable"].update(
                    resume_runtime_config_fields(snapshot.values)
                )
            # Pre-resume checkpoint id -> deterministic assistant idempotency
            # key, captured BEFORE the resume advances the checkpoint so a
            # double-confirm derives the same key. Mirrors streaming.py's
            # _resume_assistant_cmid (its checkpoint-anchored branch).
            try:
                resume_ckpt_id = (
                    (snapshot.config or {})["configurable"]["checkpoint_id"]
                    if snapshot
                    else None
                )
            except Exception:
                resume_ckpt_id = None
            if snapshot and snapshot.values:
                snapshot_user_id = snapshot.values.get("user_id")
                if not snapshot_user_id or snapshot_user_id != str(current_user.id):
                    logger.warning(
                        "HITL ownership mismatch: job %s thread owned by %s, requested by %s",
                        job_id,
                        snapshot_user_id,
                        current_user.id,
                    )
                    # Stamp the *requesting* user so their polling sees the
                    # error; never the snapshot owner. Status is FAILED — the
                    # legacy failed/error split is collapsed (audit C7).
                    await _set_job_async(
                        job_id,
                        {
                            "status": JobStatus.FAILED,
                            "error": "Thread not found",
                            **_actor_fields(current_user),
                        },
                    )
                    return

                # Idempotency guard: if the interrupt has already been
                # consumed (e.g. by a prior resume that completed without
                # updating the in-memory job, or by a stale background
                # task firing late), short-circuit instead of issuing a
                # second Command(resume=...) that would have nothing to
                # resume against.
                #
                # A live interrupt shows up as a pending task carrying
                # `.interrupts` (the same signal streaming.py:480 uses), NOT as a
                # truthy `pending_confirmation` value — that key is only ever
                # written back as `{}` once the interrupt is consumed, so the old
                # `if not pending_confirmation` predicate was inverted and
                # rejected EVERY legitimate first resume.
                has_pending_interrupt = any(
                    getattr(t, "interrupts", None) for t in (snapshot.tasks or ())
                )
                if not has_pending_interrupt:
                    logger.warning(
                        "Resume requested for job %s but no pending interrupt in "
                        "checkpoint; interrupt already consumed",
                        job_id,
                    )
                    await _set_job_async(
                        job_id,
                        {
                            # Collapsed from the legacy "error" status (C7).
                            "status": JobStatus.FAILED,
                            "error": "Interrupt already consumed",
                            **_actor_fields(current_user),
                        },
                    )
                    return

            durable_run = await get_run(
                db,
                job_id,
                organization_id=getattr(current_user, "organization_id", None),
                user_id=current_user.id,
            )
            if asyncio.iscoroutine(
                durable_run
            ):  # fail closed on a malformed DB adapter
                durable_run.close()
                durable_run = None
            verified_thread_id = None
            if (
                durable_run is not None
                and durable_run.thread_id is not None
                and str(durable_run.thread_id) == str(resume_thread_id)
            ):
                verified_thread_id = durable_run.thread_id
            config["metadata"] = build_trace_metadata(
                trace_source=TraceSource.GRAPH,
                user_id=current_user.id,
                org_id=getattr(current_user, "organization_id", None),
                thread_id=verified_thread_id,
                request_id=job_id,
                agent_run_id=(durable_run.job_id if durable_run is not None else None),
                user_message_id=(
                    durable_run.user_message_id if durable_run is not None else None
                ),
                client_message_id=(
                    durable_run.client_message_id if durable_run is not None else None
                ),
            )

            # See the parallel commit in _run_agent_graph above (audit M9) —
            # get_run() above is a bare SELECT, so without this the session
            # holds its pooled connection through the whole confirm run.
            await db.commit()

            # Same heartbeat as the fresh-run path (S2-M15): a confirm resume
            # is equally live and equally silent between status writes.
            async with _run_heartbeat(job_id), asyncio.timeout(360):
                final_state = await graph.ainvoke(
                    Command(resume={"confirmed": confirmed}),
                    config=config,
                )

            # Primary interrupt detection (mirrors _run_agent_graph and the
            # pre-resume check above) — a multi-step destructive flow can
            # re-fire interrupt() during resume without raising GraphInterrupt.
            confirmation_details = _extract_pending_interrupt(
                await graph.aget_state(config)
            )
            if confirmation_details is not None:
                await _set_job_async(
                    job_id,
                    {
                        "status": JobStatus.AWAITING_CONFIRMATION,
                        "confirmation": confirmation_details,
                        "tool_executions": list(final_state.get("tool_executions", [])),
                        **_actor_fields(current_user),
                        "request": (
                            original_request.model_dump(mode="json")
                            if original_request
                            else None
                        ),
                    },
                )
                return

            # Extract assistant content
            assistant_content = ""
            for msg in reversed(final_state["messages"]):
                if hasattr(msg, "type") and msg.type == "ai" and msg.content:
                    assistant_content = msg.content
                    break

            # Token cost on the HITL resume path (parity with the initial run).
            # Computed once here and reused for the assistant-row token_usage,
            # the usage counter below, and the response payload.
            in_tok, out_tok = _sum_message_usage(final_state.get("messages"))

            # Persist ONLY the assistant row for the resumed turn. The user row
            # that started this turn was already written up-front by the
            # original /execute run (_run_agent_graph -> _persist_user_message),
            # exactly like the SSE confirm path; re-persisting it here would
            # insert a second bare user row (no client_message_id -> no dedup)
            # and inflate thread.message_count.
            thread_id, conversation_id = "", ""
            try:
                if original_request and original_request.thread_id:
                    from uuid import UUID as _UUID

                    from src.models.thread import Thread as _Thread

                    thread_row = await db.get(
                        _Thread, _UUID(original_request.thread_id)
                    )
                    if thread_row is None:
                        # Preserve the old create_if_missing=False semantics: a
                        # re-lookup miss must skip persistence, not create a
                        # fresh thread that would split the conversation.
                        logger.warning(
                            "Confirm/resume persist skipped: thread %s not found "
                            "(user_id=%s)",
                            original_request.thread_id,
                            current_user.id,
                        )
                    else:
                        thread_id = str(thread_row.id)
                        conversation_id = str(thread_row.conversation_id)
                        tool_executions_out = [
                            ToolExecutionResponse(**te)
                            for te in final_state.get("tool_executions", [])
                        ] or None
                        # Checkpoint-anchored idempotency key — BYTE-IDENTICAL to
                        # streaming.py's _resume_assistant_cmid so a double-confirm
                        # across the SSE and job paths dedupes to the same row.
                        assistant_cmid = (
                            str(
                                _uuid.uuid5(
                                    _uuid.NAMESPACE_URL,
                                    f"nous-assistant-resume:{original_request.thread_id}:{resume_ckpt_id}",
                                )
                            )
                            if resume_ckpt_id
                            else None
                        )
                        persisted_assistant_id = await _persist_assistant_message_safe(
                            thread_id=thread_id,
                            content=assistant_content,
                            model_name=original_request.model,
                            tool_executions_out=tool_executions_out,
                            retrieved_contexts=final_state.get("retrieved_contexts"),
                            plan=final_state.get("plan") or None,
                            plan_reasoning=final_state.get("plan_reasoning") or None,
                            token_usage=(
                                {
                                    "input_tokens": in_tok,
                                    "output_tokens": out_tok,
                                }
                                if (in_tok or out_tok)
                                else None
                            ),
                            client_message_id=assistant_cmid,
                            required=True,
                        )
                        if persisted_assistant_id is None:
                            raise RuntimeError(
                                "Assistant message persistence returned no id"
                            )
            except Exception as e:
                logger.warning(
                    "Failed to persist confirmation thread messages", exc_info=e
                )
                raise

            response_model_name: str = (
                getattr(original_request, "model", "") if original_request else ""
            )
            if in_tok or out_tok:
                try:
                    from src.services.agent.observability import record_token_usage

                    record_token_usage(
                        response_model_name or "unknown", in_tok, out_tok
                    )
                except Exception:
                    logger.debug("record_token_usage failed", exc_info=True)
            result = AgentExecuteResponse(
                message=AgentMessage(role="assistant", content=assistant_content),
                model=response_model_name,
                usage={"input_tokens": in_tok, "output_tokens": out_tok},
                finish_reason="stop",
                timestamp=datetime.now(timezone.utc).isoformat(),
                tool_executions=[
                    ToolExecutionResponse(**te)
                    for te in final_state.get("tool_executions", [])
                ]
                or None,
                thread_id=thread_id,
                conversation_id=conversation_id,
            )

            await _set_job_async(
                job_id,
                {
                    "status": JobStatus.COMPLETED,
                    "result": result.model_dump(),
                    "tool_executions": list(final_state.get("tool_executions", [])),
                    **_actor_fields(current_user),
                },
            )
        except GraphInterrupt as exc:
            # A multi-step destructive flow can re-fire interrupt() during the
            # resume (user confirms tool #1, the agent then issues tool #2).
            # Without this handler the second interrupt bubbles into the generic
            # ``except Exception`` below and the job is wrongly marked "failed"
            # via client_safe_error, losing the second confirmation and breaking
            # HITL on the job/poll path. Mirror _run_agent_graph: re-park the job
            # as awaiting_confirmation. Uses original_request (the resume path's
            # request), not ``request``.
            confirmation_details = extract_interrupt_confirmation(exc)
            await _set_job_async(
                job_id,
                {
                    "status": JobStatus.AWAITING_CONFIRMATION,
                    "confirmation": confirmation_details,
                    # ainvoke raised before returning, so no final_state exists —
                    # match _run_agent_graph and reset the per-turn executions.
                    "tool_executions": [],
                    **_actor_fields(current_user),
                    "request": (
                        original_request.model_dump(mode="json")
                        if original_request
                        else None
                    ),
                },
            )
            return
        except asyncio.CancelledError:
            # See parallel handler in _run_agent_graph above — CancelledError
            # is a BaseException, so the ``except Exception`` below misses it.
            logger.warning("Agent graph resume cancelled", extra={"job_id": job_id})
            try:
                await _set_job_async(
                    job_id,
                    {
                        "status": JobStatus.CANCELLED,
                        "error": "resume cancelled",
                        **_actor_fields(current_user),
                    },
                )
            except Exception:
                logger.exception("Failed to mark cancelled resume job %s", job_id)
            raise
        except asyncio.TimeoutError:
            logger.error("Agent graph resume timed out", extra={"job_id": job_id})
            await _set_job_async(
                job_id,
                {
                    "status": JobStatus.FAILED,
                    "error": "Agent execution timed out after 360s",
                    **_actor_fields(current_user),
                },
            )
        except Exception as e:
            logger.error("Agent graph resume failed", exc_info=e)
            await _set_job_async(
                job_id,
                {
                    "status": JobStatus.FAILED,
                    "error": client_safe_error(e),
                    **_actor_fields(current_user),
                },
            )
