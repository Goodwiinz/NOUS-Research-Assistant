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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
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
_PRE_STOP_RUN_STATUSES = tuple(
    status for status in _ACTIVE_RUN_STATUSES if status != JobStatus.STOPPING.value
)


class ActiveRunConflict(RuntimeError):
    """A non-terminal run already owns the thread's single-writer slot."""


class RunStatusProjectionRejected(RuntimeError):
    """The durable row rejected a requested job-status projection."""


@dataclass(frozen=True)
class RunStatusDecision:
    """Immutable durable outcome copied from the committed agent run row."""

    job_id: str
    requested_status: JobStatus
    effective_status: JobStatus
    user_id: Optional[str]
    organization_id: Optional[str]
    thread_id: Optional[str]
    error: Optional[str]
    cancel_requested_at: Optional[str]
    updated_at: str

    @property
    def requested_status_won(self) -> bool:
        """Whether the durable row has the requested status after this write."""
        return self.requested_status == self.effective_status


def _decision_from_run(
    job_id: str, requested_status: JobStatus, run: Optional[AgentRun]
) -> Optional[RunStatusDecision]:
    if run is None:
        return None
    effective_status = _coerce_status(run.status)
    if effective_status is None:
        return None

    def as_string(value: Any) -> Optional[str]:
        return str(value) if value is not None else None

    cancel_requested_at = run.cancel_requested_at
    return RunStatusDecision(
        job_id=job_id,
        requested_status=requested_status,
        effective_status=effective_status,
        user_id=as_string(run.user_id),
        organization_id=as_string(run.organization_id),
        thread_id=as_string(run.thread_id),
        error=run.error,
        cancel_requested_at=(
            cancel_requested_at.isoformat() if cancel_requested_at is not None else None
        ),
        updated_at=run.updated_at.isoformat(),
    )


def _integrity_error_chain(exc: IntegrityError) -> list[Any]:
    """Return the bounded SQLAlchemy/driver cause chain for an integrity error."""
    chain: list[Any] = []
    current: Any = exc
    for _ in range(5):
        if current is None or current in chain:
            break
        chain.append(current)
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
    return chain


def _integrity_constraint_name(exc: IntegrityError) -> Optional[str]:
    """Read a driver-reported constraint name without parsing user text."""
    for original in _integrity_error_chain(exc):
        constraint = getattr(original, "constraint_name", None)
        if constraint:
            return str(constraint)
        diagnostic = getattr(original, "diag", None)
        constraint = getattr(diagnostic, "constraint_name", None)
        if constraint:
            return str(constraint)
    return None


def _is_active_thread_conflict(exc: IntegrityError) -> bool:
    constraint = _integrity_constraint_name(exc)
    if constraint == "uq_agent_runs_active_thread":
        return True
    # SQLite's unit schema keeps the same partial unique index but does not
    # expose its name on IntegrityError; only this exact generated key message
    # is accepted as the active-thread collision.
    message = str(exc.orig).lower()
    return "unique constraint failed: agent_runs.thread_id" in message


