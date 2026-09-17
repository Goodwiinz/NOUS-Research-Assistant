"""Contract tests for atomic submission acceptance (P0-C).

The guarantee under test: an ``accepted`` acknowledgment is a claim about
durable state, so the user message, the run row, the run's ``run.created``
event and the outbox dispatch record either all exist or none do, and a
resubmitted idempotency key attaches to the run it already created rather than
producing a second one.

Everything runs against SQLite, which carries the same partial unique indexes
the accept path depends on (``uq_agent_runs_user_idempotency_key``,
``uq_agent_runs_active_thread``, ``uq_agent_outbox_run``) because the models
declare ``sqlite_where`` alongside ``postgresql_where``. True cross-connection
contention needs PostgreSQL; the race is therefore driven deterministically by
suppressing the pre-insert lookup, which is exactly the state a losing racer
sees — its snapshot predates the winner's commit — and lets the real
``IntegrityError`` recovery run.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.chat_message import ChatMessage, MessageRole
from src.models.conversation import Conversation
from src.models.message_attachment import MessageAttachment
from src.models.thread import Thread
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.services.agent import agent_submission_service as submission_mod
from src.services.agent.agent_run_service import ActiveRunConflict
from src.services.agent.agent_submission_service import (
    abandon_awaiting_submission,
    accept_submission,
    mark_submission_dispatched,
    request_run_cancellation,
    stream_idempotency_key,
)
from src.services.agent.run_event_types import RunEventType
from src.shared.enums import AgentOutboxStatus, JobStatus

pytestmark = pytest.mark.unit

ORG_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
ORG_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER_A = uuid.UUID("33333333-3333-3333-3333-333333333333")
USER_B = uuid.UUID("44444444-4444-4444-4444-444444444444")
THREAD_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
CONVERSATION_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
WORKSPACE_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    # pysqlite/aiosqlite emit an implicit COMMIT before SAVEPOINT unless driver
    # transaction handling is disabled and BEGIN is emitted by hand (SQLAlchemy's
    # documented sqlite caveat). Without this the savepoint-scoped inserts would
    # silently commit the accept transaction and the rollback assertions below
    # would test nothing.
    @event.listens_for(engine.sync_engine, "connect")
    def _disable_driver_transactions(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _emit_begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Thread.__table__.create)
        await conn.run_sync(ChatMessage.__table__.create)
        # Edit-and-resend's tombstone pass re-verifies thread ownership through
        # Conversation -> Workspace.owner_id, so those tables have to exist for
        # it to supersede anything.
        await conn.run_sync(Workspace.__table__.create)
        await conn.run_sync(WorkspaceMember.__table__.create)
        await conn.run_sync(Conversation.__table__.create)
        await conn.run_sync(AgentRun.__table__.create)
        await conn.run_sync(AgentRunEvent.__table__.create)
        await conn.run_sync(AgentOutbox.__table__.create)
        await conn.run_sync(MessageAttachment.__table__.create)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def db(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        session.add(
            Workspace(
                id=WORKSPACE_ID,
                name="w",
                owner_id=USER_A,
                organization_id=ORG_A,
            )
        )
        session.add(
            Conversation(
                id=CONVERSATION_ID,
                workspace_id=WORKSPACE_ID,
                title="c",
                created_by_id=USER_A,
            )
        )
        session.add(
            Thread(
                id=THREAD_ID,
                conversation_id=CONVERSATION_ID,
                title="t",
                created_by_id=USER_A,
                message_count=0,
            )
        )
        await session.commit()
        yield session


def _user(user_id: uuid.UUID = USER_A, org: uuid.UUID | None = ORG_A) -> Any:
    return SimpleNamespace(id=user_id, organization_id=org)


def _request(
    cmid: uuid.UUID | None,
    content: str = "hello",
    *,
    supersedes: uuid.UUID | None = None,
    attachment_ids: list[uuid.UUID] | None = None,
) -> Any:
    return SimpleNamespace(
        messages=[
            SimpleNamespace(role="user", content=content, client_message_id=cmid)
        ],
        model="",
        use_rag=True,
        thread_id=str(THREAD_ID),
        supersedes_client_message_id=supersedes,
        attachment_ids=attachment_ids,
    )


def _thread() -> Any:
    return SimpleNamespace(id=THREAD_ID, conversation_id=CONVERSATION_ID)


async def _count(db: AsyncSession, model: Any) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_idempotent_retry_returns_existing_run(db: AsyncSession) -> None:
    """Same key + same user twice: one run, one user row, one outbox row."""
    cmid = uuid.uuid4()
    user = _user()

    first = await accept_submission(
        db, current_user=user, request=_request(cmid), thread=_thread()
    )
    second = await accept_submission(
        db, current_user=user, request=_request(cmid), thread=_thread()
    )

    assert second.run_id == first.run_id, (
        "a resubmitted idempotency key must attach to the run it already "
        "created, not start a second one"
    )
    assert second.replayed is True
    assert first.replayed is False
    assert second.outbox_id == first.outbox_id
    assert await _count(db, AgentRun) == 1
    assert await _count(db, ChatMessage) == 1
    assert await _count(db, AgentOutbox) == 1


async def test_idempotency_race_attaches_to_the_winner(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The IntegrityError path: the loser re-reads the winner, never duplicates.

    A racing submission's pre-insert lookup runs against a snapshot that does
    not yet contain the winner's row, so it proceeds to INSERT and loses on
    ``uq_agent_runs_user_idempotency_key``. Suppressing the lookup reproduces
    exactly that state deterministically.
    """
    cmid = uuid.uuid4()
    user = _user()
    winner = await accept_submission(
        db, current_user=user, request=_request(cmid), thread=_thread()
    )

    calls = {"n": 0}
    real_lookup = submission_mod._find_by_idempotency_key

    async def blind_first_lookup(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:  # the pre-insert probe: winner not yet visible
            return None
        return await real_lookup(*args, **kwargs)

    monkeypatch.setattr(submission_mod, "_find_by_idempotency_key", blind_first_lookup)

    loser = await accept_submission(
        db, current_user=user, request=_request(cmid), thread=_thread()
    )

    assert calls["n"] == 2, "the recovery must re-read after the IntegrityError"
    assert loser.run_id == winner.run_id
    assert loser.replayed is True
    assert await _count(db, AgentRun) == 1
    assert await _count(db, ChatMessage) == 1
    assert await _count(db, AgentOutbox) == 1


async def test_idempotency_key_is_scoped_per_user(db: AsyncSession) -> None:
    """Two users sending the same client id get two runs, never one shared."""
    cmid = uuid.uuid4()
    key_a = stream_idempotency_key(_request(cmid), _user(USER_A))
    key_b = stream_idempotency_key(_request(cmid), _user(USER_B, ORG_B))
    assert key_a is not None and key_b is not None and key_a != key_b


def test_no_client_message_id_means_no_idempotency_key() -> None:
    """Legacy clients dispatch unconditionally, exactly as before."""
    assert stream_idempotency_key(_request(None), _user()) is None


# ---------------------------------------------------------------------------
# What the transaction actually writes
# ---------------------------------------------------------------------------


async def test_accept_writes_all_four_rows_atomically(db: AsyncSession) -> None:
    cmid = uuid.uuid4()
    accepted = await accept_submission(
        db, current_user=_user(), request=_request(cmid), thread=_thread()
    )

    run = await db.get(AgentRun, accepted.run_id)
    assert run is not None
    assert run.status == JobStatus.QUEUED.value
    assert run.thread_id == THREAD_ID
    assert run.organization_id == ORG_A
    assert run.user_id == USER_A
    assert str(run.user_message_id) == accepted.user_message_id

    message = await db.get(ChatMessage, uuid.UUID(str(accepted.user_message_id)))
    assert message is not None
    assert message.role == MessageRole.USER
    assert message.content == "hello"

    events = (
        (
            await db.execute(
                select(AgentRunEvent).where(AgentRunEvent.run_id == accepted.run_id)
            )
        )
        .scalars()
        .all()
    )
    assert [(e.seq, e.event_type) for e in events] == [
        (1, RunEventType.RUN_CREATED.value)
    ], "run.created is seq 1 of a new run's ledger"
    assert events[0].organization_id == ORG_A

    outbox = await db.get(AgentOutbox, uuid.UUID(str(accepted.outbox_id)))
    assert outbox is not None
    assert outbox.run_id == accepted.run_id
    assert outbox.status == AgentOutboxStatus.PENDING.value
    assert outbox.organization_id == ORG_A
    assert outbox.dispatched_at is None

    thread = await db.get(Thread, THREAD_ID)
    assert thread is not None and thread.message_count == 1


async def test_dispatch_storage_failure_raises_and_rolls_back_claim(
    db: AsyncSession,
) -> None:
    """Only a lost compare-and-set may look like a lost execution claim."""
    accepted = await accept_submission(
        db,
        current_user=_user(),
        request=_request(uuid.uuid4()),
        thread=_thread(),
    )

    with patch.object(
        submission_mod,
        "append_event",
        new=AsyncMock(side_effect=RuntimeError("ledger unavailable")),
    ):
        with pytest.raises(RuntimeError, match="ledger unavailable"):
            await submission_mod.mark_submission_dispatched(
                db,
                run_id=accepted.run_id,
                outbox_id=accepted.outbox_id,
                organization_id=ORG_A,
                user_id=USER_A,
            )

    run = await db.get(AgentRun, accepted.run_id)
    outbox = await db.get(AgentOutbox, uuid.UUID(str(accepted.outbox_id)))
    events = (
        (
            await db.execute(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == accepted.run_id)
                .order_by(AgentRunEvent.seq)
            )
        )
        .scalars()
        .all()
    )
    assert run is not None and run.status == JobStatus.QUEUED.value
    assert outbox is not None and outbox.status == AgentOutboxStatus.PENDING.value
    assert [event.event_type for event in events] == [RunEventType.RUN_CREATED.value]


