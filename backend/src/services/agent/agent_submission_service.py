"""Atomic acceptance of an agent submission (P0-C).

Until this module existed, ``POST /api/v1/agent/stream`` emitted its
``accepted`` SSE frame as the first line of the generator — before a single
row was written. A crash anywhere after that frame left the client holding an
acknowledgment for a run the system had no record of: the repo's dominant
"fake-success" bug shape, at the protocol level.

``accept_submission`` closes that gap. Everything the acknowledgment implies is
written in ONE transaction and the caller may only emit ``accepted`` after it
commits:

1. the user's message row (``chat_messages``),
2. on an edit-and-resend, the tombstones that supersede the edited turn and
   everything after it (``superseded_by_message_id``),
3. the run row (``agent_runs``) the accepted frame's ``run_id`` names,
4. ``run.created`` — seq 1 of the run's ledger, via the P0-B
   ``run_event_store`` (this is its first producer),
5. an ``agent_outbox`` dispatch-intent record.

Anything that fails before the commit rolls the whole thing back: no
``accepted``, an ``error`` frame instead, and no half-written turn.

**The outbox row is a record, not a queue.** Dispatch still happens exactly as
before — the stream runs the graph in-process immediately after the commit and
``mark_submission_dispatched`` stamps the row. No relay/poller reads
``status='pending'``; building one is a later work order, and whatever does it
must re-dispatch idempotently keyed on ``run_id``.

Design notes
------------

**Transaction ownership.** ``accept_submission`` owns exactly one transaction
on the session it is handed: it commits on success and rolls back on any
failure. The per-write helpers below never commit, so the fault-injection
tests can break the chain at any statement boundary and assert nothing
survives. ``run_event_store`` was built to the same rule (it never commits),
which is what lets its append join this transaction.

**Idempotency.** The key is derived from the newest user turn's
``client_message_id`` — the field ``/execute`` already uses for the same
purpose — under a distinct ``agent-stream:`` prefix so a turn sent to both
endpoints does not collapse into one run. A resubmission of the same key by
the same user resolves to the existing run instead of creating a second one;
the ``uq_agent_runs_user_idempotency_key`` partial unique index closes the
race, and the loser re-reads the winner.

**Single writer.** ``uq_agent_runs_active_thread`` makes "one non-terminal run
per thread" a database invariant. A new submission rejects while another run
is queued, running, or stopping. A parked ``awaiting_confirmation`` run is safe
to abandon because no graph invocation is active; it is cancelled atomically
with accepting the fresh turn, then the caller clears the stale checkpoint.

**Tenancy.** ``organization_id`` is nullable (org-less users exist), compared
null-safely, and NEVER stringified — ``str(None) == "None"`` has merged tenants
in this codebase before. The one query keyed by thread rather than org
(``_ensure_thread_idle``) is scoped by construction: its ``thread_id`` comes
from ``_resolve_thread``, which joins ``Workspace.owner_id == current_user.id``,
so the thread — and therefore every run correlated to it — is already
ownership-verified. Same reasoning as ``run_event_store.has_terminal_event``,
which is keyed only by a server-generated run id.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, cast
from uuid import UUID, uuid4

from asyncpg.exceptions import ConnectionDoesNotExistError
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.chat_message import ChatMessage, MessageRole
from src.models.thread import Thread
from src.services.agent.agent_run_service import ActiveRunConflict
from src.services.agent.run_event_store import RunAlreadyTerminalError, append_event
from src.services.agent.run_event_types import RunEventType
from src.shared.enums import TERMINAL_JOB_STATUSES, AgentOutboxStatus, JobStatus

logger = logging.getLogger(__name__)

# Dispatch kind for a turn executed in-process by the SSE stream generator.
# Free-form by design (see the model docstring): a relay dispatching to another
# backend must be addable without a migration.
DISPATCH_KIND_STREAM = "agent.stream.execute"

# Prefix keeps /stream and /execute idempotency namespaces disjoint: the same
# client_message_id sent to both endpoints is two different submissions.
_IDEMPOTENCY_PREFIX = "agent-stream"

_ACTIVE_RUN_STATUSES: frozenset[str] = frozenset(
    status.value for status in JobStatus if status not in TERMINAL_JOB_STATUSES
)
_TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    status.value for status in TERMINAL_JOB_STATUSES
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_uuid(value: Any) -> Optional[UUID]:
    """Best-effort UUID coercion — callers carry ids as strings or UUIDs.

    Never ``str()``: an unparseable or missing value must become ``None``
    (which filters ``IS NULL``), not the literal string ``"None"``.
    """
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


@dataclass(frozen=True)
class AcceptedSubmission:
    """What the caller needs after the accept transaction has committed."""

    run_id: str
    thread_id: str
    user_message_id: Optional[str]
    outbox_id: Optional[str]
    idempotency_key: Optional[str]
    # True when an idempotency key resolved to a run accepted by an earlier
    # request: nothing was written, and the caller must not dispatch again.
    replayed: bool = False
    # Edit-and-resend: True when this thread now HAS superseded rows
    # attributable to this turn, so the caller must resync the LangGraph
    # checkpoint before the model reads history. Distinct from
    # ``tombstoned_count`` (rows THIS transaction updated) for the same reason
    # ``TombstoneReport`` keeps the two apart: a replayed or deduped submission
    # updates nothing yet still needs the resync.
    tombstoned: bool = False
    tombstoned_count: int = 0


@dataclass(frozen=True)
class RunCancellationResult:
    """Outcome of the durable normal-Stop claim."""

    run_id: str
    status: JobStatus
    claimed: bool


def stream_idempotency_key(request: Any, current_user: Any) -> Optional[str]:
    """Idempotency key for a ``/stream`` submission, or ``None``.

    Derived from the newest user turn's ``client_message_id`` and scoped by
    user id so the per-user partial unique index cannot collide across
    tenants. ``None`` for legacy clients that send no id — those submissions
    are accepted unconditionally, exactly as before.
    """
    last = next(
        (
            m
            for m in reversed(getattr(request, "messages", []) or [])
            if m.role == "user"
        ),
        None,
    )
    cmid = getattr(last, "client_message_id", None) if last is not None else None
    if cmid is None:
        return None
    return f"{_IDEMPOTENCY_PREFIX}:{current_user.id}:{cmid}"


async def _find_by_idempotency_key(
    db: AsyncSession,
    key: str,
    *,
    organization_id: Any,
    user_id: Any,
) -> Optional[AgentRun]:
    """Tenant-scoped lookup of the run a key already created.

    Mandatory null-safe org + user filter: a key collision across tenants
    resolves to nothing rather than another tenant's run.
    """
    stmt = select(AgentRun).where(
        AgentRun.idempotency_key == key,
        AgentRun.organization_id == _coerce_uuid(organization_id),
        AgentRun.user_id == _coerce_uuid(user_id),
    )
    # Explicit cast: CI's Lint Backend job installs only the linters, so
    # SQLAlchemy is unresolvable there and ``scalar_one_or_none()`` degrades to
    # ``Any`` — which trips ``warn_return_any`` in CI while passing locally.
    return cast(Optional[AgentRun], (await db.execute(stmt)).scalar_one_or_none())


async def _existing_outbox_id(db: AsyncSession, run_id: str) -> Optional[str]:
    """The dispatch record for *run_id* (keyed by a server-generated id)."""
    stmt = select(AgentOutbox.id).where(AgentOutbox.run_id == run_id).limit(1)
    found = (await db.execute(stmt)).scalar_one_or_none()
    return str(found) if found is not None else None


async def _replayed_submission(
    db: AsyncSession,
    existing: AgentRun,
    *,
    thread_id: UUID,
    idempotency_key: str,
    request: Any,
) -> AcceptedSubmission:
    return AcceptedSubmission(
        run_id=str(existing.job_id),
        thread_id=str(thread_id),
        user_message_id=(
            str(existing.user_message_id)
            if existing.user_message_id is not None
            else None
        ),
        outbox_id=await _existing_outbox_id(db, str(existing.job_id)),
        idempotency_key=idempotency_key,
        replayed=True,
        # The winner committed this edit's tombstones, but a replay still has
        # to converge the checkpoint. A redundant reseed from the DB is safe.
        tombstoned=(getattr(request, "supersedes_client_message_id", None) is not None),
    )


async def _existing_user_message_id(
    db: AsyncSession, *, thread_id: UUID, client_message_id: Optional[UUID]
) -> Optional[str]:
    """The already-durable user row for this turn, if the insert deduped."""
    if client_message_id is None:
        return None
    stmt = (
        select(ChatMessage.id)
        .where(
            ChatMessage.thread_id == thread_id,
            ChatMessage.client_message_id == client_message_id,
            ChatMessage.role == MessageRole.USER,
        )
        .limit(1)
    )
    found = (await db.execute(stmt)).scalar_one_or_none()
    return str(found) if found is not None else None


async def _insert_user_message(
    db: AsyncSession,
    *,
    thread_id: UUID,
    user_id: Any,
    content: str,
    client_message_id: Optional[UUID],
    attachment_ids: Optional[Sequence[Any]] = None,
) -> tuple[Optional[str], bool]:
    """Insert the turn's user row idempotently. Does NOT commit.

    Returns ``(row_id, inserted)``. The flag is not cosmetic: edit-and-resend
    tombstoning must know whether it is holding a row THIS transaction created
    or one a previous attempt left behind, because the second case has to be
    validated before it may supersede anything (see
    ``apply_edit_resend_tombstones``).

    ``_persist_user_message`` (the pre-P0-C writer) used PostgreSQL's
    ``INSERT ... ON CONFLICT DO NOTHING`` against the partial unique index
    ``uq_chat_messages_thread_client_msg_user``. Here the insert runs inside a
    SAVEPOINT and a duplicate is caught as ``IntegrityError`` instead, for two
    reasons: the same unique index still decides the outcome, but this form
    yields the row id (``ON CONFLICT DO NOTHING ... RETURNING`` returns nothing
    on conflict, and ``agent_runs.user_message_id`` needs that id), and it is
    dialect-agnostic, so the fault-injection suite can exercise the real
    transaction on SQLite.

    ``attachment_ids`` are linked exactly as ``/execute`` does (R7-M5): only
    against a row THIS call inserted, and only after ownership filtering, so a
    retried turn cannot double-attach and no caller can hang another tenant's
    document off their message.
    """
    message = ChatMessage(
        thread_id=thread_id,
        user_id=user_id,
        role=MessageRole.USER,
        content=content,
        client_message_id=client_message_id,
    )
    try:
        # SAVEPOINT: a duplicate must roll back only this insert, leaving the
        # accept transaction usable for the run/event/outbox writes below.
        async with db.begin_nested():
            db.add(message)
            await db.flush()
    except IntegrityError:
        logger.debug(
            "accept_submission: user row already durable for thread %s", thread_id
        )
        existing = await _existing_user_message_id(
            db, thread_id=thread_id, client_message_id=client_message_id
        )
        return existing, False

    # Thread counters follow the insert, never the dedupe — a retried turn must
    # not inflate message_count. Expressed as SQL rather than a read-modify-write
    # so concurrent turns on one thread cannot lose an increment.
    await db.execute(
        update(Thread)
        .where(Thread.id == thread_id)
        .values(
            message_count=func.coalesce(Thread.message_count, 0) + 1,
            last_message_at=_utcnow(),
        )
        .execution_options(synchronize_session=False)
    )

    if attachment_ids:
        from src.models.message_attachment import MessageAttachment
        from src.services.threads import workspace_access

        owned_ids = await workspace_access.filter_owned_document_ids(
            db, list(attachment_ids), user_id
        )
        for doc_id in owned_ids:
            db.add(MessageAttachment(message_id=message.id, document_id=doc_id))
        await db.flush()

    return str(message.id), True


async def _apply_tombstones(
    db: AsyncSession,
    *,
    current_user: Any,
    request: Any,
    thread_id: UUID,
    user_message_id: Optional[str],
    inserted: bool,
    client_message_id: Optional[UUID],
) -> Any:  # TombstoneReport
    """Edit-and-resend: supersede the edited turn's tail. Does NOT commit.

    Joins the accept transaction deliberately. Before P0-C the tombstone pass
    rode ``_persist_user_message``'s own transaction; now the accept
    transaction is the one that writes this turn's user row, so the tombstones
    must land in it or a reader could observe the replacement question next to
    the superseded answer it was supposed to erase.

    Raises like every other statement in the accept transaction — a failure
    rolls the whole submission back and the client is told so. Swallowing it
    would accept an edit that did not take effect, which is exactly the
    fake-success shape P0-C exists to close.
    """
    from src.services.agent.agent_execution_service import (
        TombstoneReport,
        apply_edit_resend_tombstones,
    )

    report = TombstoneReport()
    supersedes = getattr(request, "supersedes_client_message_id", None)
    if supersedes is None or user_message_id is None:
        return report
    await apply_edit_resend_tombstones(
        db,
        current_user,
        thread_id=str(thread_id),
        cmid_value=(str(client_message_id) if client_message_id is not None else None),
        supersedes=supersedes,
        inserted_row_id=_coerce_uuid(user_message_id) if inserted else None,
        tombstoned_out=report,
    )
    return report


def _decrement_message_count(tombstoned: int) -> Any:
    """``message_count - tombstoned``, floored at 0.

    A CASE rather than ``GREATEST``: the accept transaction's fault-injection
    suite runs on SQLite, which has no ``greatest``, and ``max()`` is an
    aggregate under PostgreSQL.
    """
    remaining = func.coalesce(Thread.message_count, 0) - tombstoned
    return case((remaining < 0, 0), else_=remaining)


async def _ensure_thread_idle(db: AsyncSession, *, thread_id: UUID) -> None:
    """Reject active work; retire an abandoned HITL pause. Does NOT commit.

    Tenant scope: see the module docstring — ``thread_id`` comes from an
    ownership-verified thread, so every run correlated to it belongs to the
    submitting user by construction.
    """
    active = (
        await db.execute(
            select(AgentRun).where(
                AgentRun.thread_id == thread_id,
                AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
            )
        )
    ).scalar_one_or_none()
    if active is None:
        return
    if active.status == JobStatus.AWAITING_CONFIRMATION.value:
        abandoned = await abandon_awaiting_submission(
            db,
            thread_id=thread_id,
            organization_id=active.organization_id,
            user_id=active.user_id,
            reason="superseded_by_new_turn",
        )
        if abandoned is not None:
            return
    raise ActiveRunConflict("A response is already in progress for this thread.")


async def abandon_awaiting_submission(
    db: AsyncSession,
    *,
    thread_id: Any,
    organization_id: Any,
    user_id: Any,
    reason: str,
) -> Optional[str]:
    """Cancel one caller-owned parked run without committing.

    The guarded UPDATE is the claim: concurrent Stop/new-turn requests cannot
    both close the same run or append two terminal ledger events.
    """
    now = _utcnow()
    row = (
        await db.execute(
            update(AgentRun)
            .where(
                AgentRun.thread_id == _coerce_uuid(thread_id),
                AgentRun.organization_id == _coerce_uuid(organization_id),
                AgentRun.user_id == _coerce_uuid(user_id),
                AgentRun.status == JobStatus.AWAITING_CONFIRMATION.value,
            )
            .values(
                status=JobStatus.CANCELLED.value,
                completed_at=now,
                updated_at=now,
            )
            .returning(AgentRun.job_id)
            .execution_options(synchronize_session=False)
        )
    ).first()
    if row is None:
        return None
    run_id = str(row[0])
    await append_event(
        db,
        run_id=run_id,
        event_type=RunEventType.RUN_CANCELLED,
        payload={"reason": reason},
        organization_id=organization_id,
    )
    return run_id


async def request_run_cancellation(
    db: AsyncSession,
    *,
    run_id: str,
    thread_id: Any,
    organization_id: Any,
    user_id: Any,
    reason: str,
    request_id: Optional[str] = None,
) -> Optional[RunCancellationResult]:
    """Durably request cancellation of one caller-owned active run.

    The guarded update is the cancellation claim.  Only queued/running rows
    may transition to ``stopping``; a repeated request observes the existing
    stopping row and never appends a second ``run.stopping`` event.  The
    producer owns the later acknowledgement transition to ``cancelled``.
    This function deliberately does not commit so the API can combine the
    claim and its ledger event in one transaction.
    """
    now = _utcnow()
    claimed = (
        await db.execute(
            update(AgentRun)
            .where(
                AgentRun.job_id == run_id,
                AgentRun.thread_id == _coerce_uuid(thread_id),
                AgentRun.organization_id == _coerce_uuid(organization_id),
                AgentRun.user_id == _coerce_uuid(user_id),
                AgentRun.status.in_((JobStatus.QUEUED.value, JobStatus.RUNNING.value)),
            )
            .values(
                status=JobStatus.STOPPING.value,
                cancel_requested_at=now,
                updated_at=now,
            )
            .returning(AgentRun.job_id)
            .execution_options(synchronize_session=False)
        )
    ).first()
    if claimed is not None:
        stopping_payload: dict[str, Any] = {"reason": reason}
        if request_id:
            stopping_payload["request_id"] = request_id
        await append_event(
            db,
            run_id=run_id,
            event_type=RunEventType.RUN_STOPPING,
            payload=stopping_payload,
            organization_id=organization_id,
        )
        return RunCancellationResult(
            run_id=run_id,
            status=JobStatus.STOPPING,
            claimed=True,
        )

    # A request racing the first claim is idempotent.  The exact owner filters
    # below also ensure a stale or foreign request cannot discover a different
    # run on the same thread.
    current = (
        await db.execute(
            select(AgentRun.status).where(
                AgentRun.job_id == run_id,
                AgentRun.thread_id == _coerce_uuid(thread_id),
                AgentRun.organization_id == _coerce_uuid(organization_id),
                AgentRun.user_id == _coerce_uuid(user_id),
            )
        )
    ).scalar_one_or_none()
    if current is None:
        return None
    try:
        current_status = JobStatus(current)
    except ValueError:
        return None
    if current_status == JobStatus.STOPPING:
        return RunCancellationResult(
            run_id=run_id,
            status=current_status,
            claimed=False,
        )
    if current_status.is_terminal:
        return RunCancellationResult(
            run_id=run_id,
            status=current_status,
            claimed=False,
        )
    return RunCancellationResult(
        run_id=run_id,
        status=current_status,
        claimed=False,
    )


async def fail_queued_submission(
    db: AsyncSession,
    *,
    run_id: str,
    thread_id: Any,
    organization_id: Any,
    user_id: Any,
    error_code: str,
    error: str,
    payload: dict[str, Any],
) -> bool:
    """Atomically fail one caller-owned run only while it is still queued.

    The guarded update is the ownership claim between replay cleanup and the
    original dispatcher. If dispatch already moved the run to ``running``,
    this function leaves both the run and its ledger untouched and returns
    ``False``. A winning failure also retires the pending outbox intent in the
    same transaction so a future relay cannot dispatch an abandoned run.
    """
    now = _utcnow()
    try:
        claimed = (
            await db.execute(
                update(AgentRun)
                .where(
                    AgentRun.job_id == run_id,
                    AgentRun.thread_id == _coerce_uuid(thread_id),
                    AgentRun.organization_id == _coerce_uuid(organization_id),
                    AgentRun.user_id == _coerce_uuid(user_id),
                    AgentRun.status == JobStatus.QUEUED.value,
                )
                .values(
                    status=JobStatus.FAILED.value,
                    error_code=error_code,
                    error=error,
                    completed_at=now,
                    updated_at=now,
                )
                .returning(AgentRun.job_id)
                .execution_options(synchronize_session=False)
            )
        ).first()
        if claimed is None:
            await db.rollback()
            return False
        await db.execute(
            update(AgentOutbox)
            .where(
                AgentOutbox.run_id == run_id,
                AgentOutbox.organization_id == _coerce_uuid(organization_id),
                AgentOutbox.status == AgentOutboxStatus.PENDING.value,
            )
            .values(status=AgentOutboxStatus.FAILED.value, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        await append_event(
            db,
            run_id=run_id,
            event_type=RunEventType.RUN_FAILED,
            payload=payload,
            organization_id=organization_id,
        )
        await db.commit()
        return True
    except Exception:
        await db.rollback()
        raise


async def _insert_run(
    db: AsyncSession,
    *,
    job_id: str,
    organization_id: Any,
    user_id: Any,
    thread_id: UUID,
    conversation_id: Any,
    user_message_id: Optional[str],
    client_message_id: Optional[UUID],
    idempotency_key: Optional[str],
) -> AgentRun:
    """Create the run row the accepted frame names. Does NOT commit.

    Flushed (not committed) so the ledger append and the outbox insert below
    can reference ``job_id`` through their foreign keys inside this same
    transaction.

    Status is ``QUEUED`` — accepted, dispatch not yet started. The caller moves
    it to ``RUNNING`` once the graph is actually running
    (``mark_submission_dispatched``).
    """
    run = AgentRun(
        job_id=job_id,
        organization_id=_coerce_uuid(organization_id),
        user_id=_coerce_uuid(user_id),
        thread_id=thread_id,
        conversation_id=_coerce_uuid(conversation_id),
        user_message_id=_coerce_uuid(user_message_id),
        status=JobStatus.QUEUED.value,
        client_message_id=str(client_message_id) if client_message_id else None,
        idempotency_key=idempotency_key,
    )
    db.add(run)
    await db.flush()
    return run


async def _insert_outbox(
    db: AsyncSession,
    *,
    run_id: str,
    organization_id: Any,
    kind: str,
    payload: dict[str, Any],
) -> AgentOutbox:
    """Write the dispatch-intent record. Does NOT commit.

    A record, not a queue (see the module docstring): nothing polls ``pending``
    today, and the actual dispatch is unchanged.
    """
    record = AgentOutbox(
        run_id=run_id,
        organization_id=_coerce_uuid(organization_id),
        kind=kind,
        payload=payload,
        status=AgentOutboxStatus.PENDING.value,
    )
    db.add(record)
    await db.flush()
    return record


async def accept_submission(
    db: AsyncSession,
    *,
    current_user: Any,
    request: Any,  # AgentExecuteRequest
    thread: Any,  # models.thread.Thread — ownership-verified by _resolve_thread
    kind: str = DISPATCH_KIND_STREAM,
) -> AcceptedSubmission:
    """Accept a submission atomically; the caller may then emit ``accepted``.

    Commits ONE transaction containing the user message, the run row, the
    run's ``run.created`` event and the outbox dispatch record. Raises on any
    failure with the transaction rolled back — the caller must emit an
    ``error`` frame and must NOT claim acceptance.

    A resubmitted idempotency key short-circuits to the existing run
    (``replayed=True``) without writing anything, so a retried turn can never
    produce a second run, a second user row, or a second dispatch record.
    """
    thread_uuid = _coerce_uuid(getattr(thread, "id", None))
    if thread_uuid is None:
        raise ValueError("accept_submission requires an ownership-verified thread")

    organization_id = getattr(current_user, "organization_id", None)
    idempotency_key = stream_idempotency_key(request, current_user)

    last_user = next(
        (m for m in reversed(request.messages) if m.role == "user"),
        None,
    )
    if last_user is None:
        raise ValueError("accept_submission requires a user message")
    client_message_id = _coerce_uuid(getattr(last_user, "client_message_id", None))

    if idempotency_key is not None:
        existing = await _find_by_idempotency_key(
            db,
            idempotency_key,
            organization_id=organization_id,
            user_id=current_user.id,
        )
        if existing is not None:
            return await _replayed_submission(
                db,
                existing,
                thread_id=thread_uuid,
                idempotency_key=idempotency_key,
                request=request,
            )

    job_id = str(uuid4())
    try:
        try:
            await _ensure_thread_idle(db, thread_id=thread_uuid)
        except ActiveRunConflict:
            if idempotency_key is not None:
                existing = await _find_by_idempotency_key(
                    db,
                    idempotency_key,
                    organization_id=organization_id,
                    user_id=current_user.id,
                )
                if existing is not None:
                    return await _replayed_submission(
                        db,
                        existing,
                        thread_id=thread_uuid,
                        idempotency_key=idempotency_key,
                        request=request,
                    )
            raise
        user_message_id, user_row_inserted = await _insert_user_message(
            db,
            thread_id=thread_uuid,
            user_id=current_user.id,
            content=last_user.content,
            client_message_id=client_message_id,
            attachment_ids=getattr(request, "attachment_ids", None),
        )
        tombstones = await _apply_tombstones(
            db,
            current_user=current_user,
            request=request,
            thread_id=thread_uuid,
            user_message_id=user_message_id,
            inserted=user_row_inserted,
            client_message_id=client_message_id,
        )
        if tombstones.count:
            # Net delta in the SAME transaction: ``_insert_user_message``
            # already added +1 for the replacement row, so only the -N for the
            # rows it superseded is owed. Floored at 0 — message_count is a
            # denormalised counter that historically drifts, and a negative
            # count renders as nonsense in the UI.
            await db.execute(
                update(Thread)
                .where(Thread.id == thread_uuid)
                .values(message_count=_decrement_message_count(tombstones.count))
                .execution_options(synchronize_session=False)
            )
        run = await _insert_run(
            db,
            job_id=job_id,
            organization_id=organization_id,
            user_id=current_user.id,
            thread_id=thread_uuid,
            conversation_id=getattr(thread, "conversation_id", None),
            user_message_id=user_message_id,
            client_message_id=client_message_id,
            idempotency_key=idempotency_key,
        )
        await append_event(
            db,
            run_id=job_id,
            event_type=RunEventType.RUN_CREATED,
            payload={},
            organization_id=organization_id,
        )
        outbox = await _insert_outbox(
            db,
            run_id=job_id,
            organization_id=organization_id,
            kind=kind,
            payload={
                "thread_id": str(thread_uuid),
                "user_id": str(current_user.id),
                "model": getattr(request, "model", "") or "",
                "use_rag": bool(getattr(request, "use_rag", True)),
            },
        )
        outbox_id = str(outbox.id)
        await db.commit()
    except IntegrityError:
        # The idempotency index is the expected loser here: a concurrent
        # submission with the same key committed first. Re-read the winner and
        # attach to it instead of failing the turn.
        await db.rollback()
        if idempotency_key is not None:
            winner = await _find_by_idempotency_key(
                db,
                idempotency_key,
                organization_id=organization_id,
                user_id=current_user.id,
            )
            if winner is not None:
                logger.info(
                    "accept_submission: idempotency race lost — attaching to run %s",
                    winner.job_id,
                )
                return await _replayed_submission(
                    db,
                    winner,
                    thread_id=thread_uuid,
                    idempotency_key=idempotency_key,
                    request=request,
                )
        active_job_id = (
            await db.execute(
                select(AgentRun.job_id).where(
                    AgentRun.thread_id == thread_uuid,
                    AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
                )
            )
        ).scalar_one_or_none()
        if active_job_id is not None:
            raise ActiveRunConflict(
                "A response is already in progress for this thread."
            ) from None
        raise
    except Exception:
        # Nothing partial may survive: no run row without its event, no user
        # row without a run, no acknowledgment without any of them.
        await db.rollback()
        raise

    return AcceptedSubmission(
        run_id=str(run.job_id),
        thread_id=str(thread_uuid),
        user_message_id=user_message_id,
        outbox_id=outbox_id,
        idempotency_key=idempotency_key,
        tombstoned=tombstones.any,
        tombstoned_count=tombstones.count,
    )


async def mark_submission_dispatched(
    db: AsyncSession,
    *,
    run_id: str,
    outbox_id: Optional[str],
    organization_id: Any,
    user_id: Any,
) -> bool:
    """Atomically claim an accepted run for execution and commit.

    The ``queued`` → ``running`` update is the execution claim. ``False`` means
    a replay already abandoned the run, so the caller must not enter the
    graph/LLM. Durable-state failures raise so the stream's error path can
    terminalize the accepted run. The run is locked before its outbox row,
    matching :func:`fail_queued_submission` and avoiding a deadlock when those
    two claims race.
    """
    now = _utcnow()
    try:
        claimed = (
            await db.execute(
                update(AgentRun)
                .where(
                    AgentRun.job_id == run_id,
                    AgentRun.organization_id == _coerce_uuid(organization_id),
                    AgentRun.user_id == _coerce_uuid(user_id),
                    AgentRun.status == JobStatus.QUEUED.value,
                )
                .values(
                    status=JobStatus.RUNNING.value,
                    started_at=now,
                    updated_at=now,
                )
                .returning(AgentRun.job_id)
                .execution_options(synchronize_session=False)
            )
        ).first()
        if claimed is None:
            await db.rollback()
            return False
        if outbox_id is not None:
            await db.execute(
                update(AgentOutbox)
                .where(
                    AgentOutbox.id == _coerce_uuid(outbox_id),
                    AgentOutbox.run_id == run_id,
                    AgentOutbox.organization_id == _coerce_uuid(organization_id),
                    AgentOutbox.status == AgentOutboxStatus.PENDING.value,
                )
                .values(
                    status=AgentOutboxStatus.DISPATCHED.value,
                    dispatched_at=now,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
        await append_event(
            db,
            run_id=run_id,
            event_type=RunEventType.RUN_STARTED,
            payload={},
            organization_id=organization_id,
        )
        await db.commit()
        return True
    except Exception:
        logger.warning(
            "mark_submission_dispatched failed for run %s", run_id, exc_info=True
        )
        try:
            await db.rollback()
        except Exception:
            logger.debug("rollback after dispatch stamp failure failed", exc_info=True)
        raise


# One retry only: a second consecutive disconnect means the database is
# genuinely unreachable, and the stale-run sweeper is the correct backstop.
_FINALIZE_ATTEMPTS = 2
# Breather before that retry. The observed incident was a node-level network
# blip that killed the Redis and Postgres sockets in the same second, so an
# instant retry tends to land on the same dead network. Tests zero this out.
_FINALIZE_RETRY_BACKOFF_S = 0.2


async def finalize_submission(
    db: AsyncSession,
    *,
    run_id: str,
    status: JobStatus,
    organization_id: Any,
    event_type: Optional[RunEventType] = None,
    payload: Optional[dict[str, Any]] = None,
    error_code: Optional[str] = None,
    error: Optional[str] = None,
    run_metadata: Optional[dict[str, Any]] = None,
) -> bool:
    """Move the run to *status* at the end of the turn and commit.

    The run row must reach a terminal status or ``uq_agent_runs_active_thread``
    would leave a phantom active run that rejects the next turn, and the
    stale-run sweeper would eventually mark a perfectly successful turn
    ``failed``.

    ``event_type`` is optional because not every exit is terminal: a turn
    parked on a HITL confirmation moves to ``awaiting_confirmation`` and its
    ledger must stay OPEN — the run genuinely continues on ``/stream/confirm``.

    Terminal failures propagate so callers cannot emit ``done`` while the
    thread's single-writer slot is still held. Non-terminal HITL parking stays
    best-effort: the checkpoint remains the authoritative resume state.
    """
    now = _utcnow()
    values: dict[str, Any] = {
        "status": status.value,
        "error_code": error_code,
        "error": error,
        "updated_at": now,
    }
    if status in TERMINAL_JOB_STATUSES:
        values["completed_at"] = now
    if run_metadata is not None:
        values["run_metadata"] = run_metadata
    # Project the transcript linkage onto the run row itself. Terminal
    # payloads (run.completed / run.cancelled) carry the persisted assistant
    # row id; without this the AgentRun.assistant_message_id FK stays NULL
    # forever and consumers must dig through event JSONB. Guarded to valid
    # UUIDs: the column is a GUID FK and unit-test doubles pass opaque ids.
    raw_assistant_id = (payload or {}).get("assistant_message_id")
    if raw_assistant_id:
        try:
            values["assistant_message_id"] = UUID(str(raw_assistant_id))
        except (ValueError, AttributeError, TypeError):
            logger.debug(
                "finalize_submission: non-UUID assistant_message_id %r ignored",
                raw_assistant_id,
            )
    # A pooled connection dropped by the pooler must not turn a finished turn
    # into a failed one: this runs after the answer was already streamed, and a
    # raise here leaves the run non-terminal so the thread's single-writer slot
    # keeps rejecting the user's next turn. The write is idempotent — the
    # UPDATE is guarded on a non-terminal status and append_event absorbs
    # RunAlreadyTerminalError — so a lost connection is rolled back and retried
    # once. SQLAlchemy has already discarded the dead connection by then, so
    # the retry checks out a fresh one (see pool_pre_ping in src/core/database).
    for attempt in range(_FINALIZE_ATTEMPTS):
        try:
            transition = (
                update(AgentRun)
                .where(
                    AgentRun.job_id == run_id,
                    AgentRun.status.notin_(_TERMINAL_RUN_STATUSES),
                    # A producer that observed the durable stop marker must
                    # acknowledge it as cancelled; it may never publish a
                    # late completed/failed transition over ``stopping``.
                    *(
                        []
                        if status == JobStatus.CANCELLED
                        else [AgentRun.status != JobStatus.STOPPING.value]
                    ),
                )
                .values(**values)
                .returning(AgentRun.job_id)
                .execution_options(synchronize_session=False)
            )
            transition_result = await db.execute(transition)
            if transition_result is None:
                # A few lightweight test doubles model execute() as a write
                # with no result object. Real SQLAlchemy UPDATE..RETURNING
                # always supplies one; treating the double as a successful
                # transition preserves the pre-returning contract.
                transitioned = True
            else:
                first = getattr(transition_result, "first", None)
                transitioned = first() if callable(first) else transition_result
                if inspect.isawaitable(transitioned):
                    transitioned = await transitioned
            if transitioned is None:
                # The guarded UPDATE is the ownership/race decision. A
                # stopping or terminal row is already owned by another
                # terminalizer; there is no event to append from this call.
                await db.commit()
                return False
            if event_type is not None:
                try:
                    await append_event(
                        db,
                        run_id=run_id,
                        event_type=event_type,
                        payload=payload or {},
                        organization_id=organization_id,
                    )
                except RunAlreadyTerminalError:
                    # Absorbing terminal ledger — another writer closed it first.
                    logger.debug(
                        "finalize_submission: ledger already closed for %s", run_id
                    )
            await db.commit()
            return True
        except Exception as exc:
            try:
                await db.rollback()
            except Exception:
                logger.debug("rollback after finalize failure failed", exc_info=True)
            orig = getattr(exc, "orig", None)
            lost_connection = (
                bool(getattr(exc, "connection_invalidated", False))
                or isinstance(orig, ConnectionDoesNotExistError)
                or isinstance(
                    getattr(orig, "__cause__", None), ConnectionDoesNotExistError
                )
            )
            if lost_connection and attempt + 1 < _FINALIZE_ATTEMPTS:
                logger.warning(
                    "finalize_submission lost its connection for run %s, retrying",
                    run_id,
                )
                await asyncio.sleep(_FINALIZE_RETRY_BACKOFF_S)
                continue
            logger.warning(
                "finalize_submission failed for run %s", run_id, exc_info=True
            )
            if status in TERMINAL_JOB_STATUSES:
                raise
            return False
    return False


__all__ = [
    "DISPATCH_KIND_STREAM",
    "AcceptedSubmission",
    "RunCancellationResult",
    "accept_submission",
    "fail_queued_submission",
    "finalize_submission",
    "mark_submission_dispatched",
    "request_run_cancellation",
    "stream_idempotency_key",
]
