"""Owner of ALL access to the ``agent_runs`` Postgres projection.

The Redis job store (``job_store.py``) stays authoritative for live polling;
this service mirrors every status transition into Postgres so a run's
lifecycle survives Redis failover (audit findings X1/D7) and a future sweeper
can reap runs stuck ``running``. Nothing else may query the table.

Design rules:

- **Log-and-continue writes.** ``record_job_status`` (the write-through entry
  point used by the job store) opens its own ``AsyncSessionLocal`` and never
  raises — a Postgres blip must not fail an agent turn while Redis is still
  authoritative.
- **Tenancy.** User-facing reads (``get_run`` / the poll fallback) MUST filter
  ``organization_id`` and ``user_id`` — both compared null-safely, so an
  org-less user only sees org-less rows. Only the sweeper's claim/list APIs
  scan cross-tenant (system maintenance, never exposed to clients).
- **Status domain.** Every write normalizes through ``JobStatus`` (collapsing
  the legacy ``"error"`` alias to ``FAILED``); unknown strings are dropped
  with a log instead of poisoning the projection.

Core functions take an explicit ``AsyncSession`` (testable against sqlite);
the ``*_safe`` wrappers open their own session for background contexts,
mirroring ``jobs._persist_assistant_message_safe``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_run import AgentRun
from src.shared.enums import JobStatus

logger = logging.getLogger(__name__)

# Default sweeper lease: long enough to resolve/kill a stuck run, short enough
# that a crashed sweeper doesn't block the next pass for long.
DEFAULT_LEASE_SECONDS = 300
_ACTIVE_RUN_STATUSES = tuple(
    status.value for status in JobStatus if not status.is_terminal
)


class ActiveRunConflict(RuntimeError):
    """A non-terminal run already owns the thread's single-writer slot."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_uuid(value: Any) -> Optional[UUID]:
    """Best-effort UUID coercion — job-store dicts carry ids as strings."""
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _coerce_status(value: Any) -> Optional[JobStatus]:
    """Normalize a wire/job-store status to JobStatus, or None if unknown."""
    try:
        return JobStatus(value)
    except ValueError:
        logger.warning("agent_runs: dropping unknown job status %r", value)
        return None


# ---------------------------------------------------------------------------
# Core API (explicit session — unit-testable against sqlite)
# ---------------------------------------------------------------------------


