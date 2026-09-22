"""Celery execution backend for agent /execute turns + agent-run sweeper.

Audit P1.3 + P1.4 (findings X1 dispatch/recovery halves, D7 stuck-job half).

Dispatch (flag-gated by ``AGENT_DISPATCH_BACKEND=celery``, default
``background``): the API commits the ``agent_runs`` row (+ idempotency key)
and writes the Redis job record BEFORE publishing (flush-before-external),
then ``run_agent_job`` claims the row's one-shot execution lease and drives
``agent_execution_service._run_agent_graph`` — the exact coroutine the
FastAPI BackgroundTasks path runs, so both backends share one runner and one
status protocol.

Idempotency: ``agent_run_service.claim_execution`` succeeds at most once per
run (atomic ``queued`` → ``running`` plus ``lease_owner IS NULL``; an unleased
legacy ``running`` row is accepted during rollout), so a duplicate delivery
(acks_late redelivery, broker retry, double publish) no-ops instead of
double-running a turn whose tools may have side effects. A redelivery after a
crash *before* the claim still finds the lease free and legitimately recovers
the turn.

HITL: confirm/resume stays on the API pod BY DESIGN in both modes. The resume
re-enters the graph through the Postgres checkpointer (keyed by thread_id),
which every pod reaches; the Redis CAS on the job record already dedupes
concurrent confirms. Keeping it request-local means the confirming user gets
the same latency in both modes and this task never needs to carry resume
state.

Event-loop boundary: the agent stack caches loop-bound singletons (the
LangGraph checkpointer's AsyncConnectionPool, the job store's Redis client,
the SQLAlchemy async engine). A bare ``asyncio.run`` per task would strand
them on a dead loop after the first task, so every coroutine here runs on the
shared ``_async_utils.run_async`` boundary: ONE persistent background event
loop per worker process (``run_coroutine_threadsafe`` on a daemon thread),
created lazily and fork-safe via a PID guard. The same loop is reused across
tasks, so the loop-bound singletons survive between turns. Children are
recycled every 100 tasks (``worker_max_tasks_per_child``); a recycled child
lazily recreates its own loop on first use. This module previously hosted its
own bespoke ``_get_worker_loop``/``_run_coro`` pair; it was consolidated onto
``run_async`` (its superset) so there is exactly one loop + one boundary
implementation across all Celery tasks.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from celery.signals import worker_process_shutdown

from src.core.config import get_settings
from src.shared.enums import JobStatus
from src.tasks._async_utils import run_async
from src.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# The graph run inside _run_agent_graph is bounded by asyncio.timeout(360).
# Give the outer future room for user-load + persistence so the inner timeout
# always fires first (it is the path that marks the job failed properly).
_RUN_FUTURE_TIMEOUT_SECONDS = 420

_SWEEP_FUTURE_TIMEOUT_SECONDS = 240


@worker_process_shutdown.connect
def _close_typesafe_router(**kwargs: Any) -> None:
    """Close the worker's pooled client on the same persistent event loop."""
    from src.services.agent.typesafe_classifier import close_typesafe_client

    if get_settings().AGENT_INTENT_PROVIDER == "typesafe":
        run_async(close_typesafe_client(), timeout=5)


