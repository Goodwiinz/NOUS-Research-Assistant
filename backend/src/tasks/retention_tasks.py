"""Celery beat tasks for data retention (audit finding D5, item P2.5).

Three retention riders, each a flag-gated beat task with a **two-stage
safety** identical in spirit to the reconciler:

* ``RETENTION_ENABLED`` — kill switch. When false every task no-ops.
* ``RETENTION_APPLY`` — dry-run gate. Default **false**: the task computes
  and LOGS exactly what it *would* delete but deletes nothing. Only when an
  operator flips ``RETENTION_APPLY=true`` does anything get removed. Both
  flags default-safe, so wiring the beat schedule cannot destroy data on its
  own.

Riders
------
1. ``purge_soft_deleted_threads`` — thread delete is only a soft ``is_deleted``
   flag (``ChatService.delete_thread``); the row, its ``chat_messages`` and
   its LangGraph checkpoint rows all live forever. This hard-deletes threads
   soft-deleted for longer than ``RETENTION_SOFT_DELETED_THREAD_DAYS``
   (default 30), removing chat_messages + checkpoint rows
   (``checkpoints`` / ``checkpoint_writes`` / ``checkpoint_blobs``, keyed by
   ``thread_id``) + the thread row. Org-scoped (joins conversation→workspace),
   bounded batches.

2. ``purge_synthetic_threads`` — synthetic-traffic mints a checkpoint thread
   ``synthetic-<scenario>-<epoch_ms>`` every 20 min forever
   (``scripts/synthetic_traffic.py``), ~72 threads/day of machine noise that
   nothing ever cleans (the synthetic cleanup pass only touches Collections).
   These are checkpoint-only threads (no ``threads`` row), so this purges the
   checkpoint rows whose embedded millisecond timestamp is older than
   ``RETENTION_SYNTHETIC_THREAD_DAYS`` (default 7), regardless of soft-delete
   state.

3. ``purge_append_only_events`` — ``analytics_events`` / ``audit_events`` /
   ``rag_queries`` are append-only and never shrink. The pre-existing
   ``_process_data_retention`` machinery in ``analytics_processor`` is a
   no-op *placeholder* (returns a dict, deletes nothing), so this implements
   the minimal ``created_at``-cutoff deletes directly, defaulting to a
   conservative ``RETENTION_APPEND_ONLY_DAYS`` (180).

All tasks run on the synchronous SQLAlchemy API (``SessionLocal``) like the
other Celery sweepers, and return a summary dict for observability.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import Column, DateTime, MetaData, Table
from sqlalchemy import column as sa_column
from sqlalchemy import delete
from sqlalchemy import func as sa_func
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy import table as sa_table
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.core.config import get_settings
from src.core.database import SessionLocal
from src.models.base import GUID
from src.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# LangGraph AsyncPostgresSaver tables, all keyed by a ``thread_id`` text
# column. Pruning a thread means removing its rows from every one of them.
_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_writes", "checkpoint_blobs")

# Marker prefix for synthetic-traffic checkpoint threads
# (scripts/synthetic_traffic.py: f"synthetic-{scenario.key}-{epoch_ms}").
_SYNTHETIC_PREFIX = "synthetic-"

# Append-only event tables purged by rider 3, addressed at the Core level
# (id + created_at only) so we never import their ORM models — one of them
# (RAGQuery) carries a back_populates to a User property that does not exist,
# which would break mapper configuration for every other ORM query in the
# worker. Core Table objects generate SQL by name and never configure mappers,
# while GUID/DateTime typing keeps id round-trips and date comparisons correct
# on both Postgres and sqlite.
_append_only_meta = MetaData()


def _append_only_table(name: str) -> Table:
    return Table(
        name,
        _append_only_meta,
        Column("id", GUID(), primary_key=True),
        Column("created_at", DateTime(timezone=True)),
    )


_APPEND_ONLY_TABLES = (
    _append_only_table("analytics_events"),
    _append_only_table("audit_events"),
    _append_only_table("rag_queries"),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _table_exists(db, name: str) -> bool:
    """Whether *name* exists in the bound DB (checkpoint tables are created by
    LangGraph's saver setup, so may be absent in fresh/throwaway envs).

    Inspect the session's OWN connection (``db.connection()``), not the engine
    (``get_bind()``): the latter checks out a second connection, which under a
    single-connection pool (sqlite ``:memory:``) collides with the session's
    open transaction and can drop its uncommitted deletes.
    """
    try:
        return sa_inspect(db.connection()).has_table(name)
    except Exception:  # pragma: no cover - defensive
        logger.warning("retention: has_table(%s) failed", name, exc_info=True)
        return False


def _parse_synthetic_epoch_ms(thread_id: str) -> Optional[int]:
    """Extract the trailing ``epoch_ms`` from ``synthetic-<key>-<epoch_ms>``.

    Scenario keys never contain a hyphen, but rsplit is robust even if one
    did. Returns None when the suffix is not a positive integer so a
    malformed id is skipped rather than mis-aged into deletion.
    """
    if not thread_id.startswith(_SYNTHETIC_PREFIX):
        return None
    suffix = thread_id.rsplit("-", 1)[-1]
    try:
        ms = int(suffix)
    except (TypeError, ValueError):
        return None
    return ms if ms > 0 else None


def _count_checkpoint_rows(db, thread_id: str) -> int:
    """Total checkpoint rows across all tables for *thread_id* (dry-run)."""
    total = 0
    for tbl in _CHECKPOINT_TABLES:
        if not _table_exists(db, tbl):
            continue
        t = sa_table(tbl, sa_column("thread_id"))
        row = db.execute(
            select(sa_func.count()).select_from(t).where(t.c.thread_id == thread_id)
        ).scalar()
        total += int(row or 0)
    return total


def _delete_checkpoint_rows(db, thread_id: str) -> int:
    """Delete every checkpoint row for *thread_id*; return rows removed."""
    deleted = 0
    for tbl in _CHECKPOINT_TABLES:
        if not _table_exists(db, tbl):
            continue
        t = sa_table(tbl, sa_column("thread_id"))
        res = db.execute(delete(t).where(t.c.thread_id == thread_id))
        deleted += int(res.rowcount or 0)
    return deleted


# ---------------------------------------------------------------------------
# Rider 1 — hard-delete soft-deleted threads
# ---------------------------------------------------------------------------


@celery_app.task(name="src.tasks.retention_tasks.purge_soft_deleted_threads")
def purge_soft_deleted_threads(organization_id: Optional[str] = None) -> dict:
    """Hard-delete threads soft-deleted longer than the retention window.

    Two-stage safety: no-op when ``RETENTION_ENABLED`` is false; DRY-RUN
    (log-only) when ``RETENTION_APPLY`` is false. Org-scoped, bounded batch.

    APPLY deletes each thread in its own SAVEPOINT. A thread whose delete a
    constraint refuses (``IntegrityError``, e.g. an FK with no ON DELETE rule)
    is rolled back alone, logged at ERROR and counted in the result's
    ``"skipped"``; the rest of the batch still commits. Any other database
    error (a lost connection, a statement timeout, a read-only transaction) is
    not about one thread: it aborts the run and the task fails. A dry run
    always reports ``skipped=0``, because it deletes nothing and so cannot
    predict a refusal.
    """
    from src.models.chat_message import ChatMessage
    from src.models.conversation import Conversation
    from src.models.thread import Thread
    from src.models.workspace import Workspace

    settings = get_settings()
    if not settings.RETENTION_ENABLED:
        logger.info("purge_soft_deleted_threads: skipped (RETENTION_ENABLED=false)")
        return {"skipped": "retention-disabled"}

    apply = settings.RETENTION_APPLY
    cutoff = _utcnow() - timedelta(days=settings.RETENTION_SOFT_DELETED_THREAD_DAYS)
    batch_size = settings.RETENTION_BATCH_SIZE

    db = SessionLocal()
    try:
        q = (
            db.query(Thread, Workspace.organization_id)
            .join(Conversation, Thread.conversation_id == Conversation.id)
            .join(Workspace, Conversation.workspace_id == Workspace.id)
            .filter(
                Thread.is_deleted == True,  # noqa: E712
                Thread.updated_at < cutoff,
            )
        )
        if organization_id is not None:
            q = q.filter(Workspace.organization_id == organization_id)
        candidates = q.order_by(Thread.updated_at.asc()).limit(batch_size).all()

        threads_removed = 0
        threads_skipped = 0
        messages_removed = 0
        checkpoints_removed = 0

        for thread, org_id in candidates:
            tid = str(thread.id)
            if not apply:
                msg_count = (
                    db.query(ChatMessage)
                    .filter(ChatMessage.thread_id == thread.id)
                    .count()
                )
                cp_count = _count_checkpoint_rows(db, tid)
                logger.info(
                    "purge_soft_deleted_threads[DRY-RUN]: would delete thread %s "
                    "(org=%s, last_update=%s) -> %d messages + %d checkpoint rows",
                    tid,
                    org_id,
                    thread.updated_at.isoformat() if thread.updated_at else "unknown",
                    msg_count,
                    cp_count,
                )
                threads_removed += 1
                messages_removed += msg_count
                checkpoints_removed += cp_count
                continue

            # APPLY: messages first (explicit, not relying on DB cascade —
            # sqlite does not enforce FK cascade by default), then checkpoint
            # rows, then the thread row. Each thread in its own SAVEPOINT: a
            # row the database still refuses to delete rolls back that thread
            # alone, never the batch (one blocked thread used to stop every
            # org's purge on every run, audit HO-2).
            try:
                with db.begin_nested():
                    messages = (
                        db.query(ChatMessage)
                        .filter(ChatMessage.thread_id == thread.id)
                        .delete(synchronize_session=False)
                    )
                    checkpoints = _delete_checkpoint_rows(db, tid)
                    db.query(Thread).filter(Thread.id == thread.id).delete(
                        synchronize_session=False
                    )
            except IntegrityError:
                # Only a constraint refusal is about this thread: a row the
                # FK rules do not cover (SQLSTATE 23503). Every other database
                # error (a lost connection, a statement timeout, a read-only
                # transaction) would refuse every later thread the same way,
                # so it propagates and the run fails instead of reporting a
                # batch of skips as success.
                threads_skipped += 1
                logger.exception(
                    "purge_soft_deleted_threads: skipped thread %s (org=%s); "
                    "a constraint refused its delete",
                    tid,
                    org_id,
                )
                continue
            messages_removed += messages
            checkpoints_removed += checkpoints
            threads_removed += 1
            logger.info(
                "purge_soft_deleted_threads: deleted thread %s (org=%s)", tid, org_id
            )

        if apply:
            db.commit()

        result = {
            "mode": "apply" if apply else "dry-run",
            "threads": threads_removed,
            "skipped": threads_skipped,
            "messages": messages_removed,
            "checkpoints": checkpoints_removed,
            "batch_capped": len(candidates) >= batch_size,
        }
        logger.info("purge_soft_deleted_threads: %s", result)
        return result
    except Exception:
        db.rollback()
        logger.exception("purge_soft_deleted_threads failed")
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Rider 2 — cap synthetic-traffic checkpoint threads
# ---------------------------------------------------------------------------


@celery_app.task(name="src.tasks.retention_tasks.purge_synthetic_threads")
def purge_synthetic_threads() -> dict:
    """Purge synthetic-traffic checkpoint threads older than the cap.

    Synthetic threads are checkpoint-only (no ``threads`` row) and identified
    by the ``synthetic-<key>-<epoch_ms>`` thread_id; aged by the embedded
    millisecond timestamp. Two-stage safety like the other riders.
    """
    settings = get_settings()
    if not settings.RETENTION_ENABLED:
        logger.info("purge_synthetic_threads: skipped (RETENTION_ENABLED=false)")
        return {"skipped": "retention-disabled"}

    apply = settings.RETENTION_APPLY
    cutoff_ms = int(
        (
            _utcnow() - timedelta(days=settings.RETENTION_SYNTHETIC_THREAD_DAYS)
        ).timestamp()
        * 1000
    )
    batch_size = settings.RETENTION_BATCH_SIZE

    db = SessionLocal()
    try:
        if not _table_exists(db, "checkpoints"):
            logger.info("purge_synthetic_threads: no checkpoints table; nothing to do")
            return {"mode": "apply" if apply else "dry-run", "threads": 0, "rows": 0}

        # Distinct synthetic thread_ids from the primary checkpoint table.
        rows = db.execute(
            text(
                "SELECT DISTINCT thread_id FROM checkpoints "
                "WHERE thread_id LIKE :prefix"
            ),
            {"prefix": _SYNTHETIC_PREFIX + "%"},
        ).all()

        threads_removed = 0
        rows_removed = 0
        for (thread_id,) in rows:
            epoch_ms = _parse_synthetic_epoch_ms(thread_id)
            if epoch_ms is None or epoch_ms >= cutoff_ms:
                continue
            if threads_removed >= batch_size:
                break
            if not apply:
                cp_count = _count_checkpoint_rows(db, thread_id)
                logger.info(
                    "purge_synthetic_threads[DRY-RUN]: would delete synthetic "
                    "thread %s -> %d checkpoint rows",
                    thread_id,
                    cp_count,
                )
                threads_removed += 1
                rows_removed += cp_count
                continue
            rows_removed += _delete_checkpoint_rows(db, thread_id)
            threads_removed += 1

        if apply:
            db.commit()

        result = {
            "mode": "apply" if apply else "dry-run",
            "threads": threads_removed,
            "rows": rows_removed,
            "batch_capped": threads_removed >= batch_size,
        }
        logger.info("purge_synthetic_threads: %s", result)
        return result
    except Exception:
        db.rollback()
        logger.exception("purge_synthetic_threads failed")
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Rider 3 — prune append-only event tables
# ---------------------------------------------------------------------------


def _purge_append_only_table(db, tbl, cutoff, apply, batch_size, max_batches) -> int:
    """Delete rows of Core table *tbl* with ``created_at < cutoff`` in bounded
    batches. Returns rows deleted (or, in dry-run, rows that would be deleted —
    capped at ``batch_size * max_batches`` to stay bounded per run)."""
    if not _table_exists(db, tbl.name):
        return 0

    if not apply:
        # Bounded count so a huge backlog does not scan the whole table.
        total = db.execute(
            select(sa_func.count()).select_from(tbl).where(tbl.c.created_at < cutoff)
        ).scalar()
        return min(int(total or 0), batch_size * max_batches)

    total = 0
    for _ in range(max_batches):
        ids = [
            r[0]
            for r in db.execute(
                select(tbl.c.id).where(tbl.c.created_at < cutoff).limit(batch_size)
            ).all()
        ]
        if not ids:
            break
        db.execute(tbl.delete().where(tbl.c.id.in_(ids)))
        db.commit()
        total += len(ids)
        if len(ids) < batch_size:
            break
    return total


@celery_app.task(name="src.tasks.retention_tasks.purge_append_only_events")
def purge_append_only_events() -> dict:
    """Prune append-only analytics_events / audit_events / rag_queries.

    The pre-existing ``_process_data_retention`` job handler is a placeholder
    that deletes nothing, so these minimal ``created_at``-cutoff deletes are
    implemented here directly (Core-level; see ``_APPEND_ONLY_TABLES``). Two-
    stage safety like the other riders.
    """
    settings = get_settings()
    if not settings.RETENTION_ENABLED:
        logger.info("purge_append_only_events: skipped (RETENTION_ENABLED=false)")
        return {"skipped": "retention-disabled"}

    apply = settings.RETENTION_APPLY
    cutoff = _utcnow() - timedelta(days=settings.RETENTION_APPEND_ONLY_DAYS)
    batch_size = settings.RETENTION_BATCH_SIZE
    max_batches = settings.RETENTION_MAX_BATCHES

    db = SessionLocal()
    try:
        counts = {}
        for tbl in _APPEND_ONLY_TABLES:
            counts[tbl.name] = _purge_append_only_table(
                db, tbl, cutoff, apply, batch_size, max_batches
            )
            logger.info(
                "purge_append_only_events[%s]: %s %d rows (cutoff=%s)",
                tbl.name,
                "deleted" if apply else "would delete",
                counts[tbl.name],
                cutoff.isoformat(),
            )

        result = {"mode": "apply" if apply else "dry-run", **counts}
        logger.info("purge_append_only_events: %s", result)
        return result
    except Exception:
        db.rollback()
        logger.exception("purge_append_only_events failed")
        raise
    finally:
        db.close()