async def test_org_less_user_is_not_stringified(db: AsyncSession) -> None:
    """``str(None) == "None"`` has merged tenants here before — never again."""
    accepted = await accept_submission(
        db,
        current_user=_user(org=None),
        request=_request(uuid.uuid4()),
        thread=_thread(),
    )
    run = await db.get(AgentRun, accepted.run_id)
    outbox = await db.get(AgentOutbox, uuid.UUID(str(accepted.outbox_id)))
    event_row = (
        await db.execute(
            select(AgentRunEvent).where(AgentRunEvent.run_id == accepted.run_id)
        )
    ).scalar_one()
    assert run is not None and run.organization_id is None
    assert outbox is not None and outbox.organization_id is None
    assert event_row.organization_id is None


async def test_thread_rejects_active_writer_until_it_is_terminal(
    db: AsyncSession,
) -> None:
    """The durable slot releases only when the prior run is terminal."""
    first = await accept_submission(
        db, current_user=_user(), request=_request(uuid.uuid4()), thread=_thread()
    )
    with pytest.raises(ActiveRunConflict):
        await accept_submission(
            db, current_user=_user(), request=_request(uuid.uuid4()), thread=_thread()
        )

    old = await db.get(AgentRun, first.run_id)
    assert old is not None
    assert old.status == JobStatus.QUEUED.value

    old.status = JobStatus.COMPLETED.value
    await db.commit()
    second = await accept_submission(
        db, current_user=_user(), request=_request(uuid.uuid4()), thread=_thread()
    )

    assert second.run_id != first.run_id
    await db.refresh(old)
    assert old.status == JobStatus.COMPLETED.value

    closing = (
        (
            await db.execute(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == first.run_id)
                .order_by(AgentRunEvent.seq)
            )
        )
        .scalars()
        .all()
    )
    assert [e.event_type for e in closing] == [
        RunEventType.RUN_CREATED.value,
    ]


