"""Audit D5 (P2.5) retention beat tasks.

Contract under test (all three riders):

- DRY-RUN default: with RETENTION_APPLY=false the tasks compute + LOG what they
  would delete but delete NOTHING.
- APPLY: with RETENTION_APPLY=true they hard-delete — thread row + its
  chat_messages + its LangGraph checkpoint rows; synthetic checkpoint threads;
  aged append-only rows.
- Age boundaries: rows inside the retention window are untouched.
- Synthetic marker matching: only 'synthetic-<key>-<epoch_ms>' thread_ids past
  the embedded-timestamp cutoff are purged; malformed/non-synthetic ids kept.
- Org scoping: rider 1 honours an organization_id filter.
- All tasks are flag-gated by RETENTION_ENABLED.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import column as sa_column
from sqlalchemy import create_engine
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy import table as sa_table
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

import src.models  # noqa: F401 — load full mapper registry (relationships)
from src.models.chat_message import ChatMessage, MessageRole
from src.models.conversation import Conversation
from src.models.thread import Thread
from src.models.workspace import Workspace
from src.tasks import retention_tasks

# NOTE: the append-only tables (analytics_events / audit_events / rag_queries)
# are exercised via the same Core tables retention_tasks uses (id + created_at)
# rather than their ORM models. RAGQuery's ORM model declares a back_populates
# to a non-existent User.rag_queries property (breaks mapper config), and
# AuditEvent's postgresql.UUID id serialises differently than GUID on sqlite —
# the task only ever touches id + created_at, so Core tables test it faithfully.
_ANALYTICS_TBL, _AUDIT_TBL, _RAG_TBL = retention_tasks._APPEND_ONLY_TABLES

pytestmark = pytest.mark.unit

NOW = datetime.now(timezone.utc)


def _retention_settings(
    *,
    enabled=True,
    apply=False,
    thread_days=30,
    synthetic_days=7,
    append_days=180,
    batch=500,
    max_batches=20,
):
    return SimpleNamespace(
        RETENTION_ENABLED=enabled,
        RETENTION_APPLY=apply,
        RETENTION_SOFT_DELETED_THREAD_DAYS=thread_days,
        RETENTION_SYNTHETIC_THREAD_DAYS=synthetic_days,
        RETENTION_APPEND_ONLY_DAYS=append_days,
        RETENTION_BATCH_SIZE=batch,
        RETENTION_MAX_BATCHES=max_batches,
    )


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    for model in (Workspace, Conversation, Thread, ChatMessage):
        model.__table__.create(engine)
    # Append-only tables via the Core tables retention_tasks uses (no ORM).
    for tbl in retention_tasks._APPEND_ONLY_TABLES:
        tbl.create(engine)
    # LangGraph checkpoint tables (no ORM model): minimal thread_id-keyed shape.
    with engine.begin() as conn:
        for tbl in retention_tasks._CHECKPOINT_TABLES:
            conn.execute(
                text(
                    f"CREATE TABLE {tbl} "
                    "(thread_id TEXT, checkpoint_id TEXT, blob TEXT)"
                )
            )
    factory = sessionmaker(bind=engine)
    yield factory
    engine.dispose()


def _run(task, factory, settings, **kwargs):
    with (
        patch.object(retention_tasks, "SessionLocal", factory),
        patch.object(retention_tasks, "get_settings", return_value=settings),
    ):
        return task(**kwargs)


# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------


def _seed_thread(factory, *, is_deleted, updated_at, org_id=None, n_messages=2):
    org_id = org_id or uuid.uuid4()
    ws_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    with factory() as db:
        db.add(
            Workspace(
                id=ws_id, name="ws", owner_id=uuid.uuid4(), organization_id=org_id
            )
        )
        db.add(
            Conversation(
                id=conv_id,
                workspace_id=ws_id,
                title="c",
                created_by_id=uuid.uuid4(),
            )
        )
        db.add(
            Thread(
                id=thread_id,
                conversation_id=conv_id,
                title="t",
                is_deleted=is_deleted,
                created_at=updated_at,
                updated_at=updated_at,
            )
        )
        for i in range(n_messages):
            db.add(
                ChatMessage(
                    thread_id=thread_id,
                    role=MessageRole.USER,
                    content=f"m{i}",
                )
            )
        db.commit()
        # Checkpoint rows keyed by the string thread_id (as the agent uses it).
        for i in range(3):
            db.execute(
                text(
                    "INSERT INTO checkpoints (thread_id, checkpoint_id, blob) "
                    "VALUES (:t, :c, 'x')"
                ),
                {"t": str(thread_id), "c": f"cp{i}"},
            )
        db.execute(
            text(
                "INSERT INTO checkpoint_writes (thread_id, checkpoint_id, blob) "
                "VALUES (:t, 'w', 'x')"
            ),
            {"t": str(thread_id)},
        )
        db.commit()
    return str(thread_id), str(org_id)


def _seed_synthetic_checkpoint(factory, thread_id, n=2):
    with factory() as db:
        for i in range(n):
            db.execute(
                text(
                    "INSERT INTO checkpoints (thread_id, checkpoint_id, blob) "
                    "VALUES (:t, :c, 'x')"
                ),
                {"t": thread_id, "c": f"cp{i}"},
            )
        db.commit()


def _checkpoint_count(factory, thread_id):
    with factory() as db:
        total = 0
        for tbl in retention_tasks._CHECKPOINT_TABLES:
            t = sa_table(tbl, sa_column("thread_id"))
            total += db.execute(
                select(sa_func.count()).select_from(t).where(t.c.thread_id == thread_id)
            ).scalar()
        return total


def _thread_exists(factory, thread_id):
    with factory() as db:
        return db.get(Thread, uuid.UUID(thread_id)) is not None


def _message_count(factory, thread_id):
    with factory() as db:
        return (
            db.query(ChatMessage)
            .filter(ChatMessage.thread_id == uuid.UUID(thread_id))
            .count()
        )


# ---------------------------------------------------------------------------
# Rider 1 — soft-deleted thread hard-delete
# ---------------------------------------------------------------------------


def test_soft_deleted_dry_run_lists_without_deleting(session_factory):
    old = NOW - timedelta(days=40)
    tid, _ = _seed_thread(session_factory, is_deleted=True, updated_at=old)

    result = _run(
        retention_tasks.purge_soft_deleted_threads,
        session_factory,
        _retention_settings(apply=False),
    )

    assert result["mode"] == "dry-run"
    assert result["threads"] == 1
    assert result["messages"] == 2
    assert result["checkpoints"] == 4  # 3 checkpoints + 1 write
    # Nothing actually removed.
    assert _thread_exists(session_factory, tid)
    assert _message_count(session_factory, tid) == 2
    assert _checkpoint_count(session_factory, tid) == 4


def test_soft_deleted_apply_deletes_thread_messages_checkpoints(session_factory):
    old = NOW - timedelta(days=40)
    tid, _ = _seed_thread(session_factory, is_deleted=True, updated_at=old)

    result = _run(
        retention_tasks.purge_soft_deleted_threads,
        session_factory,
        _retention_settings(apply=True),
    )

    assert result["mode"] == "apply"
    assert result["threads"] == 1
    assert result["messages"] == 2
    assert result["checkpoints"] == 4
    assert not _thread_exists(session_factory, tid)
    assert _message_count(session_factory, tid) == 0
    assert _checkpoint_count(session_factory, tid) == 0


def test_soft_deleted_age_boundary_and_active_untouched(session_factory):
    recent_deleted, _ = _seed_thread(
        session_factory, is_deleted=True, updated_at=NOW - timedelta(days=5)
    )
    active_old, _ = _seed_thread(
        session_factory, is_deleted=False, updated_at=NOW - timedelta(days=90)
    )

    result = _run(
        retention_tasks.purge_soft_deleted_threads,
        session_factory,
        _retention_settings(apply=True),
    )

    assert result["threads"] == 0
    assert _thread_exists(session_factory, recent_deleted)  # inside window
    assert _thread_exists(session_factory, active_old)  # not soft-deleted


def test_soft_deleted_org_scoping(session_factory):
    old = NOW - timedelta(days=40)
    target_org = uuid.uuid4()
    keep, _ = _seed_thread(session_factory, is_deleted=True, updated_at=old)
    scoped, scoped_org = _seed_thread(
        session_factory, is_deleted=True, updated_at=old, org_id=target_org
    )

    result = _run(
        retention_tasks.purge_soft_deleted_threads,
        session_factory,
        _retention_settings(apply=True),
        organization_id=scoped_org,
    )

    assert result["threads"] == 1
    assert not _thread_exists(session_factory, scoped)  # in-scope org deleted
    assert _thread_exists(session_factory, keep)  # other org untouched


def test_soft_deleted_apply_skips_a_thread_the_database_refuses(
    session_factory, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One thread whose delete fails is rolled back alone and skipped; the
    rest of the batch still commits (audit HO-2: one blocked thread used to
    roll back every org's purge on every run). The skip is logged at ERROR,
    naming the thread, so a permanently refused chat stays visible.

    Mutation check (docs/engineering/testing.md): in
    purge_soft_deleted_threads replace ``with db.begin_nested():`` by
    ``if True:`` and this fails on ``_message_count(..., blocked) == 2``
    (0: the refused thread's messages were committed deleted); remove the
    ``try``/``except DBAPIError`` as well and it fails with IntegrityError.
    """
    old = NOW - timedelta(days=60)
    oldest, _ = _seed_thread(session_factory, is_deleted=True, updated_at=old)
    blocked, _ = _seed_thread(
        session_factory, is_deleted=True, updated_at=old + timedelta(days=10)
    )
    newest, _ = _seed_thread(
        session_factory, is_deleted=True, updated_at=old + timedelta(days=20)
    )
    delete_checkpoints = retention_tasks._delete_checkpoint_rows

    def refuse_blocked(db, thread_id: str) -> int:
        # Runs after the thread's messages are deleted, so only a savepoint
        # can bring them back.
        if thread_id == blocked:
            raise IntegrityError("DELETE FROM threads", {}, Exception("referenced"))
        return delete_checkpoints(db, thread_id)

    monkeypatch.setattr(retention_tasks, "_delete_checkpoint_rows", refuse_blocked)

    with caplog.at_level(logging.ERROR, logger=retention_tasks.logger.name):
        result = _run(
            retention_tasks.purge_soft_deleted_threads,
            session_factory,
            _retention_settings(apply=True),
        )

    assert (result["threads"], result["skipped"]) == (2, 1)
    assert (result["messages"], result["checkpoints"]) == (4, 8)
    assert not _thread_exists(session_factory, oldest)
    assert not _thread_exists(session_factory, newest)
    assert _thread_exists(session_factory, blocked)
    assert _message_count(session_factory, blocked) == 2
    assert _checkpoint_count(session_factory, blocked) == 4
    errors = [
        record.getMessage()
        for record in caplog.records
        if record.name == retention_tasks.logger.name
        and record.levelno == logging.ERROR
    ]
    assert len(errors) == 1 and blocked in errors[0], errors