def _is_missing_thread_fk(exc: IntegrityError) -> bool:
    sqlstate = None
    for original in _integrity_error_chain(exc):
        sqlstate = getattr(original, "sqlstate", None) or getattr(
            original, "pgcode", None
        )
        if sqlstate:
            break
    constraint = _integrity_constraint_name(exc)
    return sqlstate == "23503" and bool(
        constraint and constraint.endswith("_thread_id_fkey")
    )


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

    # The update itself is the transition claim. Reading an ORM object first
    # and writing it later lets a stale producer replace a completion or Stop
    # acknowledgement between those two operations.
    run = await _transition_existing_run(
        db,
        job_id=job_id,
        normalized=normalized,
        organization_id=org_uuid,
        user_id=user_uuid,
        thread_id=thread_uuid,
        error=error,
        idempotency_key=idempotency_key,
        backfill_metadata=True,
    )
    if run is not None:
        return run

    # A zero-row update can mean either a missing row or an existing row that
    # rejected this transition / owner. Reload before attempting an insert.
    existing = await _reload_run(db, job_id)
    if existing is not None:
        if not _belongs_to_upsert_owner(existing, org_uuid, user_uuid):
            # A mismatched payload cannot backfill tenant or correlation data
            # on another owner's partially scoped legacy row.
            return None
        return existing

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
    except IntegrityError as insert_error:
        # Resolve duplicate job/idempotency claims before classifying a thread
        # collision. The transaction must be rolled back before any recovery
        # read can be trusted.
        await db.rollback()
        existing = await _reload_run(db, job_id)
        if existing is not None:
            if not _belongs_to_upsert_owner(existing, org_uuid, user_uuid):
                return None
            return (
                await _transition_existing_run(
                    db,
                    job_id=job_id,
                    normalized=normalized,
                    organization_id=org_uuid,
                    user_id=user_uuid,
                    thread_id=thread_uuid,
                    error=error,
                    idempotency_key=idempotency_key,
                    backfill_metadata=True,
                )
                or existing
            )

        if idempotency_key is not None:
            duplicate = await get_run_by_idempotency_key(
                db,
                idempotency_key,
                organization_id=org_uuid,
                user_id=user_uuid,
            )
            if duplicate is not None:
                return None

        if thread_uuid is not None and _is_active_thread_conflict(insert_error):
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
            # The original writer may have released the slot while this
            # transaction rolled back. Retry once with the original, valid
            # correlation; never use absence from the active-row lookup as
            # evidence that its FK is dangling.
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
                return run
            except IntegrityError as retry_error:
                await db.rollback()
                existing = await _reload_run(db, job_id)
                if existing is not None:
                    if not _belongs_to_upsert_owner(existing, org_uuid, user_uuid):
                        return None
                    return existing
                if _is_active_thread_conflict(retry_error):
                    raise ActiveRunConflict(
                        "A response is already in progress for this thread."
                    ) from retry_error
                if not _is_missing_thread_fk(retry_error):
                    raise
                insert_error = retry_error
        elif not _is_missing_thread_fk(insert_error):
            raise insert_error

        if thread_uuid is None:
            return None
        # Compatibility path for a positively identified dangling-thread FK.
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
        except IntegrityError as fallback_error:
            await db.rollback()
            existing = await _reload_run(db, job_id)
            if existing is None:
                if _is_active_thread_conflict(fallback_error):
                    raise ActiveRunConflict(
                        "A response is already in progress for this thread."
                    ) from fallback_error
                raise
            if not _belongs_to_upsert_owner(existing, org_uuid, user_uuid):
                return None
            return existing
        return run
    return run


def _transition_predicate(status: JobStatus):
    """SQL predicate for a status transition, evaluated at the write point."""
    if status == JobStatus.CANCELLED:
        # A producer may acknowledge cancellation from any active state. This
        # also covers task-level cancellation without a prior HTTP Stop claim.
        return AgentRun.status.in_(_ACTIVE_RUN_STATUSES) | (
            AgentRun.status == JobStatus.CANCELLED.value
        )
    if status == JobStatus.STOPPING:
        return AgentRun.status.in_(_PRE_STOP_RUN_STATUSES) | (
            AgentRun.status == JobStatus.STOPPING.value
        )
    if status.is_terminal:
        # Same-terminal writes are idempotent. Other terminal writes require a
        # non-stopping, uncancelled active row.
        return (AgentRun.status == status.value) | and_(
            AgentRun.status.in_(_PRE_STOP_RUN_STATUSES),
            AgentRun.cancel_requested_at.is_(None),
        )
    return and_(
        AgentRun.status.in_(_PRE_STOP_RUN_STATUSES),
        AgentRun.cancel_requested_at.is_(None),
    )


def _belongs_to_upsert_owner(
    run: AgentRun, organization_id: Optional[UUID], user_id: Optional[UUID]
) -> bool:
    return (user_id is None or run.user_id in (None, user_id)) and (
        organization_id is None or run.organization_id in (None, organization_id)
    )