async def test_fresh_turn_atomically_abandons_awaiting_confirmation(
    db: AsyncSession,
) -> None:
    first = await accept_submission(
        db, current_user=_user(), request=_request(uuid.uuid4()), thread=_thread()
    )
    old = await db.get(AgentRun, first.run_id)
    assert old is not None
    old.status = JobStatus.AWAITING_CONFIRMATION.value
    await db.commit()

    second = await accept_submission(
        db, current_user=_user(), request=_request(uuid.uuid4()), thread=_thread()
    )

    await db.refresh(old)
    assert old.status == JobStatus.CANCELLED.value
    assert old.completed_at is not None
    assert second.run_id != first.run_id
    events = (
        (
            await db.execute(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == first.run_id)
                .order_by(AgentRunEvent.seq)
            )
        )
        .scalars()
        .all()
    )
    assert [event.event_type for event in events] == [
        RunEventType.RUN_CREATED.value,
        RunEventType.RUN_CANCELLED.value,
    ]
    assert events[-1].payload == {"reason": "superseded_by_new_turn"}


async def test_stale_parked_stop_cannot_cancel_replacement_run(
    db: AsyncSession,
) -> None:
    """A Stop that selected A cannot cancel replacement parked run B."""
    first = await accept_submission(
        db, current_user=_user(), request=_request(uuid.uuid4()), thread=_thread()
    )
    old = await db.get(AgentRun, first.run_id)
    assert old is not None
    old.status = JobStatus.AWAITING_CONFIRMATION.value
    await db.commit()

    second = await accept_submission(
        db, current_user=_user(), request=_request(uuid.uuid4()), thread=_thread()
    )
    replacement = await db.get(AgentRun, second.run_id)
    assert replacement is not None
    replacement.status = JobStatus.AWAITING_CONFIRMATION.value
    await db.commit()

    stale_stop = await abandon_awaiting_submission(
        db,
        thread_id=THREAD_ID,
        organization_id=ORG_A,
        user_id=USER_A,
        reason="user_stopped_confirmation",
        expected_run_id=first.run_id,
    )

    assert stale_stop is None
    await db.refresh(replacement)
    assert replacement.status == JobStatus.AWAITING_CONFIRMATION.value


