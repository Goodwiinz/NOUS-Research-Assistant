"""Redis-backed job store for agent execution jobs.

Replaces the process-local ``OrderedDict`` with a Redis-backed store so
jobs survive server restarts and can be shared across multiple workers.

The in-memory ``OrderedDict`` is kept as an L1 read cache (write-through)
so the hot path (polling) avoids a Redis round-trip.

Every status write is additionally projected into the durable ``agent_runs``
Postgres table. Non-terminal writes remain fire-and-forget. Producer terminal
writes wait for the database's effective decision before publishing to L1 or
Redis; a failed decision never exposes an uncommitted terminal result.
"""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any, Literal, Optional
from uuid import uuid4

from src.core.config import get_settings
from src.shared.enums import JobStatus

if TYPE_CHECKING:
    # Only for the `_redis` annotation below — the runtime import is deferred
    # inside `_get_redis()` (see its docstring) so `redis.asyncio`'s module-level
    # setup can't interfere with other async libraries during app startup.
    import redis.asyncio as aioredis

    from src.services.agent.agent_run_service import RunStatusDecision

logger = logging.getLogger(__name__)


class JobStatusPublicationError(RuntimeError):
    """A requested job state could not be authorized by durable storage."""


# ---------------------------------------------------------------------------
# Key naming
# ---------------------------------------------------------------------------
_JOB_KEY_PREFIX = "agent:job:"
_JOB_TTL_SECONDS = 3600  # 1 hour — matches the old in-memory expiry

# ---------------------------------------------------------------------------
# In-memory L1 cache (write-through)
# ---------------------------------------------------------------------------
_l1: OrderedDict[str, dict] = OrderedDict()
_l1_lock = Lock()
_L1_MAX_ENTRIES = 500


@dataclass(frozen=True)
class _PendingTerminalPublication:
    """Process-local gate for a terminal L1 value awaiting Redis resolution."""

    reservation: dict
    completed: asyncio.Event
    loop: asyncio.AbstractEventLoop


@dataclass(frozen=True)
class _RedisWriteOutcome:
    """Result of one atomic Redis publication attempt."""

    state: Literal["committed", "refused", "unavailable"]
    payload: Optional[dict]


_pending_terminal_publications: dict[str, _PendingTerminalPublication] = {}

_LAST_L1_CLEANUP: float = 0.0
_L1_CLEANUP_INTERVAL = 60.0

# Per-process monotonic counter — a tiebreaker for set_job calls landing in the
# same ``time.time()`` tick, so two updates in one tick cannot overwrite out of
# order (created_at alone can't disambiguate them).
_seq: int = 0


class ConfirmationCoordinationUnavailable(RuntimeError):
    """Raised when a shared deployment cannot claim a confirmation safely."""


def process_local_confirmation_coordination_allowed() -> bool:
    """Whether this process may use an in-memory confirmation claim."""
    return get_settings().is_throwaway_environment


def _is_newer_or_equal(data: dict, existing: dict) -> bool:
    """True if *data* should overwrite *existing* in the L1 cache.

    Orders by ``(created_at, _seq)``: ``created_at`` is the cross-process clock,
    ``_seq`` the per-process tiebreaker. Missing keys default to 0 for backward
    compatibility with pre-``_seq`` values already stored in Redis.
    """
    return (data.get("created_at", 0), data.get("_seq", 0)) >= (
        existing.get("created_at", 0),
        existing.get("_seq", 0),
    )


def _status_value(data: dict) -> Optional[JobStatus]:
    try:
        return JobStatus(data.get("status"))
    except (TypeError, ValueError):
        return None


