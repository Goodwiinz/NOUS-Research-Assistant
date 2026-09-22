"""Owner of ALL access to the ``agent_run_events`` append-only ledger (P0-B).

PostgreSQL is authoritative for a run's event history: replay, reconnect and
audit read this table, and Redis only wakes subscribers. Rows are immutable —
this module appends and reads, it never updates or deletes an event. Nothing
else may query the table (same rule as ``agent_run_service`` for ``agent_runs``).

Design rules
------------

**Sequence allocation is server-side.** Clients never supply ``seq``. Each
append computes ``COALESCE(MAX(seq), 0) + 1`` for the run *inside the INSERT*
and relies on ``uq_agent_run_events_run_seq`` as the backstop. Concurrency
strategy, explicitly:

- Two transactions racing on the same run compute the same ``seq`` under
  READ COMMITTED (PostgreSQL's default). The loser blocks on the unique index
  until the winner commits, then raises ``IntegrityError``.
- The insert runs inside a SAVEPOINT (``begin_nested``), so that failure
  rolls back only the failed insert — the caller's transaction survives — and
  the retry re-reads a snapshot that now *includes* the winner's row, so it
  computes the next free ``seq``.
- One retry (``_MAX_APPEND_ATTEMPTS``). Exhausting it raises
  ``SequenceContentionError`` rather than skipping or guessing a ``seq``;
  gaplessness is a contract, so failing loudly beats a silent hole. In
  practice a run has one live writer plus, at most, a terminalizer.
- Under REPEATABLE READ / SERIALIZABLE the retry would re-read the same stale
  snapshot and fail again; callers using those isolation levels must retry the
  whole transaction.

A row-lock alternative (``SELECT ... FOR UPDATE`` on ``agent_runs``) was
rejected: it serializes every append behind the run row even when there is no
contention, and it cannot be exercised by the sqlite-backed unit suite, so the
allocation path would ship untested.

**Terminal events are absorbing.** Once ``run.completed`` / ``run.failed`` /
``run.cancelled`` exists for a run, ``append_event`` refuses to append
(``RunAlreadyTerminalError``). The read-then-write check alone races, so the
partial unique index ``uq_agent_run_events_one_terminal`` enforces the same
invariant in the database; a losing racer's ``IntegrityError`` is re-classified
into ``RunAlreadyTerminalError`` on the retry pass.

**Tenancy.** ``organization_id`` is nullable (org-less users exist) and is
compared null-safely — ``== None`` compiles to ``IS NULL``. It is NEVER
stringified: ``str(None) == "None"`` has merged tenants in this codebase
before. Both ``append_event`` and ``read_events`` take ``organization_id`` as a
required keyword argument so a caller cannot silently omit the scope, and
``read_events`` additionally requires ``user_id`` — events have no owner
column, so the scope is joined off ``agent_runs.user_id``.

**The caller owns the transaction.** These primitives never commit and never
roll back the outer transaction, so an append can be made atomic with the
message/run writes that accompany it (P0-C).
"""

from __future__ import annotations

import logging
from typing import Any, Optional, cast
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.services.agent.run_event_types import (
    TERMINAL_RUN_EVENTS,
    RunEventType,
    validate_payload,
)

logger = logging.getLogger(__name__)

# Initial attempt + one retry. See the module docstring for why this is
# sufficient and why exhaustion raises instead of guessing.
_MAX_APPEND_ATTEMPTS = 2

# Replay page size — bounded so a pathological run cannot materialize its whole
# history into one response.
DEFAULT_READ_LIMIT = 500

_TERMINAL_VALUES = frozenset(event.value for event in TERMINAL_RUN_EVENTS)


class RunEventStoreError(RuntimeError):
    """Base class for ledger errors the caller is expected to handle."""


class RunAlreadyTerminalError(RunEventStoreError):
    """A terminal event already exists for the run — the ledger is closed."""


class SequenceContentionError(RunEventStoreError):
    """Sequence allocation lost its race repeatedly; retry the transaction."""


def _coerce_uuid(value: Any) -> Optional[UUID]:
    """Best-effort UUID coercion — callers carry ids as strings or UUIDs.

    Never ``str()``: an unparseable or missing org must become ``None`` (which
    filters ``IS NULL``), not the literal string ``"None"``.
    """
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


async def has_terminal_event(db: AsyncSession, run_id: str) -> bool:
    """True when the run's ledger is already closed by a terminal event.

    Integrity guard, not a data read: it is keyed only by the server-generated
    ``run_id`` and returns a bool, so it carries no tenant scope.
    """
    stmt = (
        select(AgentRunEvent.id)
        .where(
            AgentRunEvent.run_id == run_id,
            AgentRunEvent.event_type.in_(_TERMINAL_VALUES),
        )
        .limit(1)
    )
    return (await db.execute(stmt)).first() is not None


