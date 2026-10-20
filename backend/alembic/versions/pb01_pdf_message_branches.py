"""Add opt-in canonical PDF message branches.

Revision ID: pb01_pdf_message_branches
Revises: ho04_handoff_org_slots

Existing threads retain chronological/tombstone behavior. No parent edges
are inferred for historical messages. Nullable SET NULL links preserve the
thread-retention purge; message_branching remains true if its PDF is removed,
so a missing document binding fails closed rather than becoming unscoped.
"""

import alembic.op as op

revision = "pb01_pdf_message_branches"
down_revision = "ho04_handoff_org_slots"
branch_labels = None
depends_on = None


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
    for table, column, target, name in (
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
    ):
        # Static identifiers only; compatible with create_all bootstraps.
        op.execute(f"""DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{name}' AND conrelid = '{table}'::regclass
            ) THEN
                ALTER TABLE {table} ADD CONSTRAINT {name}
                FOREIGN KEY ({column}) REFERENCES {target}(id) ON DELETE SET NULL;
            END IF;
            END $$""")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_messages_parent "
        "ON chat_messages (thread_id, parent_message_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_messages_branch_assistant "
        "ON chat_messages (parent_message_id) "
        "WHERE parent_message_id IS NOT NULL AND role = 'assistant'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_chat_messages_branch_assistant")
    op.execute("DROP INDEX IF EXISTS ix_chat_messages_parent")
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