def _cache_transition_allowed(
    candidate: dict,
    existing: Optional[dict],
    *,
    authorized_status: Optional[JobStatus] = None,
    allow_terminal_correction: bool = False,
) -> bool:
    """Guard cache writes against terminal regression and stale freshness."""
    candidate_status = _status_value(candidate)
    if candidate_status is None:
        if existing is None:
            return True
        existing_status = _status_value(existing)
        if existing_status is None:
            return _is_newer_or_equal(candidate, existing)
        if existing_status.is_terminal or existing_status == JobStatus.STOPPING:
            return False
        return _is_newer_or_equal(candidate, existing)
    if candidate_status.is_terminal and authorized_status != candidate_status:
        return False
    if existing is None:
        return True

    existing_status = _status_value(existing)
    if existing_status is None:
        return _is_newer_or_equal(candidate, existing)
    if existing_status.is_terminal:
        return (
            candidate_status == existing_status
            and _is_newer_or_equal(candidate, existing)
        ) or (allow_terminal_correction and candidate_status.is_terminal)
    if existing_status == JobStatus.STOPPING:
        if candidate_status.is_terminal:
            return authorized_status == candidate_status
        return candidate_status == JobStatus.STOPPING and _is_newer_or_equal(
            candidate, existing
        )
    if candidate_status == JobStatus.STOPPING:
        return authorized_status == JobStatus.STOPPING
    return _is_newer_or_equal(candidate, existing)


def _scope_compatible(candidate: dict, existing: dict) -> bool:
    for key in ("user_id", "organization_id", "thread_id"):
        left, right = candidate.get(key), existing.get(key)
        if left is not None and right is not None and str(left) != str(right):
            return False
    return True


def _preserve_terminal_payload(
    candidate: dict, existing: Optional[dict], *, scope_compatible: bool = True
) -> None:
    """Keep terminal winners' result fields and strip non-success losers."""
    status = _status_value(candidate)
    if status in {JobStatus.STOPPING, JobStatus.CANCELLED, JobStatus.FAILED}:
        candidate.pop("result", None)
        candidate.pop("confirmation", None)
        return
    if (
        status == JobStatus.COMPLETED
        and existing is not None
        and scope_compatible
        and _status_value(existing) == JobStatus.COMPLETED
    ):
        if "result" in existing:
            candidate["result"] = existing["result"]
        if "confirmation" in existing:
            candidate.pop("confirmation", None)


def _signal_pending_publication(publication: _PendingTerminalPublication) -> None:
    """Wake gate waiters safely even when a synchronous writer owns the lock."""
    try:
        publication.loop.call_soon_threadsafe(publication.completed.set)
    except RuntimeError:
        # The owning event loop has already closed during shutdown.
        pass


def _retire_pending_publication(
    job_id: str,
    publication: Optional[_PendingTerminalPublication] = None,
) -> None:
    """Retire and signal a job gate; caller must hold ``_l1_lock``."""
    current = _pending_terminal_publications.get(job_id)
    if current is not None and (publication is None or current is publication):
        del _pending_terminal_publications[job_id]
        _signal_pending_publication(current)


def _l1_cleanup() -> None:
    """Remove expired entries (>1h) and evict oldest when over max.

    Must be called while holding ``_l1_lock``.
    """
    global _LAST_L1_CLEANUP
    now = time.time()
    expired = [
        k for k, v in _l1.items() if now - v.get("created_at", now) > _JOB_TTL_SECONDS
    ]
    for k in expired:
        del _l1[k]
        _retire_pending_publication(k)
    while len(_l1) > _L1_MAX_ENTRIES:
        evicted, _ = _l1.popitem(last=False)
        _retire_pending_publication(evicted)
    _LAST_L1_CLEANUP = now


def _l1_maybe_cleanup() -> None:
    """Throttled L1 cleanup called from read paths.

    Must be called while holding ``_l1_lock``.
    """
    if time.time() - _LAST_L1_CLEANUP >= _L1_CLEANUP_INTERVAL:
        _l1_cleanup()


# ---------------------------------------------------------------------------
# Redis client (lazy singleton)
# ---------------------------------------------------------------------------
_redis: Optional[aioredis.Redis] = None


async def _get_redis() -> Optional[Any]:  # noqa: ANN401
    """Return the shared Redis client, creating it on first call.

    Imports ``redis.asyncio`` lazily so its module-level setup
    (event-loop hooks, connection-factory registration) cannot
    interfere with other async libraries during application startup.
    """
    import redis.asyncio as aioredis

    global _redis
    if _redis is not None:
        return _redis
    settings = get_settings()
    if not settings.REDIS_URL:
        logger.warning("REDIS_URL not configured — job store using in-memory only")
        return None
    client: Optional[Any] = None
    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await client.ping()
    except Exception:
        logger.exception("Failed to connect to Redis for job store")
        # `from_url` allocates a connection pool synchronously; a failed ping
        # leaves it open with nothing left to close it if we just drop the
        # reference — one leaked pool per retry cycle for the life of a Redis
        # outage. Best-effort close: this path already logged and is about to
        # degrade to in-memory-only, so a second failure here is not fatal.
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()
        _redis = None
        return _redis
    _redis = client
    return _redis


