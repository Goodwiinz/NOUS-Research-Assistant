"""Machine handoff: child runs, the delegated slot, per-kind outbox rows.

Revision ID: mh01_machine_handoff
Revises: pb01_pdf_message_branches

Adds ``agent_runs.parent_run_id`` (self FK, ON DELETE SET NULL) with
``idx_agent_runs_parent``, admits ``'delegated'`` to ``ck_agent_runs_status``,
narrows ``uq_agent_runs_active_thread`` to top-level runs and adds
``'delegated'`` to it, and replaces ``uq_agent_outbox_run (run_id)`` with
``uq_agent_outbox_run_kind (run_id, kind)``.

Locking follows pb01: the column, the NOT VALID foreign key and the NOT VALID
check are catalog-only and commit with the migration transaction (ACCESS
EXCLUSIVE on ``agent_runs`` for that short transaction, no ``lock_timeout``, as
in rp01). ``autocommit_block`` then validates each constraint as its own
statement (SHARE UPDATE EXCLUSIVE) and builds each index CONCURRENTLY. The
active-thread index is replaced by building ``uq_agent_runs_active_thread_new``
CONCURRENTLY and then, in one statement, dropping the old index and renaming the
new one. That statement takes ACCESS EXCLUSIVE but only touches the catalog, and
the slot is never unenforced. The final name stays
``uq_agent_runs_active_thread``, which agent_run_service._is_active_thread_conflict
matches. An interrupted concurrent build leaves an INVALID index behind, which
``IF NOT EXISTS`` would accept; each one is dropped first iff it is INVALID, so
re-running the upgrade repairs it.

The downgrade refuses while handoff state exists: a child run, a delegated run,
or a run with more than one outbox row. The narrower slot and the one-row
outbox key cannot hold that state.
"""

import alembic.op as op

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


def _drop_index_if_invalid(name: str) -> None:
    # Static identifiers only. Never touches a valid index.
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM pg_index
            WHERE indexrelid = to_regclass('{name}') AND NOT indisvalid
        ) THEN
            EXECUTE 'DROP INDEX ' || to_regclass('{name}')::text;
        END IF;
        END $$""")


def _swap_active_thread_index(predicate: str) -> None:
    """Rebuild ``uq_agent_runs_active_thread`` with ``predicate``. Autocommit."""
    name = "uq_agent_runs_active_thread"
    new = f"{name}_new"
    _drop_index_if_invalid(new)
    op.execute(
        f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {new} "
        f"ON agent_runs (thread_id) WHERE {predicate}"
    )
    # One statement: the slot always has exactly one enforcing index.
    op.execute(f"""DO $$ BEGIN
        IF to_regclass('{new}') IS NOT NULL THEN
            EXECUTE 'DROP INDEX IF EXISTS {name}';
            EXECUTE 'ALTER INDEX {new} RENAME TO {name}';
        END IF;
        END $$""")


def _status_check(statuses: str) -> None:
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS ck_agent_runs_status")
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT ck_agent_runs_status "
        f"CHECK (status IN ({statuses})) NOT VALID"
    )


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS parent_run_id VARCHAR(36)"
    )
    # The model names the FK the same, so a create_all schema is left alone.
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'fk_agent_runs_parent_run_id'
              AND conrelid = to_regclass('agent_runs')
        ) THEN
            ALTER TABLE agent_runs ADD CONSTRAINT fk_agent_runs_parent_run_id
            FOREIGN KEY (parent_run_id) REFERENCES agent_runs(job_id)
            ON DELETE SET NULL NOT VALID;
        END IF;
        END $$""")
    _status_check(_STATUSES + ", 'delegated'")
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
        _swap_active_thread_index(_LEGACY_ACTIVE_PREDICATE)
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_runs_parent")
    # Catalog-only again; the check is validated after this commits.
    _status_check(_STATUSES)
    op.execute(
        "ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS fk_agent_runs_parent_run_id"
    )
    op.execute("ALTER TABLE agent_runs DROP COLUMN IF EXISTS parent_run_id")
    with op.get_context().autocommit_block():
        op.execute("ALTER TABLE agent_runs VALIDATE CONSTRAINT ck_agent_runs_status")