async def _reload_run(db: AsyncSession, job_id: str) -> Optional[AgentRun]:
    return (
        await db.execute(
            select(AgentRun)
            .where(AgentRun.job_id == job_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _transition_existing_run(
    db: AsyncSession,
    *,
    job_id: str,
    normalized: JobStatus,
    organization_id: Optional[UUID],
    user_id: Optional[UUID],
    thread_id: Optional[UUID],
    error: Optional[str],
    idempotency_key: Optional[str],
    backfill_metadata: bool,
) -> Optional[AgentRun]:
    predicates = [AgentRun.job_id == job_id, _transition_predicate(normalized)]
    if organization_id is not None:
        predicates.append(
            or_(
                AgentRun.organization_id.is_(None),
                AgentRun.organization_id == organization_id,
            )
        )
    if user_id is not None:
        predicates.append(or_(AgentRun.user_id.is_(None), AgentRun.user_id == user_id))

    values: dict[str, Any] = {
        "status": normalized.value,
        "error": error,
        "updated_at": _utcnow(),
    }
    if backfill_metadata:
        if organization_id is not None:
            values["organization_id"] = func.coalesce(
                AgentRun.organization_id, organization_id
            )
        if user_id is not None:
            values["user_id"] = func.coalesce(AgentRun.user_id, user_id)
        if thread_id is not None:
            values["thread_id"] = func.coalesce(AgentRun.thread_id, thread_id)
        if idempotency_key:
            values["idempotency_key"] = func.coalesce(
                AgentRun.idempotency_key, idempotency_key
            )

    retry_with_correlation_succeeded = False
    fallback_to_status_only = False
    try:
        result = await db.execute(
            update(AgentRun)
            .where(*predicates)
            .values(**values)
            .returning(AgentRun.job_id)
            .execution_options(synchronize_session=False)
        )
        transitioned = result.scalar_one_or_none() is not None
        await db.commit()
    except IntegrityError as update_error:
        await db.rollback()
        if thread_id is not None and _is_active_thread_conflict(update_error):
            active = (
                await db.execute(
                    select(AgentRun).where(
                        AgentRun.thread_id == thread_id,
                        AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
                    )
                )
            ).scalar_one_or_none()
            if active is not None and active.job_id != job_id:
                raise ActiveRunConflict(
                    "A response is already in progress for this thread."
                )
            # The blocking run may have completed between rollback and the
            # lookup. Retry the original guarded write once, retaining T.
            try:
                result = await db.execute(
                    update(AgentRun)
                    .where(*predicates)
                    .values(**values)
                    .returning(AgentRun.job_id)
                    .execution_options(synchronize_session=False)
                )
                transitioned = result.scalar_one_or_none() is not None
                await db.commit()
                retry_with_correlation_succeeded = True
            except IntegrityError as retry_error:
                await db.rollback()
                if _is_active_thread_conflict(retry_error):
                    raise ActiveRunConflict(
                        "A response is already in progress for this thread."
                    ) from retry_error
                if not _is_missing_thread_fk(retry_error):
                    raise
                update_error = retry_error
                fallback_to_status_only = True
        elif not _is_missing_thread_fk(update_error):
            raise update_error
        else:
            fallback_to_status_only = True

        if fallback_to_status_only and thread_id is not None:
            # Only a positive thread-FK failure authorizes the legacy
            # status-only fallback. Preserve status/owner/cancellation guards.
            fallback_values = {
                "status": normalized.value,
                "error": error,
                "updated_at": _utcnow(),
            }
            result = await db.execute(
                update(AgentRun)
                .where(*predicates)
                .values(**fallback_values)
                .returning(AgentRun.job_id)
                .execution_options(synchronize_session=False)
            )
            transitioned = result.scalar_one_or_none() is not None
            await db.commit()
        elif not retry_with_correlation_succeeded:
            raise update_error

    if not transitioned:
        return None
    return await _reload_run(db, job_id)


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


async def get_latest_run_for_thread(
    db: AsyncSession,
    thread_id: Any,
    *,
    organization_id: Any,
    user_id: Any,
) -> Optional[AgentRun]:
    """Return the newest caller-owned run for a durable thread.

    Resume uses this only to distinguish a stale checkpoint from a legacy
    thread with no durable run. A terminal run means the checkpoint's old HITL
    interrupt must not be re-delivered after completion or cancellation.
    """
    thread_uuid = _coerce_uuid(thread_id)
    if thread_uuid is None:
        return None
    stmt = (
        select(AgentRun)
        .where(
            AgentRun.thread_id == thread_uuid,
            AgentRun.organization_id == _coerce_uuid(organization_id),
            AgentRun.user_id == _coerce_uuid(user_id),
        )
        .order_by(AgentRun.updated_at.desc())
        .limit(1)
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
) -> Optional[RunStatusDecision]:
    """Project a job-store write into ``agent_runs`` — log-and-continue.

    The write-through entry point fired (as a background task) by the job
    store on every status write. Opens its own ``AsyncSessionLocal`` and
    swallows exceptions by default: Redis stays authoritative during rollout.
    Thread-scoped terminal writes pass ``raise_on_error=True`` so completion
    cannot become visible while the durable single-writer slot remains held.
    """
    status = data.get("status")
    if status is None:
        return None
    normalized = _coerce_status(status)
    if normalized is None:
        return None
    try:
        from src.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            run = await upsert_run(
                db,
                job_id=job_id,
                status=normalized,
                organization_id=data.get("organization_id"),
                user_id=data.get("user_id"),
                thread_id=_extract_thread_id(data),
                error=data.get("error"),
            )
            decision = _decision_from_run(job_id, normalized, run)
            if decision is None and raise_on_error:
                raise RunStatusProjectionRejected(
                    f"agent_runs rejected status projection for job {job_id}"
                )
            return decision
    except Exception:
        logger.warning(
            "agent_runs projection write failed for job %s (Redis remains "
            "authoritative)",
            job_id,
            exc_info=True,
        )
        if raise_on_error:
            raise
        return None


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