async def get_redis() -> Optional[Any]:  # noqa: ANN401
    """Public alias for the shared lazy Redis client (used by stream_buffer)."""
    return await _get_redis()


async def close_redis() -> None:
    """Close the Redis connection (called during shutdown)."""
    global _redis
    if _redis is not None:
        await _redis.close()
        _redis = None


# ---------------------------------------------------------------------------
# Durable projection (agent_runs) — fire-and-forget write-through
# ---------------------------------------------------------------------------

# Strong references to in-flight projection tasks so the event loop cannot
# garbage-collect them mid-write (same pattern as jobs._background_tasks).
_projection_tasks: set = set()


def _projection_payload(data: dict, fallback: Optional[dict] = None) -> dict:
    """Snapshot the fields projected into ``agent_runs``."""
    request = data.get("request")
    result = data.get("result")
    fallback = fallback or {}
    fallback_request = fallback.get("request")
    fallback_result = fallback.get("result")
    return {
        "status": data.get("status"),
        "user_id": data.get("user_id"),
        "organization_id": data.get("organization_id"),
        "error": data.get("error"),
        "thread_id": (
            data.get("thread_id")
            or (request.get("thread_id") if isinstance(request, dict) else None)
            or (result.get("thread_id") if isinstance(result, dict) else None)
            or fallback.get("thread_id")
            or (
                fallback_request.get("thread_id")
                if isinstance(fallback_request, dict)
                else None
            )
            or (
                fallback_result.get("thread_id")
                if isinstance(fallback_result, dict)
                else None
            )
        ),
    }


def _projection_enabled() -> bool:
    """Whether to schedule the background agent_runs projection at all.

    Disabled under ``ENVIRONMENT=testing``: pytest closes each test's event
    loop as soon as the test returns, so a fire-and-forget task scheduled
    here outlives its loop. With the CI sqlite database that leaves an
    aiosqlite ``_connection_worker_thread`` (non-daemon) blocked on a future
    whose loop is already closed — the worker process never exits and the
    unit-test job hangs. Projection *scheduling* is pinned by tests that
    monkeypatch this to True (with ``record_job_status`` mocked); the
    projection body is covered against an explicit session in
    test_agent_run_service.py.
    """
    try:
        return get_settings().ENVIRONMENT != "testing"
    except Exception:  # pragma: no cover — settings must never break writes
        return True


def _on_projection_done(task) -> None:
    """Drop a finished projection task and surface unexpected errors."""
    _projection_tasks.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        # record_job_status already swallows its own failures; this catches
        # anything raised before it ran (e.g. import errors).
        logger.exception("agent_runs projection task failed")