async def test_running_run_stop_claim_is_durable_and_idempotent(
    db: AsyncSession,
) -> None:
    """A normal running turn claims stopping exactly once before acknowledgement."""
    accepted = await accept_submission(
        db, current_user=_user(), request=_request(uuid.uuid4()), thread=_thread()
    )
    run = await db.get(AgentRun, accepted.run_id)
    assert run is not None
    assert await mark_submission_dispatched(
        db,
        run_id=accepted.run_id,
        outbox_id=accepted.outbox_id,
        organization_id=ORG_A,
        user_id=USER_A,
    )

    first = await request_run_cancellation(
        db,
        run_id=accepted.run_id,
        thread_id=THREAD_ID,
        organization_id=ORG_A,
        user_id=USER_A,
        reason="user_requested",
        request_id="stop-1",
    )
    assert first is not None and first.claimed is True
    await db.commit()

    second = await request_run_cancellation(
        db,
        run_id=accepted.run_id,
        thread_id=THREAD_ID,
        organization_id=ORG_A,
        user_id=USER_A,
        reason="user_requested",
        request_id="stop-2",
    )
    assert second is not None and second.claimed is False
    await db.commit()

    await db.refresh(run)
    assert run.status == JobStatus.STOPPING.value
    assert run.cancel_requested_at is not None
    events = (
        (
            await db.execute(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == accepted.run_id)
                .order_by(AgentRunEvent.seq)
            )
        )
        .scalars()
        .all()
    )
    assert [event.event_type for event in events] == [
        RunEventType.RUN_CREATED.value,
        RunEventType.RUN_STARTED.value,
        RunEventType.RUN_STOPPING.value,
    ]
    assert events[-1].payload == {"reason": "user_requested", "request_id": "stop-1"}