async def append_event(
    db: AsyncSession,
    *,
    run_id: str,
    event_type: RunEventType | str,
    payload: Optional[dict[str, Any]] = None,
    organization_id: Any,
) -> AgentRunEvent:
    """Append one immutable fact to *run_id*'s ledger. Does NOT commit.

    ``seq`` is allocated by the store (never by the caller) and the payload is
    validated/redacted/bounded through ``run_event_types.validate_payload``
    before it can reach durable JSONB.

    Raises ``ValueError`` for an unknown event type, ``ValidationError`` /
    ``PayloadTooLargeError`` for a bad payload, ``RunAlreadyTerminalError``
    once the run is terminal, and ``SequenceContentionError`` if sequence
    allocation loses its race repeatedly.
    """
    typed = RunEventType(event_type)  # ValueError on unknown — never persist it
    validated = validate_payload(typed, payload or {})
    org_uuid = _coerce_uuid(organization_id)

    next_seq = (
        select(func.coalesce(func.max(AgentRunEvent.seq), 0) + 1)
        .where(AgentRunEvent.run_id == run_id)
        .scalar_subquery()
    )
    stmt = (
        insert(AgentRunEvent)
        .values(
            run_id=run_id,
            organization_id=org_uuid,
            seq=next_seq,
            event_type=typed.value,
            payload=validated,
        )
        .returning(AgentRunEvent)
    )

    for attempt in range(_MAX_APPEND_ATTEMPTS):
        if await has_terminal_event(db, run_id):
            raise RunAlreadyTerminalError(
                f"run {run_id} already has a terminal event; "
                f"refusing to append {typed.value}"
            )
        try:
            # SAVEPOINT: a unique violation here must not poison the caller's
            # transaction, and the retry must see the winner's committed row.
            async with db.begin_nested():
                event: AgentRunEvent = (await db.execute(stmt)).scalar_one()
                # Legacy ``Column``-style model: ``.seq`` is typed as the
                # Column descriptor, the instance attribute is the int.
                await _bump_last_event_seq(db, run_id, cast(int, event.seq))
        except IntegrityError:
            logger.debug(
                "run_event_store: seq contention on run %s (attempt %d)",
                run_id,
                attempt + 1,
            )
            continue
        return event

    raise SequenceContentionError(
        f"could not allocate a sequence for run {run_id} after "
        f"{_MAX_APPEND_ATTEMPTS} attempts"
    )


async def _bump_last_event_seq(db: AsyncSession, run_id: str, seq: int) -> None:
    """Advance the run's high-water mark, monotonically.

    Guarded by ``last_event_seq < seq`` so a late/duplicate append can only
    move the cursor forward — a resume cursor must never go backwards.
    """
    await db.execute(
        update(AgentRun)
        .where(AgentRun.job_id == run_id, AgentRun.last_event_seq < seq)
        .values(last_event_seq=seq)
        .execution_options(synchronize_session=False)
    )


async def read_events(
    db: AsyncSession,
    run_id: str,
    *,
    organization_id: Any,
    user_id: Any,
    after_seq: int = 0,
    limit: int = DEFAULT_READ_LIMIT,
) -> list[AgentRunEvent]:
    """Ordered replay of a run's ledger after *after_seq*, caller-scoped.

    ``organization_id`` is mandatory and null-safe: ``None`` compiles to
    ``IS NULL``, so an org-less caller sees only org-less rows and never
    another tenant's history.

    ``user_id`` is mandatory too (R7-L7). Events carry no owner column, so the
    predicate comes from the run they belong to: org scope alone would let any
    member of a shared organization replay a colleague's prompts and tool args
    given a run id.
    """
    stmt = (
        select(AgentRunEvent)
        .join(AgentRun, AgentRun.job_id == AgentRunEvent.run_id)
        .where(
            AgentRunEvent.run_id == run_id,
            AgentRunEvent.organization_id == _coerce_uuid(organization_id),
            AgentRun.user_id == _coerce_uuid(user_id),
            AgentRunEvent.seq > after_seq,
        )
        .order_by(AgentRunEvent.seq.asc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


__all__ = [
    "DEFAULT_READ_LIMIT",
    "RunAlreadyTerminalError",
    "RunEventStoreError",
    "SequenceContentionError",
    "append_event",
    "has_terminal_event",
    "read_events",
]
