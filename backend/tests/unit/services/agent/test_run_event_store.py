"""Contract tests for the append-only run-event ledger (P0-B).

The ledger's guarantees are: server-allocated gapless ``seq`` per run, one
terminal event per run ever, and tenant-scoped reads that are null-safe (an
org-less caller must not see another tenant's rows, and ``organization_id``
must never be stringified into ``"None"`` — that exact bug has merged tenants
in this codebase before).

True cross-connection contention needs PostgreSQL, which the unit suite does
not have. Contention is therefore covered three ways here: the allocation SQL
shape (``COALESCE(MAX(seq), 0) + 1`` computed inside the INSERT), the retry
path driven by an injected ``IntegrityError``, and the unique constraint that
backstops both.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy import Index, Insert, Select, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.agent_run import AgentRun
from src.models.agent_run_event import _TERMINAL_EVENT_PREDICATE, AgentRunEvent
from src.services.agent.run_event_store import (
    RunAlreadyTerminalError,
    SequenceContentionError,
    append_event,
    has_terminal_event,
    read_events,
)
from src.services.agent.run_event_types import TERMINAL_RUN_EVENTS, RunEventType

pytestmark = pytest.mark.unit

ORG_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
ORG_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER_A = uuid.UUID("33333333-3333-3333-3333-333333333333")
USER_B = uuid.UUID("44444444-4444-4444-4444-444444444444")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    # pysqlite/aiosqlite emit an implicit COMMIT before SAVEPOINT unless the
    # driver's transaction handling is disabled and BEGIN is emitted by hand
    # (SQLAlchemy's documented sqlite caveat). Without this the store's
    # savepoint-scoped retry would silently commit the caller's transaction
    # here and the transaction-ownership assertions would test nothing.
    @event.listens_for(engine.sync_engine, "connect")
    def _disable_driver_transactions(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _emit_begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(AgentRun.__table__.create)
        await conn.run_sync(AgentRunEvent.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def db(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


def _index(name: str) -> Index:
    """Look the index up by name — a clear miss beats a bare StopIteration."""
    matches = [idx for idx in AgentRunEvent.__table__.indexes if idx.name == name]
    assert matches, f"index {name} is missing from AgentRunEvent"
    return cast(Index, matches[0])


async def _make_run(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID | None,
    user_id: uuid.UUID = USER_A,
) -> str:
    job_id = str(uuid.uuid4())
    db.add(
        AgentRun(
            job_id=job_id,
            status="running",
            user_id=user_id,
            organization_id=organization_id,
        )
    )
    await db.commit()
    return job_id


class _RecordingSession:
    """AsyncSession proxy: records statements, optionally fails INSERTs.

    ``fail_inserts`` raises ``IntegrityError`` for the first N INSERT
    executions, standing in for the unique-constraint violation a concurrent
    appender causes on PostgreSQL.
    """

    def __init__(self, db: AsyncSession, *, fail_inserts: int = 0) -> None:
        self._db = db
        self._fail_inserts = fail_inserts
        self.insert_attempts = 0
        self.statements: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        self.statements.append(statement)
        if isinstance(statement, Insert):
            self.insert_attempts += 1
            if self.insert_attempts <= self._fail_inserts:
                raise IntegrityError(
                    "INSERT INTO agent_run_events",
                    {},
                    Exception("uq_agent_run_events_run_seq"),
                )
        return await self._db.execute(statement, *args, **kwargs)

    def compiled(self, statement_type: type, *, literal_binds: bool = True) -> str:
        """SQL of the last statement of *statement_type*.

        ``literal_binds`` inlines parameter values, which is what turns a
        stringified tenant id into visible evidence. It is disabled for
        INSERTs — JSONB payloads have no literal renderer.
        """
        match: Any = [s for s in self.statements if isinstance(s, statement_type)][-1]
        return str(match.compile(compile_kwargs={"literal_binds": literal_binds}))


# ---------------------------------------------------------------------------
# Sequence allocation / concurrency
# ---------------------------------------------------------------------------


async def test_run_event_store_concurrency(db: AsyncSession) -> None:
    """Appends allocate gapless, unique, server-side sequences — and the
    allocation survives a lost race (injected IntegrityError) without
    skipping or reusing a number."""
    run_id = await _make_run(db, organization_id=ORG_A)

    seqs = [
        (
            await append_event(
                db,
                run_id=run_id,
                event_type=RunEventType.ASSISTANT_DELTA,
                payload={"text": f"chunk {i}"},
                organization_id=ORG_A,
            )
        ).seq
        for i in range(5)
    ]
    await db.commit()
    assert seqs == [1, 2, 3, 4, 5]
    assert len(set(seqs)) == len(seqs)

    # The next seq is computed by the database inside the INSERT, not by the
    # caller: a client-supplied seq is impossible by construction.
    recorder = _RecordingSession(db)
    await append_event(
        cast(AsyncSession, recorder),
        run_id=run_id,
        event_type=RunEventType.USAGE_UPDATED,
        payload={"total_tokens": 7},
        organization_id=ORG_A,
    )
    sql = " ".join(recorder.compiled(Insert, literal_binds=False).lower().split())
    assert "(select coalesce(max(agent_run_events.seq)" in sql
    assert "from agent_run_events where agent_run_events.run_id = :run_id_1)" in sql
    # seq must NOT be a bound parameter — that would mean the caller supplied it.
    assert ":seq" not in sql

    # Lost race: the first INSERT raises IntegrityError (the unique constraint
    # firing on PostgreSQL). The retry must succeed with the NEXT free seq —
    # no gap, no duplicate.
    flaky = _RecordingSession(db, fail_inserts=1)
    retried = await append_event(
        cast(AsyncSession, flaky),
        run_id=run_id,
        event_type=RunEventType.RUN_STOPPING,
        payload={},
        organization_id=ORG_A,
    )
    await db.commit()
    assert flaky.insert_attempts == 2
    assert retried.seq == 7

    stored = await read_events(db, run_id, organization_id=ORG_A, user_id=USER_A)
    assert [e.seq for e in stored] == [1, 2, 3, 4, 5, 6, 7]


async def test_append_gives_up_instead_of_guessing_a_sequence(
    db: AsyncSession,
) -> None:
    """Exhausted retries raise rather than skip a seq — a gap in the ledger is
    worse than a failed append."""
    run_id = await _make_run(db, organization_id=ORG_A)
    always_failing = _RecordingSession(db, fail_inserts=99)

    with pytest.raises(SequenceContentionError):
        await append_event(
            cast(AsyncSession, always_failing),
            run_id=run_id,
            event_type=RunEventType.RUN_STARTED,
            payload={},
            organization_id=ORG_A,
        )
    assert always_failing.insert_attempts == 2


async def test_append_advances_the_run_high_water_mark(db: AsyncSession) -> None:
    run_id = await _make_run(db, organization_id=ORG_A)
    for _ in range(3):
        await append_event(
            db,
            run_id=run_id,
            event_type=RunEventType.ASSISTANT_DELTA,
            payload={"text": "x"},
            organization_id=ORG_A,
        )
    await db.commit()
    run = await db.get(AgentRun, run_id)
    assert run is not None
    assert run.last_event_seq == 3


async def test_append_does_not_commit(db: AsyncSession) -> None:
    """The caller owns the transaction: a rollback must discard the append."""
    run_id = await _make_run(db, organization_id=ORG_A)
    await append_event(
        db,
        run_id=run_id,
        event_type=RunEventType.RUN_STARTED,
        payload={},
        organization_id=ORG_A,
    )
    await db.rollback()
    assert await read_events(db, run_id, organization_id=ORG_A, user_id=USER_A) == []


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


async def test_run_event_org_scope_null_safe(db: AsyncSession) -> None:
    """An org-less read filters IS NULL — it must never stringify to "None",
    and it must not see another tenant's events."""
    run_id = await _make_run(db, organization_id=None)
    for org in (ORG_A, ORG_B, None):
        await append_event(
            db,
            run_id=run_id,
            event_type=RunEventType.ASSISTANT_DELTA,
            payload={"text": str(org)},
            organization_id=org,
        )
    await db.commit()

    orgless = await read_events(db, run_id, organization_id=None, user_id=USER_A)
    assert [e.organization_id for e in orgless] == [None]

    scoped = await read_events(db, run_id, organization_id=ORG_A, user_id=USER_A)
    assert [e.organization_id for e in scoped] == [ORG_A]

    # Compile-level proof: the org-less predicate is IS NULL, and the literal
    # string "None" appears nowhere in the emitted SQL.
    recorder = _RecordingSession(db)
    await read_events(
        cast(AsyncSession, recorder), run_id, organization_id=None, user_id=USER_A
    )
    sql = recorder.compiled(Select)
    assert "agent_run_events.organization_id IS NULL" in sql
    assert "'None'" not in sql
    assert "None" not in sql

    # A garbage org id degrades to IS NULL (fail-closed), never to a string.
    recorder = _RecordingSession(db)
    await read_events(
        cast(AsyncSession, recorder),
        run_id,
        organization_id="not-a-uuid",
        user_id=USER_A,
    )
    assert "agent_run_events.organization_id IS NULL" in recorder.compiled(Select)