def schedule_run_projection(job_id: str, data: dict) -> None:
    """Fire-and-forget the ``agent_runs`` Postgres projection for a write.

    Snapshots the few projected fields synchronously so a later mutation of
    *data* (the same dict the L1 cache holds) cannot race the background
    write. Never blocks and never raises: Redis stays authoritative — a
    skipped/failed projection only narrows the failover fallback.

    No-op under ``ENVIRONMENT=testing`` (see ``_projection_enabled``) so no
    background task can outlive a test's event loop.
    """
    if not _projection_enabled():
        return
    status = data.get("status")
    if not status:
        return
    payload = _projection_payload(data)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop — sync caller outside async context; best-effort skip

    try:
        from src.services.agent.agent_run_service import record_job_status

        task = loop.create_task(record_job_status(job_id, payload))
        _projection_tasks.add(task)
        task.add_done_callback(_on_projection_done)
    except Exception:
        # The projection is strictly best-effort — scheduling problems must
        # never break the authoritative Redis/L1 write path.
        logger.warning(
            "Failed to schedule agent_runs projection for job %s",
            job_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Public API (replaces the old _set_job / _get_job)
# ---------------------------------------------------------------------------


_REDIS_WRITE_MAX_ATTEMPTS = 5


async def _redis_write_if_newer(
    redis_client: Any,
    job_id: str,
    data: dict,
    *,
    authorized_status: Optional[JobStatus] = None,
    allow_terminal_correction: bool = False,
) -> _RedisWriteOutcome:
    """Atomically guard Redis publication while preserving Python JSON shape.

    WATCH/MULTI keeps the status/freshness decision and write atomic. JSON is
    decoded and merged in Python because Redis cjson encodes empty arrays as
    objects, which changes the public job payload shape.
    """
    key = f"{_JOB_KEY_PREFIX}{job_id}"
    from redis.exceptions import WatchError

    try:
        candidate_json = _json.dumps(data, default=str)
        async with redis_client.pipeline(transaction=True) as pipe:
            for attempt in range(_REDIS_WRITE_MAX_ATTEMPTS):
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    existing = _json.loads(raw) if raw is not None else None
                    candidate = _json.loads(candidate_json)
                    candidate_status = _status_value(candidate)
                    if (
                        candidate_status == JobStatus.STOPPING
                        and authorized_status != JobStatus.STOPPING
                    ) or not _cache_transition_allowed(
                        candidate,
                        existing,
                        authorized_status=authorized_status,
                        allow_terminal_correction=allow_terminal_correction,
                    ):
                        await pipe.reset()
                        return _RedisWriteOutcome("refused", existing)

                    scope_compatible = existing is None or _scope_compatible(
                        candidate, existing
                    )
                    _preserve_terminal_payload(
                        candidate, existing, scope_compatible=scope_compatible
                    )
                    if existing is not None and scope_compatible:
                        for field in ("user_id", "organization_id", "thread_id"):
                            if (
                                candidate.get(field) is None
                                and existing.get(field) is not None
                            ):
                                candidate[field] = existing[field]

                    pipe.multi()
                    pipe.setex(
                        key,
                        _JOB_TTL_SECONDS,
                        _json.dumps(candidate, default=str),
                    )
                    await pipe.execute()
                    return _RedisWriteOutcome("committed", candidate)
                except WatchError:
                    await pipe.reset()
                    if attempt + 1 == _REDIS_WRITE_MAX_ATTEMPTS:
                        logger.warning(
                            "Redis job publication retries exhausted for job %s",
                            job_id,
                        )
                        return _RedisWriteOutcome("unavailable", None)
    except Exception:
        logger.exception("Failed to write job %s to Redis", job_id)
        return _RedisWriteOutcome("unavailable", None)
    return _RedisWriteOutcome("unavailable", None)


async def _write_to_redis_only(job_id: str, data: dict) -> None:
    """Write *data* to Redis without touching the L1 cache.

    Used by the sync ``_set_job`` wrapper whose caller has already
    populated L1 directly.  Avoids a race where the fire-and-forget
    background task overwrites a later L1 update from the main flow.
    """
    redis_client = await _get_redis()
    if redis_client is not None:
        await _redis_write_if_newer(redis_client, job_id, data)


def _decision_status(decision: Any) -> Optional[JobStatus]:
    try:
        return JobStatus(decision.effective_status)
    except (AttributeError, TypeError, ValueError):
        return None


def _decision_authorizes(
    job_id: str, data: dict, decision: Optional[RunStatusDecision]
) -> Optional[JobStatus]:
    if decision is None or getattr(decision, "job_id", None) != job_id:
        return None
    status = _decision_status(decision)
    if status is None or _status_value(data) != status:
        return None
    for field in ("user_id", "organization_id", "thread_id"):
        decided = getattr(decision, field, None)
        supplied = data.get(field)
        if (
            decided is not None
            and supplied is not None
            and str(supplied) != str(decided)
        ):
            return None
    return status


def _apply_durable_decision(
    job_id: str,
    data: dict,
    decision: Optional[RunStatusDecision],
    requested_status: Optional[JobStatus],
) -> tuple[JobStatus, bool]:
    authorized_status = _decision_status(decision) if decision is not None else None
    if (
        decision is None
        or authorized_status is None
        or getattr(decision, "job_id", None) != job_id
    ):
        raise JobStatusPublicationError(
            f"Durable status decision is missing or mismatched for job {job_id}"
        )
    for field in ("user_id", "organization_id", "thread_id"):
        value = getattr(decision, field, None)
        supplied = data.get(field)
        if value is not None and supplied is not None and str(supplied) != str(value):
            raise JobStatusPublicationError(
                f"Durable status scope mismatched for job {job_id}"
            )
        if value is None:
            data.pop(field, None)
        else:
            data[field] = value
    data["status"] = authorized_status
    error = getattr(decision, "error", None)
    if error is None:
        data.pop("error", None)
    else:
        data["error"] = error

    if requested_status is not None and requested_status != authorized_status:
        data.pop("result", None)
        data.pop("confirmation", None)
    _preserve_terminal_payload(data, None)
    return authorized_status, authorized_status.is_terminal


async def set_job(
    job_id: str,
    data: dict,
    *,
    project: bool = True,
    require_durable_decision: bool = False,
    decision: Optional[RunStatusDecision] = None,
) -> Optional[RunStatusDecision]:
    """Persist a job to Redis (L2) + in-memory L1 cache (write-through).

    Every terminal request and explicitly guarded producer-exit status waits
    for the durable row's effective decision before it may touch either cache.
    ``project=False`` skips a duplicate database projection; a terminal mirror
    still requires the immutable decision produced by the committed write.
    """
    global _seq

    # Carry ownership forward and capture the prior request/result before the
    # replacement write. Terminal updates often contain only status + actor.
    with _l1_lock:
        existing = _l1.get(job_id)
        # set_job REPLACES the record. Carry the owner forward when a status
        # update omits it — the GET ownership check fails closed on a missing
        # user_id, so dropping it would lock the owner out of their own job.
        # Scope: L1 only. If the entry was evicted from L1 but still lives in
        # Redis, an ownerless write is NOT enriched — acceptable because every
        # writer stamps user_id explicitly; this is a same-process backstop.
        if "user_id" not in data and existing is not None and existing.get("user_id"):
            data["user_id"] = existing["user_id"]
        # Same carry-forward for tenancy: replacement writes typically omit
        # organization_id; keep it so the agent_runs projection stays scoped.
        if (
            "organization_id" not in data
            and existing is not None
            and existing.get("organization_id")
        ):
            data["organization_id"] = existing["organization_id"]

    requested_status = _status_value(data)
    strict_projection = require_durable_decision or bool(
        requested_status is not None and requested_status.is_terminal
    )
    durable_decision = decision
    if strict_projection and project:
        from src.services.agent.agent_run_service import record_job_status

        try:
            durable_decision = await record_job_status(
                job_id,
                _projection_payload(data, existing),
                raise_on_error=True,
            )
        except Exception as exc:
            raise JobStatusPublicationError(
                f"Could not decide status for job {job_id}: {exc}"
            ) from exc
        if durable_decision is None:
            raise JobStatusPublicationError(
                f"Durable status projection returned no decision for job {job_id}"
            )
    elif strict_projection and durable_decision is None:
        raise JobStatusPublicationError(
            f"A committed status decision is required to mirror job {job_id}"
        )

    authorized_status: Optional[JobStatus] = None
    allow_terminal_correction = False
    projected_synchronously = False
    if strict_projection:
        authorized_status, allow_terminal_correction = _apply_durable_decision(
            job_id, data, durable_decision, requested_status
        )
        projected_synchronously = project

    data["created_at"] = time.time()
    pending_publication: Optional[_PendingTerminalPublication] = None
    with _l1_lock:
        _seq += 1
        data["_seq"] = _seq
        existing = _l1.get(job_id)
        scope_compatible = existing is None or _scope_compatible(data, existing)
        if _cache_transition_allowed(
            data,
            existing,
            authorized_status=authorized_status,
            allow_terminal_correction=allow_terminal_correction,
        ):
            _preserve_terminal_payload(
                data, existing, scope_compatible=scope_compatible
            )
            _l1[job_id] = data
        _l1_maybe_cleanup()
        if (
            authorized_status is not None
            and authorized_status.is_terminal
            and _l1.get(job_id) is data
        ):
            pending_publication = _PendingTerminalPublication(
                reservation=data,
                completed=asyncio.Event(),
                loop=asyncio.get_running_loop(),
            )
            previous = _pending_terminal_publications.get(job_id)
            _pending_terminal_publications[job_id] = pending_publication
            if previous is not None:
                # Old waiters must recheck and attach to the newer generation.
                _signal_pending_publication(previous)

    if project and not projected_synchronously:
        schedule_run_projection(job_id, data)

    try:
        try:
            outcome = await set_job_redis_only(job_id, data, decision=durable_decision)
        except Exception:
            logger.exception("Redis job publication failed for job %s", job_id)
            outcome = None
        if pending_publication is not None and isinstance(outcome, _RedisWriteOutcome):
            with _l1_lock:
                if (
                    _pending_terminal_publications.get(job_id) is pending_publication
                    and _l1.get(job_id) is pending_publication.reservation
                    and outcome.state in {"committed", "refused"}
                    and outcome.payload is not None
                    and _status_value(outcome.payload) == authorized_status
                    and _scope_compatible(data, outcome.payload)
                ):
                    resolved = dict(outcome.payload)
                    for field in ("user_id", "organization_id", "thread_id"):
                        if resolved.get(field) is None and data.get(field) is not None:
                            resolved[field] = data[field]
                    _preserve_terminal_payload(resolved, None)
                    _l1[job_id] = resolved
    finally:
        if pending_publication is not None:
            with _l1_lock:
                _retire_pending_publication(job_id, pending_publication)
            # Signal this generation even if a newer publisher replaced it.
            _signal_pending_publication(pending_publication)
    return durable_decision


async def set_job_redis_only(
    job_id: str,
    data: dict,
    *,
    decision: Optional[RunStatusDecision] = None,
) -> _RedisWriteOutcome:
    """Atomically write an authorized payload to Redis without touching L1."""
    redis_client = await _get_redis()
    if redis_client is None:
        return _RedisWriteOutcome("unavailable", None)
    authorized_status = _decision_authorizes(job_id, data, decision)
    return await _redis_write_if_newer(
        redis_client,
        job_id,
        data,
        authorized_status=authorized_status,
        allow_terminal_correction=(
            authorized_status is not None and authorized_status.is_terminal
        ),
    )


async def _cas_in_memory(
    job_id: str,
    expected: JobStatus | str,
    new_status: JobStatus | str,
    *,
    project: bool = True,
) -> str:
    """Compare-and-set the status using the store API (L1 + write-through).

    Used when Redis is unavailable (single process ⇒ the L1 lock is sufficient)
    AND as the graceful fallback when a Redis operational error interrupts the
    WATCH/MULTI path — so a transient Redis hiccup degrades to single-process
    behavior (still flips + lets the caller proceed) rather than silently
    dropping the transition.
    """
    job = await get_job(job_id)
    if job is None:
        return "missing"
    with _l1_lock:
        current = _l1.get(job_id) or job
        if current.get("status") != expected:
            return "conflict"
        # Copy before mutating: `current` may be the same object other readers
        # already hold, and re-assigning an existing OrderedDict key does not
        # refresh LRU recency (audit S-L11).
        claimed = dict(current)
        claimed["status"] = new_status
        _l1[job_id] = claimed
        _l1.move_to_end(job_id)
    await set_job(job_id, claimed, project=project)
    return "claimed"


async def compare_and_set_status(
    job_id: str,
    expected: JobStatus | str,
    new_status: JobStatus | str,
    *,
    project: bool = True,
) -> str:
    """Atomically flip a job's status from *expected* to *new_status*.

    Returns one of: ``"claimed"`` (transition applied — caller is the winner),
    ``"conflict"`` (job exists but status != expected — a concurrent caller
    already transitioned it, or it is denied/completed), ``"missing"`` (no such
    job).

    This closes the multi-worker double-resume race on /confirm: two workers
    racing to confirm the same HITL job both read ``awaiting_confirmation``, but
    only one wins this compare-and-set, so only one schedules a resume — without
    it a destructive HITL tool (ingest/create_note/create_draft) could execute
    twice. Uses a Redis WATCH/MULTI optimistic transaction (JSON parsed in
    Python to avoid cjson's empty-dict ambiguity). Disposable local/CI processes
    may fall back to memory; shared deployments fail closed without Redis.

    ``project=False`` mirrors a durable transition that the caller already
    committed synchronously; it prevents a redundant fire-and-forget write
    from racing a later durable state.
    """
    redis_client = await _get_redis()
    if redis_client is None:
        if not process_local_confirmation_coordination_allowed():
            raise ConfirmationCoordinationUnavailable(
                "Distributed confirmation coordination is unavailable"
            )
        return await _cas_in_memory(job_id, expected, new_status, project=project)

    key = f"{_JOB_KEY_PREFIX}{job_id}"
    from redis.exceptions import RedisError, WatchError

    claim_id = uuid4().hex
    try:
        async with redis_client.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)  # immediate mode after WATCH
                    if raw is None:
                        await pipe.reset()
                        return "missing"
                    job = _json.loads(raw)
                    if job.get("status") != expected:
                        await pipe.reset()
                        return "conflict"
                    job["status"] = new_status
                    job["_confirmation_claim_id"] = claim_id
                    ttl = await pipe.ttl(key)
                    pipe.multi()
                    pipe.setex(
                        key,
                        ttl if (ttl and ttl > 0) else _JOB_TTL_SECONDS,
                        _json.dumps(job, default=str),
                    )
                    await pipe.execute()  # raises WatchError if key changed
                    break
                except WatchError:
                    # Reset clears the WATCH/command state before re-watching —
                    # required so the retry's watch() starts from a clean slate
                    # and the connection isn't left bound.
                    await pipe.reset()
                    continue
    except (RedisError, OSError, asyncio.TimeoutError):
        # Operational Redis/connection error (not a logical conflict). Only a
        # disposable single-process environment can safely fall back to memory;
        # shared deployments fail closed so two pods cannot both resume.
        logger.warning(
            "compare_and_set_status Redis path failed for job %s; "
            "checking whether an in-memory fallback is safe",
            job_id,
            exc_info=True,
        )
        if not process_local_confirmation_coordination_allowed():
            try:
                committed_raw = await redis_client.get(key)
                committed = _json.loads(committed_raw) if committed_raw else None
            except (RedisError, OSError, asyncio.TimeoutError, ValueError):
                committed = None
            if not (
                committed
                and committed.get("status") == new_status
                and committed.get("_confirmation_claim_id") == claim_id
            ):
                raise ConfirmationCoordinationUnavailable(
                    "Distributed confirmation coordination is unavailable"
                )
            job = committed
        else:
            return await _cas_in_memory(job_id, expected, new_status, project=project)

    # Mirror the winning transition into L1 so this worker's polls are
    # consistent. Copy-on-write + move_to_end, same as _cas_in_memory (audit
    # S-L11): `cached` may be the object other readers already hold, and
    # re-assigning an existing OrderedDict key does not refresh LRU recency.
    with _l1_lock:
        cached = _l1.get(job_id)
        if cached is not None:
            updated = dict(cached)
            updated["status"] = new_status
            _l1[job_id] = updated
            _l1.move_to_end(job_id)
    # Project the claimed transition (the in-memory fallback path projects via
    # set_job inside _cas_in_memory; this covers the Redis WATCH/MULTI path).
    if project:
        schedule_run_projection(job_id, job)
    return "claimed"