async def test_completion_cannot_overwrite_a_durable_stop(
    db: AsyncSession,
) -> None:
    """The completion/cancel race leaves the stop claim for producer acknowledgement."""
    accepted = await accept_submission(
        db, current_user=_user(), request=_request(uuid.uuid4()), thread=_thread()
    )
    run = await db.get(AgentRun, accepted.run_id)
    assert run is not None
    assert await mark_submission_dispatched(
        db,
        run_id=accepted.run_id,
        outbox_id=accepted.outbox_id,
        organization_id=ORG_A,
        user_id=USER_A,
    )

    stop = await request_run_cancellation(
        db,
        run_id=accepted.run_id,
        thread_id=THREAD_ID,
        organization_id=ORG_A,
        user_id=USER_A,
        reason="user_requested",
        request_id="stop-race",
    )
    assert stop is not None and stop.claimed is True
    await db.commit()

    finalized = await submission_mod.finalize_submission(
        db,
        run_id=accepted.run_id,
        status=JobStatus.COMPLETED,
        organization_id=ORG_A,
        event_type=RunEventType.RUN_COMPLETED,
        payload={},
    )
    assert finalized is False
    await db.refresh(run)
    assert run.status == JobStatus.STOPPING.value
    events = (
        (
            await db.execute(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == accepted.run_id)
                .order_by(AgentRunEvent.seq)
            )
        )
        .scalars()
        .all()
    )
    assert [event.event_type for event in events].count(
        RunEventType.RUN_COMPLETED.value
    ) == 0


async def test_producer_ack_closes_stopping_run_once(
    db: AsyncSession,
) -> None:
    """The producer ACK is the single terminal cancellation ledger event."""
    accepted = await accept_submission(
        db, current_user=_user(), request=_request(uuid.uuid4()), thread=_thread()
    )
    run = await db.get(AgentRun, accepted.run_id)
    assert run is not None
    assert await mark_submission_dispatched(
        db,
        run_id=accepted.run_id,
        outbox_id=accepted.outbox_id,
        organization_id=ORG_A,
        user_id=USER_A,
    )
    stop = await request_run_cancellation(
        db,
        run_id=accepted.run_id,
        thread_id=THREAD_ID,
        organization_id=ORG_A,
        user_id=USER_A,
        reason="user_requested",
        request_id="stop-ack",
    )
    assert stop is not None and stop.claimed is True
    await db.commit()

    first = await submission_mod.finalize_submission(
        db,
        run_id=accepted.run_id,
        status=JobStatus.CANCELLED,
        organization_id=ORG_A,
        event_type=RunEventType.RUN_CANCELLED,
        payload={"reason": "user_requested"},
    )
    second = await submission_mod.finalize_submission(
        db,
        run_id=accepted.run_id,
        status=JobStatus.CANCELLED,
        organization_id=ORG_A,
        event_type=RunEventType.RUN_CANCELLED,
        payload={"reason": "user_requested"},
    )

    assert first is True
    assert second is False
    await db.refresh(run)
    assert run.status == JobStatus.CANCELLED.value
    events = (
        (
            await db.execute(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == accepted.run_id)
                .order_by(AgentRunEvent.seq)
            )
        )
        .scalars()
        .all()
    )
    assert [event.event_type for event in events].count(
        RunEventType.RUN_STOPPING.value
    ) == 1
    assert [event.event_type for event in events].count(
        RunEventType.RUN_CANCELLED.value
    ) == 1