async def test_append_writes_org_scope_null_safely(db: AsyncSession) -> None:
    run_id = await _make_run(db, organization_id=ORG_A)
    event = await append_event(
        db,
        run_id=run_id,
        event_type=RunEventType.RUN_STARTED,
        payload={},
        organization_id=str(ORG_A),
    )
    await db.commit()
    assert event.organization_id == ORG_A

    orgless = await append_event(
        db,
        run_id=run_id,
        event_type=RunEventType.PLAN_UPDATED,
        payload={"steps": []},
        organization_id=None,
    )
    await db.commit()
    assert orgless.organization_id is None


async def test_run_event_seq_unique_across_null_org(db: AsyncSession) -> None:
    """The unique key is (run_id, seq). organization_id is NOT part of it —
    NULLs are distinct in a PG unique constraint, so an org in the key would
    let org-less rows repeat a seq for the same run."""
    constraints = [
        c
        for c in AgentRunEvent.__table__.constraints
        if c.name == "uq_agent_run_events_run_seq"
    ]
    assert constraints, "uq_agent_run_events_run_seq is missing from AgentRunEvent"
    constraint = constraints[0]
    assert [c.name for c in constraint.columns] == ["run_id", "seq"]
    assert "organization_id" not in {c.name for c in constraint.columns}

    # Behavioural proof: same (run_id, seq) under two DIFFERENT orgs still
    # violates the constraint.
    run_id = await _make_run(db, organization_id=ORG_A)
    db.add(
        AgentRunEvent(
            run_id=run_id,
            seq=1,
            event_type=RunEventType.RUN_STARTED.value,
            payload={},
            organization_id=ORG_A,
        )
    )
    await db.commit()
    db.add(
        AgentRunEvent(
            run_id=run_id,
            seq=1,
            event_type=RunEventType.PLAN_UPDATED.value,
            payload={},
            organization_id=ORG_B,
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_org_scope_index_exists_for_tenant_reads() -> None:
    index = _index("idx_agent_run_events_org_run")
    assert [c.name for c in index.columns] == ["organization_id", "run_id"]
    assert not index.unique


# ---------------------------------------------------------------------------
# Terminal invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal", sorted(TERMINAL_RUN_EVENTS))
async def test_append_after_terminal_event_is_refused(
    db: AsyncSession, terminal: RunEventType
) -> None:
    run_id = await _make_run(db, organization_id=ORG_A)
    payloads: dict[RunEventType, dict[str, Any]] = {
        RunEventType.RUN_FAILED: {"code": "boom", "message": "boom"},
    }
    await append_event(
        db,
        run_id=run_id,
        event_type=terminal,
        payload=payloads.get(terminal, {}),
        organization_id=ORG_A,
    )
    await db.commit()
    assert await has_terminal_event(db, run_id) is True

    with pytest.raises(RunAlreadyTerminalError):
        await append_event(
            db,
            run_id=run_id,
            event_type=RunEventType.ASSISTANT_DELTA,
            payload={"text": "after the end"},
            organization_id=ORG_A,
        )
    # ...including a second terminal event.
    with pytest.raises(RunAlreadyTerminalError):
        await append_event(
            db,
            run_id=run_id,
            event_type=RunEventType.RUN_CANCELLED,
            payload={},
            organization_id=ORG_A,
        )
    assert (
        len(await read_events(db, run_id, organization_id=ORG_A, user_id=USER_A)) == 1
    )


async def test_one_terminal_event_per_run_is_a_database_invariant(
    db: AsyncSession,
) -> None:
    """The service check races; the partial unique index does not."""
    index = _index("uq_agent_run_events_one_terminal")
    assert index.unique
    assert [c.name for c in index.columns] == ["run_id"]
    where = str(index.dialect_options["postgresql"]["where"])
    # The predicate must cover exactly the terminal vocabulary — no more, no
    # less — or a "terminal" event escapes the invariant.
    assert {
        value.strip().strip("'") for value in where.split("(")[1].rstrip(")").split(",")
    } == {terminal.value for terminal in TERMINAL_RUN_EVENTS}

    run_id = await _make_run(db, organization_id=ORG_A)
    db.add(
        AgentRunEvent(
            run_id=run_id,
            seq=1,
            event_type=RunEventType.RUN_COMPLETED.value,
            payload={},
        )
    )
    await db.commit()
    db.add(
        AgentRunEvent(
            run_id=run_id,
            seq=2,
            event_type=RunEventType.RUN_FAILED.value,
            payload={"code": "x", "message": "y"},
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_terminal_predicate_matches_the_event_vocabulary() -> None:
    """The model spells the terminal set as SQL (models cannot import
    services); it must stay identical to TERMINAL_RUN_EVENTS."""
    for terminal_event in TERMINAL_RUN_EVENTS:
        assert f"'{terminal_event.value}'" in _TERMINAL_EVENT_PREDICATE
    assert _TERMINAL_EVENT_PREDICATE.count("'") == 2 * len(TERMINAL_RUN_EVENTS)


# ---------------------------------------------------------------------------
# Payload gate
# ---------------------------------------------------------------------------


async def test_unknown_event_type_never_reaches_the_ledger(db: AsyncSession) -> None:
    run_id = await _make_run(db, organization_id=ORG_A)
    with pytest.raises(ValueError):
        await append_event(
            db,
            run_id=run_id,
            event_type="langgraph.on_chat_model_stream",
            payload={},
            organization_id=ORG_A,
        )
    assert (
        await db.execute(select(AgentRunEvent).where(AgentRunEvent.run_id == run_id))
    ).first() is None


async def test_payload_is_validated_and_redacted_before_persistence(
    db: AsyncSession,
) -> None:
    run_id = await _make_run(db, organization_id=ORG_A)
    appended = await append_event(
        db,
        run_id=run_id,
        event_type=RunEventType.TOOL_STARTED,
        payload={
            "tool_call_id": "call_1",
            "name": "search_documents",
            "args": {"query": "papers by alice@example.com"},
        },
        organization_id=ORG_A,
    )
    await db.commit()
    assert "alice@example.com" not in appended.payload["args"]["query"]
    assert "<email>" in appended.payload["args"]["query"]

    with pytest.raises(ValidationError):
        await append_event(
            db,
            run_id=run_id,
            event_type=RunEventType.TOOL_STARTED,
            payload={"unexpected": "field"},
            organization_id=ORG_A,
        )


async def test_read_events_is_scoped_to_the_owning_user(db: AsyncSession) -> None:
    """R7-L7: org scope alone let any colleague replay another user's prompts."""
    run_id = await _make_run(db, organization_id=ORG_A, user_id=USER_A)
    await append_event(
        db,
        run_id=run_id,
        event_type=RunEventType.ASSISTANT_DELTA,
        payload={"text": "private"},
        organization_id=ORG_A,
    )
    await db.commit()

    assert (
        len(await read_events(db, run_id, organization_id=ORG_A, user_id=USER_A)) == 1
    )
    assert await read_events(db, run_id, organization_id=ORG_A, user_id=USER_B) == []