async def get_job(job_id: str) -> Optional[dict]:
    """Retrieve a job: Redis-first (cross-worker truth), L1 as fallback.

    Redis is authoritative across workers — an L1-first read let worker A
    serve a stale local entry after worker B advanced the job in Redis
    (codex audit on #1405: the resume and worker-failure paths acted on such
    reads). The monotonic ``_seq`` guard still protects the one case where
    the LOCAL copy is fresher (this worker's write-through beat its own
    fire-and-forget Redis write): the newer of the two wins. L1 serves the
    answer only when Redis is unavailable or has no key.
    """
    # L1 lookup (expiry-checked); used for the freshness compare and as the
    # Redis-down fallback — never returned early over a live Redis read.
    cached: Optional[dict] = None
    with _l1_lock:
        _l1_maybe_cleanup()
        entry = _l1.get(job_id)
        if entry is not None:
            if time.time() - entry.get("created_at", 0) > _JOB_TTL_SECONDS:
                del _l1[job_id]
            else:
                cached = entry

    redis_client = await _get_redis()
    if redis_client is not None:
        try:
            raw = await redis_client.get(f"{_JOB_KEY_PREFIX}{job_id}")
            if raw is not None:
                data = _json.loads(raw)
                with _l1_lock:
                    existing = _l1.get(job_id)
                    if existing is None or _is_newer_or_equal(data, existing):
                        _l1[job_id] = data
                        return data
                    # Local write-through is newer than the Redis read
                    # (its async projection hasn't landed yet).
                    return existing
        except Exception:
            logger.exception("Failed to read job %s from Redis", job_id)

    return cached


