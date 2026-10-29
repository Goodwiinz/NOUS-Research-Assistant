"""Machine handoff: child runs, the delegated slot, per-kind outbox rows.

Revision ID: mh01_machine_handoff
Revises: pb01_pdf_message_branches

Adds ``agent_runs.parent_run_id`` (self FK, ON DELETE SET NULL) with
``idx_agent_runs_parent``, adds ``agent_runs.handoff_tool_call_id`` with
``uq_agent_runs_handoff_call`` (one child per parent tool call), admits ``'delegated'`` to ``ck_agent_runs_status``,
narrows ``uq_agent_runs_active_thread`` to top-level runs and adds
``'delegated'`` to it, and replaces ``uq_agent_outbox_run (run_id)`` with
``uq_agent_outbox_run_kind (run_id, kind)``.

Locking. Three kinds of step take ACCESS EXCLUSIVE on ``agent_runs``. That
lock blocks every read and write of the table while it is held, and also while
it is waited for, because later requests queue behind the waiter:

- the opening catalog step in the migration transaction: the two nullable ADD
  COLUMNs (no default), the FK and the replaced CHECK, both ``NOT VALID`` (no
  table scan);
- the active-thread swap: ``uq_agent_runs_active_thread_new`` is built
  CONCURRENTLY, then one statement drops the old index and renames the new one,
  so the slot is never unenforced and keeps the name
  agent_run_service._is_active_thread_conflict matches;
- dropping an INVALID index left by an interrupted concurrent build (which
  ``IF NOT EXISTS`` would otherwise accept), so a re-run repairs it.

Each of those runs in a PL/pgSQL block under ``lock_timeout = 5s``
(``set_config`` local to its transaction). On lock_not_available (55P03) the
block's subtransaction rolls back, sleeps 1, 2, 3 then 4 s and retries, up to 5
attempts; after the last it raises 55P03 saying the step changed nothing and the
migration can be re-run. The opening step retries in place inside alembic's own
transaction. Once granted, the lock is held only for catalog work. No other step
blocks reads or writes: constraints are VALIDATEd (SHARE UPDATE EXCLUSIVE) and
indexes built and dropped CONCURRENTLY in ``autocommit_block``, with no
lock_timeout. A re-run that finds the active-thread index already in its target
shape skips the rebuild.

The downgrade is for code that has been rolled back: scale every handoff writer
(API, workers and beat on the mh01 code) to zero first. It refuses while handoff
state exists (a child run, a delegated run, or a run with more than one outbox
row), because the narrower slot and the one-row outbox key cannot hold it. That
check is not atomic with the index rebuilds that follow it.
"""

from typing import Optional

import alembic.op as op
from sqlalchemy import text

revision = "mh01_machine_handoff"
down_revision = "pb01_pdf_message_branches"
branch_labels = None
depends_on = None

_STATUSES = (
    "'queued', 'running', 'awaiting_confirmation', 'stopping', 'recovering', "
    "'completed', 'failed', 'cancelled'"
)
_LEGACY_ACTIVE_PREDICATE = (
    "thread_id IS NOT NULL AND status IN "
    "('queued', 'running', 'awaiting_confirmation', 'stopping', 'recovering')"
)
# Must equal models.agent_run.AGENT_RUN_ACTIVE_THREAD_PREDICATE
# (tests/unit/test_machine_handoff_migration.py pins it without importing).
_ACTIVE_PREDICATE = (
    "thread_id IS NOT NULL AND parent_run_id IS NULL AND status IN "
    "('queued', 'running', 'awaiting_confirmation', 'stopping', 'recovering', "
    "'delegated')"
)
_ACTIVE_INDEX = "uq_agent_runs_active_thread"
# Must equal models.agent_run.AGENT_RUN_HANDOFF_CALL_PREDICATE (same unit test).
_HANDOFF_CALL_PREDICATE = (
    "parent_run_id IS NOT NULL AND handoff_tool_call_id IS NOT NULL"
)

# ACCESS EXCLUSIVE steps: wait at most _LOCK_TIMEOUT per attempt, sleep
# attempt * _LOCK_BACKOFF_S seconds between attempts. Read at call time, so the
# PostgreSQL test can shorten them.
_LOCK_TIMEOUT = "5s"
_LOCK_ATTEMPTS = 5
_LOCK_BACKOFF_S = 1.0


def _exclusive(body: str) -> str:
    """PL/pgSQL statements running ``body`` under lock_timeout, retried on 55P03.

    Each attempt is a BEGIN/EXCEPTION subtransaction, so a timed-out attempt
    rolls back alone, even inside alembic's migration transaction.
    """
    return f"""
        PERFORM set_config('lock_timeout', '{_LOCK_TIMEOUT}', true);
        FOR attempt IN 1..{_LOCK_ATTEMPTS} LOOP
            BEGIN
                {body}
                EXIT;
            EXCEPTION WHEN lock_not_available THEN
                IF attempt = {_LOCK_ATTEMPTS} THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'lock_not_available',
                        MESSAGE = format(
                            'mh01_machine_handoff: agent_runs stayed locked '
                            'through %s attempts of lock_timeout %s; this step '
                            'changed nothing, re-run the migration',
                            attempt, '{_LOCK_TIMEOUT}');
                END IF;
                PERFORM pg_sleep(attempt * {_LOCK_BACKOFF_S});
            END;
        END LOOP;"""


