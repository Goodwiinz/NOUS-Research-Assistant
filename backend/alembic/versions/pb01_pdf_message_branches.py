"""Add opt-in canonical PDF message branches.

Revision ID: pb01_pdf_message_branches
Revises: ac01_account_bindings

Existing threads retain chronological/tombstone behavior. No parent edges
are inferred for historical messages. Nullable SET NULL links preserve the
thread-retention purge; message_branching remains true if its PDF is removed,
so a missing document binding fails closed rather than becoming unscoped.

Locking: the ADD COLUMN statements take ACCESS EXCLUSIVE on ``chat_messages``
and ``threads``, so the transaction that holds them does only catalog work —
the nullable/defaulted columns and the foreign keys added ``NOT VALID``. That
transaction is committed by ``autocommit_block`` before any table scan runs:
each constraint is then validated as its own statement (SHARE UPDATE
EXCLUSIVE, reads and writes continue) and both indexes are built
``CONCURRENTLY``, as ``e6f7a8b9c0d1`` and ``y7z8a9b0c1d2`` do.

An interrupted concurrent build leaves its index behind marked INVALID, which
``IF NOT EXISTS`` would silently accept — and an invalid
``uq_chat_messages_branch_assistant`` enforces nothing. Each index is
therefore dropped first iff it exists and is invalid (as
``repair_invalid_chat_msg_idx`` does), so re-running the upgrade repairs it.
Index lookups use ``to_regclass`` so they follow ``search_path``.
"""

import alembic.op as op

revision = "pb01_pdf_message_branches"
down_revision = "ac01_account_bindings"
branch_labels = None
depends_on = None

FOREIGN_KEYS = (
    (
        "chat_messages",
        "parent_message_id",
        "chat_messages",
        "fk_chat_messages_parent_message_id",
    ),
    ("threads", "branch_document_id", "documents", "fk_threads_branch_document_id"),
    (
        "threads",
        "selected_message_id",
        "chat_messages",
        "fk_threads_selected_message_id",
    ),
)

INDEXES = (
    (
        "ix_chat_messages_parent",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chat_messages_parent "
        "ON chat_messages (thread_id, parent_message_id)",
    ),
    (
        "uq_chat_messages_branch_assistant",
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
        "uq_chat_messages_branch_assistant "
        "ON chat_messages (parent_message_id) "
        "WHERE parent_message_id IS NOT NULL AND role = 'assistant'",
    ),
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


def upgrade() -> None:
    op.execute(
        "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS parent_message_id UUID"
    )
    op.execute(
        "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS selected_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE threads ADD COLUMN IF NOT EXISTS message_branching "
        "BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute("ALTER TABLE threads ADD COLUMN IF NOT EXISTS branch_document_id UUID")
    op.execute("ALTER TABLE threads ADD COLUMN IF NOT EXISTS selected_message_id UUID")
    for table, column, target, name in FOREIGN_KEYS:
        # Static identifiers only; compatible with create_all bootstraps.
        op.execute(f"""DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{name}' AND conrelid = '{table}'::regclass
            ) THEN
                ALTER TABLE {table} ADD CONSTRAINT {name}
                FOREIGN KEY ({column}) REFERENCES {target}(id) ON DELETE SET NULL
                NOT VALID;
            END IF;
            END $$""")
    # Commits the ACCESS EXCLUSIVE column/constraint transaction above; every
    # statement below runs and commits on its own.
    with op.get_context().autocommit_block():
        for table, _column, _target, name in FOREIGN_KEYS:
            # No-op for an already validated (e.g. create_all) constraint.
            op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")
        for name, create in INDEXES:
            _drop_index_if_invalid(name)
            op.execute(create)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS uq_chat_messages_branch_assistant"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chat_messages_parent")
    op.execute(
        "ALTER TABLE threads DROP CONSTRAINT IF EXISTS fk_threads_selected_message_id"
    )
    op.execute(
        "ALTER TABLE threads DROP CONSTRAINT IF EXISTS fk_threads_branch_document_id"
    )
    op.execute(
        "ALTER TABLE chat_messages DROP CONSTRAINT IF EXISTS fk_chat_messages_parent_message_id"
    )
    op.execute("ALTER TABLE threads DROP COLUMN IF EXISTS selected_message_id")
    op.execute("ALTER TABLE threads DROP COLUMN IF EXISTS branch_document_id")
    op.execute("ALTER TABLE threads DROP COLUMN IF EXISTS message_branching")
    op.execute("ALTER TABLE chat_messages DROP COLUMN IF EXISTS selected_at")
    op.execute("ALTER TABLE chat_messages DROP COLUMN IF EXISTS parent_message_id")