async def get_job_fresh(job_id: str) -> Optional[dict]:
    """Redis-first job read for cross-process freshness; L1 fallback.

    ``get_job`` prefers the process-local L1 cache, which is only coherent
    with writes made by THIS process. When the run's writer is a different
    process — Celery dispatch mode, or a confirm/poll landing on a different
    API replica — a previously-seeded L1 entry goes permanently stale (a
    ``running`` record would 409 every confirm and spin the poller for the
    full 1h TTL). Poll/confirm reads therefore consult Redis first and fold
    the fresh copy back into L1 under the monotonic guard (so an in-flight
    newer local write is never clobbered). Degrades to the plain L1 read when
    Redis is unavailable — identical to today's single-process behavior.
    """
    redis_client = await _get_redis()
    if redis_client is not None:
        try:
            raw = await redis_client.get(f"{_JOB_KEY_PREFIX}{job_id}")
            if raw is not None:
                data = _json.loads(raw)
                with _l1_lock:
                    existing = _l1.get(job_id)
                    if existing is None or _is_newer_or_equal(data, existing):
                        _l1[job_id] = data
                        return data
                    # L1 holds a strictly newer local write (fire-and-forget
                    # Redis write still in flight) — prefer it.
                    return existing
            # Redis miss (TTL/failover): fall through to L1 so a record that
            # only ever lived locally (Redis down at write time) still reads.
        except Exception:
            logger.exception("Failed to read job %s from Redis", job_id)
    with _l1_lock:
        _l1_maybe_cleanup()
        cached = _l1.get(job_id)
        if cached is None:
            return None
        if time.time() - cached.get("created_at", 0) > _JOB_TTL_SECONDS:
            del _l1[job_id]
            _retire_pending_publication(job_id)
            return None
        return cached


