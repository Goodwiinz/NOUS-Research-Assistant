"""Give every foreign key a thread hard-delete reaches an ON DELETE rule.

Revision ID: rp01_thread_fk_ondelete
Revises: hb03_workspace_grants

``purge_soft_deleted_threads`` (src/tasks/retention_tasks.py) hard-deletes
threads soft-deleted for 30 days with bulk DELETEs (chat_messages, then the
thread), so no ORM cascade runs. Seven thread FKs added 09-28..10-05 (hb01,
hb05, hb06, aw01, aw02, it01) and ``artifact_references.message_id`` had no
ON DELETE: one referencing row made PostgreSQL refuse the delete, and the task
rolled back every organization's batch on every run (audit HO-2).

CASCADE where a row means nothing without the chat: a handoff; a consent bound
to the chat (SET NULL would make it read as project-wide, see
``mint_integration_grant``); that consent's memory selection. SET NULL where a
row must outlive it: grants (revocation evidence; every reader fails closed on
a NULL ``request_id``), tool actions (decision ledger), and artifact versions,
references and outbox rows (project content and provenance).
``integration_handoffs.consent_id`` gets SET NULL too, so no FK into a table
the delete reaches is left without a rule
(tests/unit/architecture/test_thread_purge_fk_contract.py).

A live FK's name depends on how its table was made (a revision's explicit
name, PostgreSQL's ``<table>_<column>_fkey``, or r6h3's ``create_all`` of the
model), so each one is found by its column and recreated as
``fk_<table>_<column>``.

Lock footprint: the upgrade holds ACCESS EXCLUSIVE on ``threads``,
``chat_messages`` and ``integration_grant_requests`` (and on each referencing
table) until the whole upgrade commits, with no ``lock_timeout`` (decision
D-A3), so a stuck Argo migration Job means a long-running transaction holding
those tables.
"""

from typing import Literal

from alembic import op  # type: ignore[attr-defined]

revision = "rp01_thread_fk_ondelete"
down_revision = "hb03_workspace_grants"
branch_labels = None
depends_on = None

Rule = Literal["CASCADE", "SET NULL"]

# (table, column, referenced table, rule); every referenced column is ``id``.
FOREIGN_KEYS: tuple[tuple[str, str, str, Rule], ...] = (
    ("integration_handoffs", "thread_id", "threads", "CASCADE"),
    ("integration_grant_requests", "thread_id", "threads", "CASCADE"),
    (
        "integration_context_selections",
        "consent_id",
        "integration_grant_requests",
        "CASCADE",
    ),
    ("integration_grants", "request_id", "integration_grant_requests", "SET NULL"),
    ("integration_handoffs", "consent_id", "integration_grant_requests", "SET NULL"),
    ("integration_grants", "thread_id", "threads", "SET NULL"),
    ("integration_tool_actions", "thread_id", "threads", "SET NULL"),
    ("artifact_versions", "thread_id", "threads", "SET NULL"),
    ("artifact_references", "thread_id", "threads", "SET NULL"),
    ("artifact_references", "message_id", "chat_messages", "SET NULL"),
    ("artifact_lifecycle_outbox", "thread_id", "threads", "SET NULL"),
)


def _drop_foreign_key(table: str, column: str, referenced: str) -> None:
    """Drop the one-column FK ``table.column -> referenced``, whatever its name."""
    op.execute(f"""
        DO $$
        DECLARE fk record;
        BEGIN
            FOR fk IN
                SELECT c.conname
                FROM pg_constraint c
                JOIN pg_attribute a
                  ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
                WHERE c.contype = 'f'
                  AND c.conrelid = to_regclass('{table}')
                  AND c.confrelid = to_regclass('{referenced}')
                  AND array_length(c.conkey, 1) = 1
                  AND a.attname = '{column}'
            LOOP
                EXECUTE 'ALTER TABLE ' || quote_ident('{table}')
                    || ' DROP CONSTRAINT ' || quote_ident(fk.conname);
            END LOOP;
        END
        $$
        """)


def _replace(table: str, column: str, referenced: str, rule: Rule | None) -> None:
    _drop_foreign_key(table, column, referenced)
    op.create_foreign_key(
        f"fk_{table}_{column}", table, referenced, [column], ["id"], ondelete=rule
    )


def upgrade() -> None:
    for table, column, referenced, rule in FOREIGN_KEYS:
        _replace(table, column, referenced, rule)


def downgrade() -> None:
    # Back to NO ACTION. Rows the upgrade's rules already removed or detached
    # stay so: the thread they named is gone.
    for table, column, referenced, _ in reversed(FOREIGN_KEYS):
        _replace(table, column, referenced, None)