async def upsert_run(
    db: AsyncSession,
    *,
    job_id: str,
    status: JobStatus | str,
    organization_id: Any = None,
    user_id: Any = None,
    thread_id: Optional[str] = None,
    error: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Optional[AgentRun]:
    """Create-or-update the projection row for *job_id*. Commits.

    Update path: status/error/updated_at always; org/user/thread are filled
    only when previously NULL (a later replace-style job-store write that
    omits them must not erase tenancy). Terminal states are absorbing: the
    projection is fire-and-forget, so a delayed task carrying an earlier
    non-terminal status can land after the terminal one — it must not
    resurrect a finished run. (Non-terminal ↔ non-terminal transitions stay
    free-form: awaiting→running on confirm, running→awaiting on re-park.)

    Create path: requires ``user_id`` (a row without an owner is unreadable —
    the poll fallback fails closed on owner mismatch). Ownerless status
    updates for unknown rows are dropped with a log.
    """
    normalized = _coerce_status(status)
    if normalized is None:
        return None
    org_uuid = _coerce_uuid(organization_id)
    user_uuid = _coerce_uuid(user_id)
    # thread_id is a GUID column now; legacy callers pass strings (sometimes
    # the non-uuid job-id fallback) — drop what cannot bind.
    thread_uuid = _coerce_uuid(thread_id)

    run = await db.get(AgentRun, job_id)
    if run is None:
        if user_uuid is None:
            logger.warning(
                "agent_runs: no row for job %s and no user_id in payload; "
                "skipping insert",
                job_id,
            )
            return None
        run = AgentRun(
            job_id=job_id,
            organization_id=org_uuid,
            user_id=user_uuid,
            thread_id=thread_uuid,
            status=normalized.value,
            error=error,
            idempotency_key=idempotency_key,
        )
        db.add(run)
        try:
            await db.commit()
        except IntegrityError:
            # A concurrent creator may have won the job-id/idempotency race, or
            # another run may already own this thread's partial-unique slot.
            # Only a genuinely dangling legacy thread id may retry uncorrelated;
            # dropping thread_id for an active-run collision would let two graph
            # writers race the same LangGraph checkpoint.
            await db.rollback()
            run = await db.get(AgentRun, job_id)
            if run is None:
                if idempotency_key is not None:
                    existing = await get_run_by_idempotency_key(
                        db,
                        idempotency_key,
                        organization_id=org_uuid,
                        user_id=user_uuid,
                    )
                    if existing is not None:
                        return None
                if thread_uuid is not None:
                    active = (
                        await db.execute(
                            select(AgentRun).where(
                                AgentRun.thread_id == thread_uuid,
                                AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
                            )
                        )
                    ).scalar_one_or_none()
                    if active is not None:
                        raise ActiveRunConflict(
                            "A response is already in progress for this thread."
                        )
                if thread_uuid is None:  # pragma: no cover — PK race implies row
                    return None
                run = AgentRun(
                    job_id=job_id,
                    organization_id=org_uuid,
                    user_id=user_uuid,
                    thread_id=None,
                    status=normalized.value,
                    error=error,
                    idempotency_key=idempotency_key,
                )
                db.add(run)
                try:
                    await db.commit()
                except IntegrityError:  # pragma: no cover — PK race on retry
                    await db.rollback()
                    run = await db.get(AgentRun, job_id)
                    if run is None:
                        return None
                else:
                    return run
        else:
            return run

    current = _coerce_status(run.status)
    if current is not None and current.is_terminal and normalized != current:
        return run  # absorbing terminal state — drop the stale transition

    run.status = normalized.value
    run.error = error
    run.updated_at = _utcnow()
    if run.organization_id is None and org_uuid is not None:
        run.organization_id = org_uuid
    if run.user_id is None and user_uuid is not None:
        run.user_id = user_uuid
    if run.thread_id is None and thread_uuid is not None:
        run.thread_id = thread_uuid
    if run.idempotency_key is None and idempotency_key:
        run.idempotency_key = idempotency_key
    try:
        await db.commit()
    except IntegrityError:
        # A backfill can collide with another run's active-thread slot. Do not
        # silently drop correlation and keep both writers alive.
        await db.rollback()
        if thread_uuid is not None:
            active = (
                await db.execute(
                    select(AgentRun).where(
                        AgentRun.thread_id == thread_uuid,
                        AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
                    )
                )
            ).scalar_one_or_none()
            if active is not None and active.job_id != job_id:
                raise ActiveRunConflict(
                    "A response is already in progress for this thread."
                )
        # Otherwise this is the legacy dangling-thread path: keep the status
        # transition and leave correlation unchanged.
        run = await db.get(AgentRun, job_id)
        if run is None:  # pragma: no cover
            return None
        run.status = normalized.value
        run.error = error
        run.updated_at = _utcnow()
        await db.commit()
    return run


async def get_run(
    db: AsyncSession,
    job_id: str,
    *,
    organization_id: Any,
    user_id: Any,
) -> Optional[AgentRun]:
    """Tenant-scoped fetch — the ONLY read for user-facing paths.

    Filters organization_id AND user_id (mandatory tenancy rule). ``== None``
    compiles to ``IS NULL``, so an org-less caller matches only org-less rows.
    Returns None on any mismatch: caller 404s without confirming existence.
    """
    stmt = select(AgentRun).where(
        AgentRun.job_id == job_id,
        AgentRun.organization_id == _coerce_uuid(organization_id),
        AgentRun.user_id == _coerce_uuid(user_id),
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def is_run_cancellation_requested(
    db: AsyncSession,
    job_id: str,
    *,
    organization_id: Any,
    user_id: Any,
) -> bool:
    """Read the durable stop marker for one caller-owned run.

    The producer polls scalar columns rather than an ORM instance so a
    long-lived stream session cannot reuse stale identity-map state after the
    HTTP Stop request commits in another session.
    """
    row = (
        await db.execute(
            select(AgentRun.status, AgentRun.cancel_requested_at)
            .where(
                AgentRun.job_id == job_id,
                AgentRun.organization_id == _coerce_uuid(organization_id),
                AgentRun.user_id == _coerce_uuid(user_id),
            )
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        return False
    return bool(
        row.cancel_requested_at is not None or row.status == JobStatus.STOPPING.value
    )


async def get_active_run_for_thread(
    db: AsyncSession,
    thread_id: Any,
    *,
    organization_id: Any,
    user_id: Any,
) -> Optional[AgentRun]:
    """Return the caller-owned non-terminal run for a durable thread.

    The partial unique index permits at most one such row. This lookup exists
    for HITL resume paths that receive a thread id rather than a run id; the
    same mandatory org + user filters as :func:`get_run` prevent an untrusted
    checkpoint identifier from entering trace metadata.
    """
    thread_uuid = _coerce_uuid(thread_id)
    if thread_uuid is None:
        return None
    stmt = select(AgentRun).where(
        AgentRun.thread_id == thread_uuid,
        AgentRun.organization_id == _coerce_uuid(organization_id),
        AgentRun.user_id == _coerce_uuid(user_id),
        AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def claim_awaiting_run_for_confirmation(
    db: AsyncSession,
    job_id: str,
    *,
    organization_id: Any,
    user_id: Any,
) -> bool:
    """Atomically claim one caller-owned parked run for confirmation. Commits."""
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.job_id == job_id,
            AgentRun.organization_id == _coerce_uuid(organization_id),
            AgentRun.user_id == _coerce_uuid(user_id),
            AgentRun.status == JobStatus.AWAITING_CONFIRMATION.value,
        )
        .values(status=JobStatus.RUNNING.value, updated_at=_utcnow())
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return bool(result.rowcount)


async def release_confirmation_claim(
    db: AsyncSession,
    job_id: str,
    *,
    organization_id: Any,
    user_id: Any,
) -> bool:
    """Return a pre-execution confirmation claim to its parked state. Commits."""
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.job_id == job_id,
            AgentRun.organization_id == _coerce_uuid(organization_id),
            AgentRun.user_id == _coerce_uuid(user_id),
            AgentRun.status == JobStatus.RUNNING.value,
        )
        .values(status=JobStatus.AWAITING_CONFIRMATION.value, updated_at=_utcnow())
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return bool(result.rowcount)


async def get_run_by_idempotency_key(
    db: AsyncSession,
    idempotency_key: str,
    *,
    organization_id: Any,
    user_id: Any,
) -> Optional[AgentRun]:
    """Tenant-scoped lookup of a run by its client idempotency key.

    Used by the Celery dispatch path to resolve a retried /execute (same
    ``client_message_id``) to the run it already created instead of enqueueing
    the turn twice. Same mandatory null-safe org+user filter as ``get_run`` —
    a key collision across tenants (malicious or otherwise) resolves to
    nothing rather than another tenant's run.
    """
    stmt = select(AgentRun).where(
        AgentRun.idempotency_key == idempotency_key,
        AgentRun.organization_id == _coerce_uuid(organization_id),
        AgentRun.user_id == _coerce_uuid(user_id),
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def claim_execution(
    db: AsyncSession,
    job_id: str,
    *,
    lease_owner: str,
    lease_seconds: int,
    now: Optional[datetime] = None,
) -> str:
    """One-shot execution claim for the Celery runner. Commits.

    Returns ``"claimed"`` | ``"duplicate"`` | ``"missing"``.

    Unlike ``claim_lease`` (the sweeper's renewable/expirable lease), this
    claim succeeds at most ONCE per run: it atomically moves ``queued`` to
    ``running`` while requiring ``lease_owner IS NULL``. During the staged
    rollout it also accepts an unleased legacy ``running`` row written by an
    older API pod. That single-statement condition is what makes
    duplicate task deliveries safe:

    - acks_late redelivery after a worker crash *mid-run*: the first delivery
      already stamped ``lease_owner`` → duplicate no-ops (the sweeper reaps
      the stuck row; we never auto re-run a turn whose tools may have already
      produced side effects).
    - redelivery after a crash *before* the claim: nothing ran, the lease is
      still NULL → the redelivery legitimately claims and recovers the turn.
    - a stale duplicate arriving while a HITL confirm-resume has the run
      ``running`` again: ``lease_owner`` was stamped by the original
      execution and is never cleared on a live run → no-ops.

    The claim is intentionally never released; terminal status (not lease
    state) is what ends a run's lifecycle.
    """
    now = now or _utcnow()
    stmt = (
        update(AgentRun)
        .where(
            AgentRun.job_id == job_id,
            AgentRun.status.in_((JobStatus.QUEUED.value, JobStatus.RUNNING.value)),
            AgentRun.lease_owner.is_(None),
        )
        .values(
            status=JobStatus.RUNNING.value,
            started_at=now,
            lease_owner=lease_owner,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    result = await db.execute(stmt)
    await db.commit()
    if result.rowcount:
        return "claimed"
    exists = (
        await db.execute(select(AgentRun.job_id).where(AgentRun.job_id == job_id))
    ).scalar_one_or_none()
    return "missing" if exists is None else "duplicate"


async def claim_lease(
    db: AsyncSession,
    job_id: str,
    *,
    lease_owner: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: Optional[datetime] = None,
) -> bool:
    """Atomically claim (or renew) the sweeper lease on a run. Commits.

    Single-statement compare-and-claim: succeeds when the lease is free,
    expired, or already held by *lease_owner* (re-entrant renewal). Returns
    False when another live owner holds it or the row doesn't exist. System
    API — cross-tenant by design; never expose to request handlers.
    """
    now = now or _utcnow()
    stmt = (
        update(AgentRun)
        .where(
            AgentRun.job_id == job_id,
            or_(
                AgentRun.lease_owner.is_(None),
                AgentRun.lease_expires_at.is_(None),
                AgentRun.lease_expires_at < now,
                AgentRun.lease_owner == lease_owner,
            ),
        )
        .values(
            lease_owner=lease_owner,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    result = await db.execute(stmt)
    await db.commit()
    return bool(result.rowcount)


async def release_lease(db: AsyncSession, job_id: str, *, lease_owner: str) -> bool:
    """Release a lease held by *lease_owner* (no-op for other owners). Commits."""
    stmt = (
        update(AgentRun)
        .where(AgentRun.job_id == job_id, AgentRun.lease_owner == lease_owner)
        .values(lease_owner=None, lease_expires_at=None, updated_at=_utcnow())
        .execution_options(synchronize_session=False)
    )
    result = await db.execute(stmt)
    await db.commit()
    return bool(result.rowcount)


async def touch_run_updated_at(db: AsyncSession, job_id: str) -> bool:
    """Live-run heartbeat (audit S2-M15): bump ``updated_at`` so the staleness
    sweeper sees progress. Guarded to non-terminal rows — a real terminal
    write must never be resurrected by a heartbeat that raced it. Does not
    commit; the caller owns the transaction. Returns True when a live row was
    touched.
    """
    terminal = [status.value for status in JobStatus if status.is_terminal]
    stmt = (
        update(AgentRun)
        .where(
            AgentRun.job_id == job_id,
            AgentRun.status.notin_(terminal),
        )
        .values(updated_at=_utcnow())
        .execution_options(synchronize_session=False)
    )
    result = await db.execute(stmt)
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def list_stale_runs(
    db: AsyncSession,
    *,
    updated_before: datetime,
    limit: int = 50,
    now: Optional[datetime] = None,
) -> list[AgentRun]:
    """Non-terminal runs not updated since *updated_before* with no live lease.

    The sweeper (next PR) lists candidates here, then ``claim_lease``s each —
    the atomic per-row claim makes the list-then-claim pattern race-safe.
    System API — cross-tenant by design.
    """
    now = now or _utcnow()
    non_terminal = [s.value for s in JobStatus if not s.is_terminal]
    stmt = (
        select(AgentRun)
        .where(
            AgentRun.status.in_(non_terminal),
            AgentRun.updated_at < updated_before,
            or_(
                AgentRun.lease_owner.is_(None),
                AgentRun.lease_expires_at.is_(None),
                AgentRun.lease_expires_at < now,
            ),
        )
        .order_by(AgentRun.updated_at.asc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Background-safe wrappers (own session, never raise)
# ---------------------------------------------------------------------------


def _extract_thread_id(data: dict) -> Optional[str]:
    """Pull a thread id out of a job-store payload, wherever it lives."""
    tid = data.get("thread_id")
    if not tid:
        tid = (data.get("request") or {}).get("thread_id")
    if not tid:
        tid = (data.get("result") or {}).get("thread_id")
    return str(tid) if tid else None


async def record_job_status(
    job_id: str, data: dict, *, raise_on_error: bool = False
) -> None:
    """Project a job-store write into ``agent_runs`` — log-and-continue.

    The write-through entry point fired (as a background task) by the job
    store on every status write. Opens its own ``AsyncSessionLocal`` and
    swallows exceptions by default: Redis stays authoritative during rollout.
    Thread-scoped terminal writes pass ``raise_on_error=True`` so completion
    cannot become visible while the durable single-writer slot remains held.
    """
    try:
        status = data.get("status")
        if status is None:
            return
        from src.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await upsert_run(
                db,
                job_id=job_id,
                status=status,
                organization_id=data.get("organization_id"),
                user_id=data.get("user_id"),
                thread_id=_extract_thread_id(data),
                error=data.get("error"),
            )
    except Exception:
        logger.warning(
            "agent_runs projection write failed for job %s (Redis remains "
            "authoritative)",
            job_id,
            exc_info=True,
        )
        if raise_on_error:
            raise


async def claim_execution_safe(
    job_id: str, *, lease_owner: str, lease_seconds: int
) -> str:
    """Own-session ``claim_execution`` for the Celery task. Never raises.

    Returns the claim outcome, or ``"error"`` when Postgres is unreachable —
    the caller must then NOT run the turn (without the claim there is no
    duplicate-delivery protection) and should fail the job record instead.
    """
    try:
        from src.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            return await claim_execution(
                db, job_id, lease_owner=lease_owner, lease_seconds=lease_seconds
            )
    except Exception:
        logger.warning(
            "agent_runs execution claim failed for job %s", job_id, exc_info=True
        )
        return "error"


async def get_run_fallback(
    job_id: str, *, organization_id: Any, user_id: Any
) -> Optional[AgentRun]:
    """Poll-path fallback read for a Redis miss. Never raises.

    Opens its own session; a Postgres error degrades to None (the caller
    404s — exactly the pre-projection behavior), logged for visibility.
    """
    try:
        from src.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            return await get_run(
                db, job_id, organization_id=organization_id, user_id=user_id
            )
    except Exception:
        logger.warning(
            "agent_runs fallback read failed for job %s", job_id, exc_info=True
        )
        return None