def _drop_index_if_invalid(name: str) -> None:
    # Static identifiers only. Never touches a valid index.
    drop = _exclusive(f"EXECUTE 'DROP INDEX ' || to_regclass('{name}')::text;")
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM pg_index
            WHERE indexrelid = to_regclass('{name}') AND NOT indisvalid
        ) THEN {drop}
        END IF;
        END $$""")


def _active_index_def() -> Optional[str]:
    """``uq_agent_runs_active_thread``'s definition; None offline or if absent."""
    if op.get_context().as_sql:
        return None
    definition = op.get_bind().scalar(
        text(f"SELECT pg_get_indexdef(to_regclass('{_ACTIVE_INDEX}'))")
    )
    return None if definition is None else str(definition)


def _swap_active_thread_index(predicate: str) -> None:
    """Rebuild ``uq_agent_runs_active_thread`` with ``predicate``. Autocommit."""
    new = f"{_ACTIVE_INDEX}_new"
    _drop_index_if_invalid(new)
    op.execute(
        f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {new} "
        f"ON agent_runs (thread_id) WHERE {predicate}"
    )
    # One statement: the slot always has exactly one enforcing index, and only
    # a valid build is promoted.
    swap = _exclusive(
        f"DROP INDEX IF EXISTS {_ACTIVE_INDEX}; "
        f"ALTER INDEX {new} RENAME TO {_ACTIVE_INDEX};"
    )
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM pg_index
            WHERE indexrelid = to_regclass('{new}') AND indisvalid
        ) THEN {swap}
        END IF;
        END $$""")


def _status_check(statuses: str) -> str:
    return (
        "ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS ck_agent_runs_status; "
        "ALTER TABLE agent_runs ADD CONSTRAINT ck_agent_runs_status "
        f"CHECK (status IN ({statuses})) NOT VALID;"
    )


def upgrade() -> None:
    # The model names the FK the same, so a create_all schema is left alone.
    opening = _exclusive(f"""
        ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS parent_run_id VARCHAR(36);
        ALTER TABLE agent_runs
            ADD COLUMN IF NOT EXISTS handoff_tool_call_id VARCHAR(255);
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'fk_agent_runs_parent_run_id'
              AND conrelid = to_regclass('agent_runs')
        ) THEN
            ALTER TABLE agent_runs ADD CONSTRAINT fk_agent_runs_parent_run_id
            FOREIGN KEY (parent_run_id) REFERENCES agent_runs(job_id)
            ON DELETE SET NULL NOT VALID;
        END IF;
        {_status_check(_STATUSES + ", 'delegated'")}""")
    op.execute(f"DO $$ BEGIN {opening} END $$")
    # Commits the ACCESS EXCLUSIVE catalog transaction above; every statement
    # below runs and commits on its own.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TABLE agent_runs VALIDATE CONSTRAINT fk_agent_runs_parent_run_id"
        )
        op.execute("ALTER TABLE agent_runs VALIDATE CONSTRAINT ck_agent_runs_status")
        _drop_index_if_invalid("idx_agent_runs_parent")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_agent_runs_parent "
            "ON agent_runs (parent_run_id)"
        )
        _drop_index_if_invalid("uq_agent_runs_handoff_call")
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_agent_runs_handoff_call "
            "ON agent_runs (parent_run_id, handoff_tool_call_id) "
            f"WHERE {_HANDOFF_CALL_PREDICATE}"
        )
        if "parent_run_id IS NULL" not in (_active_index_def() or ""):
            _swap_active_thread_index(_ACTIVE_PREDICATE)
        _drop_index_if_invalid("uq_agent_outbox_run_kind")
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_agent_outbox_run_kind "
            "ON agent_outbox (run_id, kind)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_agent_outbox_run")


def downgrade() -> None:
    # Refuse rather than discard a child's only link to its parent or build a
    # unique index that would fail and stay behind INVALID. parent_run_id is
    # read dynamically so a re-run after a partial downgrade still works.
    op.execute("""DO $$ DECLARE children boolean := false; BEGIN
        IF EXISTS (
            SELECT 1 FROM pg_attribute
            WHERE attrelid = to_regclass('agent_runs')
              AND attname = 'parent_run_id' AND NOT attisdropped
        ) THEN
            EXECUTE 'SELECT EXISTS (SELECT 1 FROM agent_runs'
                || ' WHERE parent_run_id IS NOT NULL)' INTO children;
        END IF;
        IF children
           OR EXISTS (SELECT 1 FROM agent_runs WHERE status = 'delegated')
           OR EXISTS (SELECT 1 FROM agent_outbox GROUP BY run_id
                      HAVING count(*) > 1) THEN
            RAISE EXCEPTION 'Machine handoff runs prevent downgrade';
        END IF;
        END $$""")
    with op.get_context().autocommit_block():
        _drop_index_if_invalid("uq_agent_outbox_run")
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_agent_outbox_run "
            "ON agent_outbox (run_id)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_agent_outbox_run_kind")
        index = _active_index_def()
        if index is None or "parent_run_id" in index:
            _swap_active_thread_index(_LEGACY_ACTIVE_PREDICATE)
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_agent_runs_handoff_call")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_runs_parent")
    # Catalog-only again; the check is validated after this commits.
    closing = _exclusive(f"""
        {_status_check(_STATUSES)}
        ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS fk_agent_runs_parent_run_id;
        ALTER TABLE agent_runs DROP COLUMN IF EXISTS handoff_tool_call_id;
        ALTER TABLE agent_runs DROP COLUMN IF EXISTS parent_run_id;""")
    op.execute(f"DO $$ BEGIN {closing} END $$")
    with op.get_context().autocommit_block():
        op.execute("ALTER TABLE agent_runs VALIDATE CONSTRAINT ck_agent_runs_status")