def test_soft_deleted_apply_aborts_on_a_lost_connection(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dropped connection is not the database refusing one thread: the run
    raises and stops, instead of counting it and every later thread as
    skipped.

    Mutation check: remove ``if exc.connection_invalidated: raise`` from
    purge_soft_deleted_threads and this fails with ``DID NOT RAISE``.
    """
    old = NOW - timedelta(days=60)
    lost, _ = _seed_thread(session_factory, is_deleted=True, updated_at=old)
    later, _ = _seed_thread(
        session_factory, is_deleted=True, updated_at=old + timedelta(days=10)
    )
    delete_checkpoints = retention_tasks._delete_checkpoint_rows

    def drop_connection(db, thread_id: str) -> int:
        if thread_id == lost:
            raise OperationalError(
                "DELETE FROM checkpoints",
                {},
                Exception("server closed the connection unexpectedly"),
                connection_invalidated=True,
            )
        return delete_checkpoints(db, thread_id)

    monkeypatch.setattr(retention_tasks, "_delete_checkpoint_rows", drop_connection)

    with pytest.raises(OperationalError):
        _run(
            retention_tasks.purge_soft_deleted_threads,
            session_factory,
            _retention_settings(apply=True),
        )

    assert _thread_exists(session_factory, lost)
    assert _thread_exists(session_factory, later)  # never attempted


# ---------------------------------------------------------------------------
# Rider 2 — synthetic checkpoint thread cap
# ---------------------------------------------------------------------------


def _synthetic_id(key, dt):
    return f"synthetic-{key}-{int(dt.timestamp() * 1000)}"


def test_synthetic_apply_purges_only_aged_marker_threads(session_factory):
    old = _synthetic_id("greeting", NOW - timedelta(days=10))
    recent = _synthetic_id("general_qa", NOW - timedelta(days=2))
    malformed = "synthetic-broken-notanumber"
    non_synth = str(uuid.uuid4())
    for tid in (old, recent, malformed, non_synth):
        _seed_synthetic_checkpoint(session_factory, tid)

    result = _run(
        retention_tasks.purge_synthetic_threads,
        session_factory,
        _retention_settings(apply=True),
    )

    assert result["mode"] == "apply"
    assert result["threads"] == 1  # only the >7d synthetic one
    assert _checkpoint_count(session_factory, old) == 0
    assert _checkpoint_count(session_factory, recent) == 2  # inside window
    assert _checkpoint_count(session_factory, malformed) == 2  # unparseable → kept
    assert _checkpoint_count(session_factory, non_synth) == 2  # not synthetic → kept


def test_synthetic_dry_run_lists_without_deleting(session_factory):
    old = _synthetic_id("ingest", NOW - timedelta(days=30))
    _seed_synthetic_checkpoint(session_factory, old)

    result = _run(
        retention_tasks.purge_synthetic_threads,
        session_factory,
        _retention_settings(apply=False),
    )

    assert result["mode"] == "dry-run"
    assert result["threads"] == 1
    assert result["rows"] == 2
    assert _checkpoint_count(session_factory, old) == 2  # not deleted


# ---------------------------------------------------------------------------
# Rider 3 — append-only event tables
# ---------------------------------------------------------------------------


def _seed_append(factory, tbl, created_at):
    with factory() as db:
        db.execute(tbl.insert().values(id=uuid.uuid4(), created_at=created_at))
        db.commit()


def _append_count(factory, tbl):
    with factory() as db:
        return db.execute(
            retention_tasks.select(retention_tasks.sa_func.count()).select_from(tbl)
        ).scalar()


def test_append_only_dry_run_counts_without_deleting(session_factory):
    _seed_append(session_factory, _ANALYTICS_TBL, NOW - timedelta(days=200))
    _seed_append(session_factory, _ANALYTICS_TBL, NOW - timedelta(days=10))
    _seed_append(session_factory, _AUDIT_TBL, NOW - timedelta(days=200))

    result = _run(
        retention_tasks.purge_append_only_events,
        session_factory,
        _retention_settings(apply=False),
    )

    assert result["mode"] == "dry-run"
    assert result["analytics_events"] == 1  # only the 200-day-old row
    assert result["audit_events"] == 1
    assert result["rag_queries"] == 0
    assert _append_count(session_factory, _ANALYTICS_TBL) == 2  # nothing deleted
    assert _append_count(session_factory, _AUDIT_TBL) == 1


def test_append_only_apply_deletes_aged_rows_only(session_factory):
    _seed_append(session_factory, _ANALYTICS_TBL, NOW - timedelta(days=200))
    _seed_append(session_factory, _ANALYTICS_TBL, NOW - timedelta(days=10))
    _seed_append(session_factory, _AUDIT_TBL, NOW - timedelta(days=365))

    result = _run(
        retention_tasks.purge_append_only_events,
        session_factory,
        _retention_settings(apply=True),
    )

    assert result["mode"] == "apply"
    assert result["analytics_events"] == 1
    assert result["audit_events"] == 1
    assert _append_count(session_factory, _ANALYTICS_TBL) == 1  # recent kept
    assert _append_count(session_factory, _AUDIT_TBL) == 0


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "task",
    [
        retention_tasks.purge_soft_deleted_threads,
        retention_tasks.purge_synthetic_threads,
        retention_tasks.purge_append_only_events,
    ],
)
def test_tasks_gated_by_retention_enabled(session_factory, task):
    result = _run(task, session_factory, _retention_settings(enabled=False))
    assert result == {"skipped": "retention-disabled"}