# ---------------------------------------------------------------------------
# Sync→async boundary
# ---------------------------------------------------------------------------
#
# Every coroutine below is driven from sync Celery code through the shared
# ``run_async`` helper (imported above), which hosts ONE persistent, fork-safe
# event loop per worker process (see the module docstring). This module used to
# carry its own ``_get_worker_loop``/``_run_coro`` pair; it now routes through
# ``run_async`` so there is a single loop and a single boundary implementation
# across all Celery tasks. ``run_async`` is a strict superset of the old pair
# (same persistent-loop + ``run_coroutine_threadsafe`` + timeout-cancel
# semantics, plus a fork-safe PID guard), so the loop-reuse contract the agent
# stack relies on — loop-bound checkpointer/Redis/engine singletons shared
# across turns — is preserved.


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a DB datetime for comparison (sqlite may return naive UTC)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _coerce_status(value: Any) -> Optional[JobStatus]:
    try:
        return JobStatus(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Job-store helpers (worker side)
# ---------------------------------------------------------------------------


async def _fail_job_record(
    job_id: str,
    error: str,
    *,
    user_id: Optional[str] = None,
    organization_id: Optional[str] = None,
) -> None:
    """Mark the job failed in the live store AND the durable projection.

    Merges into the existing Redis record when one exists (preserving
    ``user_id``/``request`` so the poll ownership check keeps working);
    otherwise writes a minimal record with whatever actor fields we have.
    Never raises — this is the last-resort path that stops a poller from
    spinning on a run that will never execute.
    """
    from src.services.agent import agent_run_service, job_store

    payload: dict = {}
    try:
        existing = await job_store.get_job(job_id)
        if existing:
            payload = dict(existing)
    except Exception:
        logger.warning("Failed to read job %s before fail-write", job_id, exc_info=True)
    payload.update(
        {
            "status": JobStatus.FAILED,
            "error": error,
            "tool_executions": payload.get("tool_executions", []),
        }
    )
    if user_id and not payload.get("user_id"):
        payload["user_id"] = str(user_id)
    if organization_id and not payload.get("organization_id"):
        payload["organization_id"] = str(organization_id)
    try:
        await job_store.set_job(job_id, payload)
    except Exception:
        logger.warning("Failed to write failed job %s to store", job_id, exc_info=True)
    # Direct, awaited projection write (record_job_status never raises).
    await agent_run_service.record_job_status(job_id, payload)


async def _mark_job_running(
    job_id: str,
    request_payload: dict,
    user: Any,
) -> None:
    """Expose RUNNING to live pollers after the durable worker claim wins."""
    from src.services.agent import job_store

    try:
        await job_store.set_job(
            job_id,
            {
                "status": JobStatus.RUNNING,
                "tool_executions": [],
                "user_id": str(user.id),
                "organization_id": (
                    str(user.organization_id)
                    if getattr(user, "organization_id", None)
                    else None
                ),
                "request": request_payload,
            },
            project=False,
        )
    except Exception:
        # The durable claim is authoritative and the graph can still run; the
        # poll path falls back to agent_runs if the live-store write is lost.
        logger.warning(
            "run_agent_job: failed to expose running status for job %s",
            job_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# run_agent_job — the Celery dispatch target (P1.3)
# ---------------------------------------------------------------------------


async def _execute_agent_job(
    job_id: str,
    request_payload: dict,
    user_id: str,
    *,
    lease_owner: str,
) -> dict:
    """Claim the run, rebuild the request/user, and drive the shared runner.

    ``agent_execution_service._run_agent_graph`` owns every status transition
    for the run itself (completed / failed / awaiting_confirmation /
    cancelled / timeout); this wrapper only handles what can go wrong BEFORE
    the runner starts.
    """
    from src.services.agent import agent_run_service

    settings = get_settings()
    claim = await agent_run_service.claim_execution_safe(
        job_id,
        lease_owner=lease_owner,
        lease_seconds=settings.AGENT_RUN_EXECUTION_LEASE_SECONDS,
    )
    if claim == "duplicate":
        logger.info(
            "run_agent_job: duplicate delivery for job %s — no-op (lease taken "
            "or run no longer running)",
            job_id,
        )
        return {"job_id": job_id, "outcome": "duplicate-noop"}
    if claim in ("missing", "error"):
        # Without the claim there is no duplicate protection, so the turn must
        # not run. Fail the job record so the poller stops cleanly.
        logger.error(
            "run_agent_job: cannot claim job %s (%s) — refusing to run", job_id, claim
        )
        await _fail_job_record(
            job_id,
            "Agent run could not be claimed for execution. Please retry.",
            user_id=user_id,
        )
        return {"job_id": job_id, "outcome": f"unclaimed-{claim}"}

    # Rebuild the actor. AsyncSessionLocal has expire_on_commit=False and the
    # runner only reads loaded scalar attributes (id/organization_id/...), so
    # a detached instance is safe — same lifecycle as the BackgroundTasks
    # path, whose request-scoped user is detached by the time it runs.
    from src.core.database import AsyncSessionLocal
    from src.models.user import User

    try:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, uuid.UUID(user_id))
    except Exception:
        logger.exception("run_agent_job: failed to load user for job %s", job_id)
        user = None
    if user is None or not getattr(user, "is_active", True):
        await _fail_job_record(job_id, "Agent run owner unavailable.", user_id=user_id)
        return {"job_id": job_id, "outcome": "user-unavailable"}

    from src.services.agent.agent_execution_service import _run_agent_graph
    from src.services.agent.schemas import AgentExecuteRequest

    try:
        request = AgentExecuteRequest(**request_payload)
    except Exception:
        logger.exception("run_agent_job: invalid request payload for job %s", job_id)
        await _fail_job_record(
            job_id,
            "Agent run payload could not be reconstructed.",
            user_id=user_id,
            organization_id=str(getattr(user, "organization_id", "") or "") or None,
        )
        return {"job_id": job_id, "outcome": "bad-payload"}

    await _mark_job_running(job_id, request_payload, user)

    # The shared runner — the same coroutine the background dispatch runs.
    # It updates the job record itself on every outcome (including its own
    # timeout/cancel/exception paths), so nothing to do afterwards. The
    # execution lease is intentionally NOT released: terminal status ends the
    # run's lifecycle, and a NULL lease would re-open the one-shot claim.
    from src.services.agent.typesafe_classifier import start_typesafe_client

    start_typesafe_client()
    await _run_agent_graph(job_id, request, user)
    return {"job_id": job_id, "outcome": "ran"}


@celery_app.task(
    name="src.tasks.agent_run_tasks.run_agent_job",
    bind=True,
    # No autoretry: a retry would no-op on the claim, and re-running a turn
    # whose tools may have executed is never safe.
    soft_time_limit=_RUN_FUTURE_TIMEOUT_SECONDS + 60,
    time_limit=_RUN_FUTURE_TIMEOUT_SECONDS + 120,
)
def run_agent_job(self, job_id: str, request_payload: dict, user_id: str) -> dict:
    """Run one agent /execute turn on the worker (AGENT_DISPATCH_BACKEND=celery)."""
    lease_owner = f"celery:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    try:
        return run_async(
            _execute_agent_job(
                job_id, request_payload, user_id, lease_owner=lease_owner
            ),
            timeout=_RUN_FUTURE_TIMEOUT_SECONDS,
        )
    except Exception:
        # Last-resort: the runner normally marks the job itself; this covers
        # a wrapper-level crash (future timeout, loop failure) so the poller
        # is never left spinning on "running".
        logger.exception("run_agent_job: wrapper failure for job %s", job_id)
        try:
            run_async(
                _fail_job_record(
                    job_id,
                    "Agent execution failed on the worker. Please retry.",
                    user_id=user_id,
                ),
                timeout=30,
            )
        except Exception:
            logger.exception("run_agent_job: failed to fail job %s", job_id)
        raise


# ---------------------------------------------------------------------------
# sweep_stale_agent_runs — beat sweeper (P1.4a)
# ---------------------------------------------------------------------------


async def _sweep_stale_agent_runs(*, lease_owner: str) -> dict:
    """Reap non-terminal ``agent_runs`` rows nobody is working on.

    Policy (conservative — audit P1.4a): mark FAILED, never re-enqueue. Per
    candidate (non-terminal, no live lease, no status write for
    ``AGENT_RUN_STALE_AFTER_SECONDS``):

    - claim the sweeper lease (atomic; losers skip the row),
    - re-read and re-check under the lease (a confirm may have just landed),
    - ``awaiting_confirmation`` gets the longer
      ``AGENT_RUN_STALE_AWAITING_AFTER_SECONDS`` window — it is a
      legitimately-parked state until the 1h Redis job TTL makes the confirm
      impossible,
    - if the LIVE job store still holds a terminal status the projection
      missed (fire-and-forget write lost), REPAIR the projection instead of
      failing a finished run,
    - otherwise mark the run failed in the projection AND (when a record
      exists) the live job store, so pollers stop spinning.

    Skipped rows keep the short sweeper lease until it self-expires — never
    released back to NULL, so the one-shot execution-claim invariant
    (``lease_owner IS NULL`` ⇒ never executed) survives sweeping.
    """
    from src.core.database import AsyncSessionLocal
    from src.models.agent_run import AgentRun
    from src.services.agent import agent_run_service, job_store

    settings = get_settings()
    now = _utcnow()
    # S2-M15: the effective staleness floor is pinned to the live-run
    # heartbeat margin — a misconfigured STALE_AFTER below 4x heartbeat must
    # not let the sweeper kill runs that are being kept fresh right now.
    stale_after = max(
        settings.AGENT_RUN_STALE_AFTER_SECONDS,
        4 * getattr(settings, "AGENT_RUN_HEARTBEAT_SECONDS", 60),
    )
    cutoff = now - timedelta(seconds=stale_after)
    awaiting_cutoff = now - timedelta(
        seconds=settings.AGENT_RUN_STALE_AWAITING_AFTER_SECONDS
    )

    scanned = failed = repaired = skipped = 0
    async with AsyncSessionLocal() as db:
        candidates = await agent_run_service.list_stale_runs(
            db, updated_before=cutoff, limit=100, now=now
        )
        for candidate in candidates:
            scanned += 1
            job_id = candidate.job_id
            # Staleness is judged from the LISTED values: claim_lease bumps
            # updated_at (it is the row's last-touch column), so the
            # post-claim copy can't distinguish "our claim" from "progress".
            listed_status = _coerce_status(candidate.status)
            listed_updated_at = _as_utc(candidate.updated_at)
            try:
                if listed_status is None:
                    skipped += 1
                    continue
                if listed_status is JobStatus.AWAITING_CONFIRMATION and (
                    listed_updated_at is None or listed_updated_at >= awaiting_cutoff
                ):
                    skipped += 1  # still within the confirmable window
                    continue
                claimed = await agent_run_service.claim_lease(
                    db, job_id, lease_owner=lease_owner, lease_seconds=300, now=now
                )
                if not claimed:
                    skipped += 1
                    continue
                run = await db.get(AgentRun, job_id)
                if run is not None:
                    await db.refresh(run)
                status = _coerce_status(run.status) if run is not None else None
                if run is None or status is None:
                    skipped += 1
                    continue
                if status.is_terminal:
                    # Finished between listing and claiming — nothing to do.
                    await agent_run_service.release_lease(
                        db, job_id, lease_owner=lease_owner
                    )
                    skipped += 1
                    continue
                if status is not listed_status:
                    # Transitioned under us (e.g. a confirm re-entered the
                    # graph) — it is making progress; leave it and let the
                    # short sweeper lease self-expire.
                    skipped += 1
                    continue

                # Cross-check the LIVE store before declaring death: a lost
                # fire-and-forget projection write must not fail a run that
                # actually completed.
                job = None
                job_read_failed = False
                try:
                    job = await job_store.get_job_fresh(job_id)
                except Exception:
                    job_read_failed = True
                    logger.warning(
                        "sweep_stale_agent_runs: job-store read failed for %s",
                        job_id,
                        exc_info=True,
                    )
                if job_read_failed:
                    # The live state is unknown. Keep the 300-second sweeper
                    # lease until it self-expires so a delayed delivery cannot
                    # reopen a run whose tools may already have executed.
                    skipped += 1
                    continue
                live_status = _coerce_status(job.get("status")) if job else None
                if live_status is not None and live_status.is_terminal:
                    await agent_run_service.upsert_run(
                        db,
                        job_id=job_id,
                        status=live_status,
                        error=job.get("error") if job else None,
                    )
                    await agent_run_service.release_lease(
                        db, job_id, lease_owner=lease_owner
                    )
                    repaired += 1
                    logger.warning(
                        "sweep_stale_agent_runs: repaired projection for job %s "
                        "from live store (status=%s)",
                        job_id,
                        live_status.value,
                    )
                    continue

                # R2-M13: preserve any real terminal error the run recorded
                # before it went quiet instead of overwriting it with the
                # generic swept-as-stale line.
                prior_error = (job or {}).get("error")
                error = prior_error or (
                    f"Swept as stale: status '{status.value}' with no progress "
                    "since "
                    f"{listed_updated_at.isoformat() if listed_updated_at else 'unknown'}"
                )
                await agent_run_service.upsert_run(
                    db, job_id=job_id, status=JobStatus.FAILED, error=error
                )
                if job is not None:
                    live_payload = dict(job)
                    live_payload.update(
                        {
                            "status": JobStatus.FAILED,
                            "error": error,
                            "tool_executions": live_payload.get("tool_executions", []),
                        }
                    )
                    try:
                        await job_store.set_job(job_id, live_payload)
                    except Exception:
                        logger.warning(
                            "sweep_stale_agent_runs: live-store fail-write "
                            "failed for %s",
                            job_id,
                            exc_info=True,
                        )
                await agent_run_service.release_lease(
                    db, job_id, lease_owner=lease_owner
                )
                failed += 1
                logger.warning(
                    "sweep_stale_agent_runs: marked job %s failed (was %s, "
                    "stale since %s)",
                    job_id,
                    status.value,
                    listed_updated_at.isoformat() if listed_updated_at else "unknown",
                )
            except Exception:
                skipped += 1
                logger.exception(
                    "sweep_stale_agent_runs: error sweeping job %s", job_id
                )

    result = {
        "scanned": scanned,
        "failed": failed,
        "repaired": repaired,
        "skipped": skipped,
    }
    logger.info("sweep_stale_agent_runs: %s", result)
    return result


@celery_app.task(
    name="src.tasks.agent_run_tasks.sweep_stale_agent_runs",
    soft_time_limit=_SWEEP_FUTURE_TIMEOUT_SECONDS + 30,
    time_limit=_SWEEP_FUTURE_TIMEOUT_SECONDS + 60,
)
def sweep_stale_agent_runs() -> dict:
    """Beat entry point — flag-gated by SWEEPERS_ENABLED."""
    if not get_settings().SWEEPERS_ENABLED:
        logger.info("sweep_stale_agent_runs: skipped (SWEEPERS_ENABLED=false)")
        return {"skipped": "sweepers-disabled"}
    lease_owner = f"sweeper:{socket.gethostname()}:{os.getpid()}"
    return run_async(
        _sweep_stale_agent_runs(lease_owner=lease_owner),
        timeout=_SWEEP_FUTURE_TIMEOUT_SECONDS,
    )