async def get_job_for_poll(job_id: str) -> Optional[dict]:
    """Return an L1 snapshot only after its terminal publication is resolved.

    Polling uses this at each terminal-selection boundary, including after an
    awaited Redis refresh. A newer local generation replaces the gate, and an
    evicted/replaced reservation retires its own stale gate. The final gate
    check and snapshot copy happen under one lock with no intervening await.
    """
    while True:
        pending: Optional[_PendingTerminalPublication] = None
        with _l1_lock:
            _l1_maybe_cleanup()
            pending = _pending_terminal_publications.get(job_id)
            if pending is not None and _l1.get(job_id) is not pending.reservation:
                _retire_pending_publication(job_id, pending)
                pending = None
            if pending is None:
                cached = _l1.get(job_id)
                if cached is None:
                    return None
                if time.time() - cached.get("created_at", 0) > _JOB_TTL_SECONDS:
                    del _l1[job_id]
                    _retire_pending_publication(job_id)
                    return None
                return dict(cached)
        await asyncio.shield(pending.completed.wait())


async def delete_job(job_id: str) -> None:
    """Remove a job from both L1 and Redis."""
    with _l1_lock:
        _l1.pop(job_id, None)
        _retire_pending_publication(job_id)

    redis_client = await _get_redis()
    if redis_client is not None:
        try:
            await redis_client.delete(f"{_JOB_KEY_PREFIX}{job_id}")
        except Exception:
            logger.exception("Failed to delete job %s from Redis", job_id)