async def test_a_failed_write_leaves_nothing_behind(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Service-level mirror of the stream fault-injection suite."""

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("outbox write failed")

    monkeypatch.setattr(submission_mod, "_insert_outbox", boom)

    with pytest.raises(RuntimeError):
        await accept_submission(
            db, current_user=_user(), request=_request(uuid.uuid4()), thread=_thread()
        )

    assert await _count(db, AgentRun) == 0
    assert await _count(db, ChatMessage) == 0
    assert await _count(db, AgentRunEvent) == 0
    assert await _count(db, AgentOutbox) == 0


# --------------------------------------------------------------------------- #
# Edit-and-resend rides the accept transaction
# --------------------------------------------------------------------------- #


async def _seed_turn(
    db: AsyncSession, *, cmid: uuid.UUID, content: str, role: MessageRole
) -> uuid.UUID:
    row = ChatMessage(
        thread_id=THREAD_ID,
        user_id=USER_A,
        role=role,
        content=content,
        client_message_id=cmid if role == MessageRole.USER else None,
    )
    db.add(row)
    await db.commit()
    return uuid.UUID(str(row.id))


async def test_accept_tombstones_the_edited_turn_and_its_tail(db: AsyncSession) -> None:
    """The tombstones must land in the accept transaction, not after it.

    P0-C moved the user row out of ``_persist_user_message`` and into the
    accept transaction. If edit-and-resend had been left behind there, the
    replacement question would commit while the answer it replaced stayed
    visible — and, worse, kept feeding the model.
    """
    u1 = uuid.uuid4()
    await _seed_turn(db, cmid=u1, content="first q", role=MessageRole.USER)
    await _seed_turn(
        db, cmid=uuid.uuid4(), content="first a", role=MessageRole.ASSISTANT
    )

    replacement_cmid = uuid.uuid4()
    accepted = await accept_submission(
        db,
        current_user=_user(),
        request=_request(replacement_cmid, "first q, edited", supersedes=u1),
        thread=_thread(),
    )

    assert accepted.tombstoned is True
    assert accepted.tombstoned_count == 2  # the edited turn and its answer

    rows = (
        await db.execute(
            select(
                ChatMessage.id,
                ChatMessage.client_message_id,
                ChatMessage.superseded_by_message_id,
            ).where(ChatMessage.thread_id == THREAD_ID)
        )
    ).all()
    assert len(rows) == 3
    replacement = next(
        r for r in rows if str(r.client_message_id) == str(replacement_cmid)
    )
    assert str(replacement.id) == str(accepted.user_message_id)
    # The replacement is not swept by its own pass; everything else is.
    assert replacement.superseded_by_message_id is None
    assert {
        str(r.superseded_by_message_id) for r in rows if r.id != replacement.id
    } == {str(replacement.id)}

    # +1 for the replacement, -2 for what it superseded, floored at 0.
    count = (
        await db.execute(select(Thread.message_count).where(Thread.id == THREAD_ID))
    ).scalar_one()
    assert count == 0


async def test_editor_accept_tombstones_the_edited_turn_and_its_tail(
    db: AsyncSession,
) -> None:
    """An editor admitted by ``_resolve_thread`` can complete the same edit.

    Regresses the split authorization where resolution accepted workspace
    editors but the defense-in-depth tombstone query still required ownership,
    committing the replacement beside an untouched old answer.
    """
    editor_id = uuid.uuid4()
    db.add(
        WorkspaceMember(
            workspace_id=WORKSPACE_ID,
            user_id=editor_id,
            role=WorkspaceRole.EDITOR,
        )
    )
    edited_cmid = uuid.uuid4()
    await _seed_turn(
        db,
        cmid=edited_cmid,
        content="owner question",
        role=MessageRole.USER,
    )
    await _seed_turn(
        db,
        cmid=uuid.uuid4(),
        content="owner answer",
        role=MessageRole.ASSISTANT,
    )

    replacement_cmid = uuid.uuid4()
    accepted = await accept_submission(
        db,
        current_user=_user(editor_id),
        request=_request(
            replacement_cmid,
            "editor replacement",
            supersedes=edited_cmid,
        ),
        thread=_thread(),
    )

    assert accepted.tombstoned is True
    assert accepted.tombstoned_count == 2
    rows = (
        await db.execute(
            select(
                ChatMessage.client_message_id,
                ChatMessage.superseded_by_message_id,
            ).where(ChatMessage.thread_id == THREAD_ID)
        )
    ).all()
    replacement = next(row for row in rows if row.client_message_id == replacement_cmid)
    assert replacement.superseded_by_message_id is None
    assert {
        row.superseded_by_message_id
        for row in rows
        if row.client_message_id != replacement_cmid
    } == {uuid.UUID(str(accepted.user_message_id))}


@pytest.mark.parametrize("deleted_ancestor", ["thread", "conversation", "workspace"])
async def test_tombstone_write_denies_soft_deleted_ancestor(
    db: AsyncSession,
    deleted_ancestor: str,
) -> None:
    """The write-side authorization must independently enforce live ancestry."""
    from src.services.agent.agent_execution_service import _tombstone_superseded_turns

    edited_cmid = uuid.uuid4()
    await _seed_turn(
        db,
        cmid=edited_cmid,
        content="question",
        role=MessageRole.USER,
    )
    target = (
        await db.execute(
            select(ChatMessage).where(ChatMessage.client_message_id == edited_cmid)
        )
    ).scalar_one()
    replacement = ChatMessage(
        thread_id=THREAD_ID,
        user_id=USER_A,
        role=MessageRole.USER,
        content="replacement",
        client_message_id=uuid.uuid4(),
    )
    db.add(replacement)
    ancestor_model, ancestor_id = {
        "thread": (Thread, THREAD_ID),
        "conversation": (Conversation, CONVERSATION_ID),
        "workspace": (Workspace, WORKSPACE_ID),
    }[deleted_ancestor]
    ancestor = await db.get(ancestor_model, ancestor_id)
    assert ancestor is not None
    ancestor.is_deleted = True
    await db.commit()

    count = await _tombstone_superseded_turns(
        db,
        _user(),
        thread_id=str(THREAD_ID),
        supersedes_cmid=edited_cmid,
        replacement_row_id=replacement.id,
    )

    await db.refresh(target)
    assert count == 0
    assert target.superseded_by_message_id is None


@pytest.mark.parametrize(
    ("role", "membership_deleted"),
    [
        (WorkspaceRole.VIEWER, False),
        (WorkspaceRole.EDITOR, True),
    ],
    ids=["viewer", "revoked-editor"],
)
async def test_tombstone_write_denies_non_editable_membership(
    db: AsyncSession,
    role: WorkspaceRole,
    membership_deleted: bool,
) -> None:
    """Read-only and revoked members cannot mutate an old conversation turn."""
    from src.services.agent.agent_execution_service import _tombstone_superseded_turns

    member_id = uuid.uuid4()
    db.add(
        WorkspaceMember(
            workspace_id=WORKSPACE_ID,
            user_id=member_id,
            role=role,
            is_deleted=membership_deleted,
        )
    )
    edited_cmid = uuid.uuid4()
    await _seed_turn(
        db,
        cmid=edited_cmid,
        content="question",
        role=MessageRole.USER,
    )
    target = (
        await db.execute(
            select(ChatMessage).where(ChatMessage.client_message_id == edited_cmid)
        )
    ).scalar_one()
    replacement = ChatMessage(
        thread_id=THREAD_ID,
        user_id=member_id,
        role=MessageRole.USER,
        content="unauthorized replacement",
        client_message_id=uuid.uuid4(),
    )
    db.add(replacement)
    await db.commit()

    count = await _tombstone_superseded_turns(
        db,
        _user(member_id),
        thread_id=str(THREAD_ID),
        supersedes_cmid=edited_cmid,
        replacement_row_id=replacement.id,
    )

    await db.refresh(target)
    assert count == 0
    assert target.superseded_by_message_id is None


async def test_accept_leaves_a_plain_turn_untombstoned(db: AsyncSession) -> None:
    u1 = uuid.uuid4()
    await _seed_turn(db, cmid=u1, content="first q", role=MessageRole.USER)

    accepted = await accept_submission(
        db,
        current_user=_user(),
        request=_request(uuid.uuid4(), "second q"),
        thread=_thread(),
    )

    assert accepted.tombstoned is False
    assert accepted.tombstoned_count == 0
    superseded = (
        await db.execute(
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.superseded_by_message_id.isnot(None))
        )
    ).scalar_one()
    assert superseded == 0


async def test_replayed_edit_still_reports_tombstones(db: AsyncSession) -> None:
    """A replay wrote nothing — but the checkpoint still has to converge.

    The submission being attached to committed this edit's tombstones and then
    lost its response. Reporting ``tombstoned=False`` here would skip the
    resync and leave the model answering the turn the user edited away; a
    redundant resync is only an idempotent reseed from the DB.
    """
    u1 = uuid.uuid4()
    await _seed_turn(db, cmid=u1, content="first q", role=MessageRole.USER)
    replacement_cmid = uuid.uuid4()
    request = _request(replacement_cmid, "first q, edited", supersedes=u1)

    first = await accept_submission(
        db, current_user=_user(), request=request, thread=_thread()
    )
    assert first.replayed is False
    assert first.tombstoned is True

    second = await accept_submission(
        db, current_user=_user(), request=request, thread=_thread()
    )
    assert second.replayed is True
    assert second.run_id == first.run_id
    assert second.tombstoned is True
    # ...and nothing was written twice.
    assert await _count(db, AgentRun) == 1
    assert await _count(db, ChatMessage) == 2


async def test_accept_refuses_a_recycled_replacement_cmid(db: AsyncSession) -> None:
    """A reused replacement cmid must not tombstone an unrelated live turn.

    ``_insert_user_message`` dedups onto the existing row, so without the
    validation in ``apply_edit_resend_tombstones`` the second edit would ride
    that row and supersede a turn this request never edited.
    """
    u1 = uuid.uuid4()
    u2 = uuid.uuid4()
    await _seed_turn(db, cmid=u1, content="first q", role=MessageRole.USER)
    await _seed_turn(db, cmid=u2, content="second q", role=MessageRole.USER)

    replacement_cmid = uuid.uuid4()
    await accept_submission(
        db,
        current_user=_user(),
        request=_request(replacement_cmid, "first q, edited", supersedes=u1),
        thread=_thread(),
    )

    # Same replacement cmid, DIFFERENT claimed target. The pre-check would
    # short-circuit on the idempotency key, so drive the dedup path directly.
    u3 = uuid.uuid4()
    await _seed_turn(db, cmid=u3, content="third q", role=MessageRole.USER)
    before = (
        await db.execute(
            select(ChatMessage.id, ChatMessage.superseded_by_message_id).where(
                ChatMessage.client_message_id == u3
            )
        )
    ).first()
    assert before is not None and before.superseded_by_message_id is None

    from unittest.mock import AsyncMock, patch

    with patch.object(
        submission_mod, "_find_by_idempotency_key", new=AsyncMock(return_value=None)
    ):
        with pytest.raises(Exception):
            # The reused idempotency key trips its unique index; what matters
            # is that u3 survives the attempt.
            await accept_submission(
                db,
                current_user=_user(),
                request=_request(replacement_cmid, "unrelated", supersedes=u3),
                thread=_thread(),
            )

    after = (
        await db.execute(
            select(ChatMessage.superseded_by_message_id).where(
                ChatMessage.client_message_id == u3
            )
        )
    ).scalar_one()
    assert after is None


# ---------------------------------------------------------------------------
# Attachments (R7-M5)
# ---------------------------------------------------------------------------


async def test_accepted_stream_turn_persists_its_attachments(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/stream dropped attachment_ids on the floor; /execute always wrote them."""
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(
        "src.services.threads.workspace_access.filter_owned_document_ids",
        AsyncMock(return_value=[doc_a, doc_b]),
    )

    accepted = await accept_submission(
        db,
        current_user=_user(),
        request=_request(uuid.uuid4(), attachment_ids=[doc_a, doc_b]),
        thread=_thread(),
    )

    rows = (await db.execute(select(MessageAttachment.document_id))).scalars().all()
    assert sorted(str(r) for r in rows) == sorted([str(doc_a), str(doc_b)])
    assert accepted.user_message_id is not None


async def test_attachments_are_ownership_filtered(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ids the caller does not own must not become rows (same rule as /execute)."""
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(
        "src.services.threads.workspace_access.filter_owned_document_ids",
        AsyncMock(return_value=[mine]),
    )

    await accept_submission(
        db,
        current_user=_user(),
        request=_request(uuid.uuid4(), attachment_ids=[mine, theirs]),
        thread=_thread(),
    )

    rows = (await db.execute(select(MessageAttachment.document_id))).scalars().all()
    assert [str(r) for r in rows] == [str(mine)]


async def test_replayed_submission_does_not_double_attach(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = uuid.uuid4()
    monkeypatch.setattr(
        "src.services.threads.workspace_access.filter_owned_document_ids",
        AsyncMock(return_value=[doc]),
    )
    cmid = uuid.uuid4()
    user = _user()

    for _ in range(2):
        await accept_submission(
            db,
            current_user=user,
            request=_request(cmid, attachment_ids=[doc]),
            thread=_thread(),
        )

    assert await _count(db, MessageAttachment) == 1
