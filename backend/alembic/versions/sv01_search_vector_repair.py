"""Repair the thread/message full-text search objects

Revision ID: sv01_search_vector_repair
Revises: rp01_thread_fk_ondelete
Create Date: 2026-10-09

Q-P3. On the dev database every full-text search route (``/api/v2/search/
threads``, ``/messages``, ``/combined``) answered 500 and ``/api/v2/search/
health`` reported ``search_functional: false``: its probe,
``SELECT COUNT(*) FROM threads WHERE search_vector @@ plainto_tsquery(...)``,
failed, so ``threads.search_vector`` was missing or not a tsvector, although
``alembic_version`` sat at head. The database was restored from a dump during
the DO -> AWS move (docs/plans/2026-09-14-do-to-aws-migration.md); the ORM
models never declared these objects, so nothing else recreates them.

This revision restores, idempotently and for every drift state, exactly what
``b2c3d4e5f6g7_add_fulltext_search_for_threads`` defines for ``threads`` and
``chat_messages``:

- ``search_vector tsvector``: added when absent; dropped and re-added when
  present under another type (it is derived data, rebuilt below).
- the trigger functions ``update_thread_search_vector()`` (title weight A,
  summary weight B, config ``english``) and
  ``update_chat_message_search_vector()`` (content, config ``english``), via
  ``CREATE OR REPLACE``;
- the ``BEFORE INSERT OR UPDATE OF ...`` row triggers, via
  ``DROP TRIGGER IF EXISTS`` + ``CREATE TRIGGER``;
- the GIN indexes ``idx_threads_search_vector``,
  ``idx_chat_messages_search_vector`` (on the column) and
  ``idx_threads_title_gin``, ``idx_chat_messages_content_gin`` (expression
  indexes), via ``CREATE INDEX IF NOT EXISTS``. A same-named index that is
  INVALID (an interrupted build) or not GIN is dropped first, so ``IF NOT
  EXISTS`` cannot keep a broken one.
- the backfill, restricted to ``search_vector IS NULL`` with the triggers'
  own expressions. A correct schema is therefore a no-op: no existing vector
  is rewritten.

Each table is repaired in one ``DO`` block, so the whole repair renders
offline (``alembic upgrade --sql``) and skips with a NOTICE when the table is
absent. PostgreSQL only; other dialects return without doing anything.

Cost on a live database: the index builds are not CONCURRENTLY (the original
were not either, and the chain runs in one transaction), so they take a
SHARE lock on each table while they build; the backfill rewrites only rows
whose vector is NULL. Expect one pass over threads and chat_messages when the
column had to be recreated. A dependent view on a wrongly typed column makes
``DROP COLUMN`` fail loudly rather than cascade.

downgrade() is a no-op: the objects belong to b2c3d4e5f6g7, whose own
downgrade removes them.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "sv01_search_vector_repair"
down_revision = "rp01_thread_fk_ondelete"
branch_labels = None
depends_on = None


# Dollar-quote tags: the DO body ($repair$) nests the function body ($body$).
# Everything else is the b2c3d4e5f6g7 text, unqualified like the original so it
# resolves through search_path.
_THREADS = """
DO $repair$
DECLARE
    column_type oid;
    index_name text;
BEGIN
    IF to_regclass('threads') IS NULL THEN
        RAISE NOTICE 'sv01_search_vector_repair: threads missing, nothing to repair';
        RETURN;
    END IF;

    -- search_vector tsvector: add when absent, rebuild when wrongly typed.
    SELECT atttypid INTO column_type
    FROM pg_attribute
    WHERE attrelid = to_regclass('threads')
      AND attname = 'search_vector'
      AND NOT attisdropped;
    IF column_type IS NOT NULL AND column_type <> 'tsvector'::regtype THEN
        RAISE NOTICE 'sv01_search_vector_repair: threads.search_vector is %, rebuilding',
            column_type::regtype;
        ALTER TABLE threads DROP COLUMN search_vector;
        column_type := NULL;
    END IF;
    IF column_type IS NULL THEN
        ALTER TABLE threads ADD COLUMN search_vector tsvector;
    END IF;

    CREATE OR REPLACE FUNCTION update_thread_search_vector()
    RETURNS TRIGGER AS $body$
    BEGIN
        NEW.search_vector :=
            setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(NEW.summary, '')), 'B');
        RETURN NEW;
    END;
    $body$ LANGUAGE plpgsql;

    DROP TRIGGER IF EXISTS trigger_update_thread_search_vector ON threads;
    CREATE TRIGGER trigger_update_thread_search_vector
    BEFORE INSERT OR UPDATE OF title, summary ON threads
    FOR EACH ROW
    EXECUTE FUNCTION update_thread_search_vector();

    -- A same-named index that is invalid or not GIN would survive IF NOT EXISTS.
    FOREACH index_name IN ARRAY
        ARRAY['idx_threads_search_vector', 'idx_threads_title_gin']
    LOOP
        IF EXISTS (
            SELECT 1 FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_am am ON am.oid = c.relam
            WHERE c.oid = to_regclass(index_name)
              AND (NOT i.indisvalid OR am.amname <> 'gin')
        ) THEN
            RAISE NOTICE 'sv01_search_vector_repair: dropping unusable index %', index_name;
            EXECUTE format('DROP INDEX %I', index_name);
        END IF;
    END LOOP;
    CREATE INDEX IF NOT EXISTS idx_threads_search_vector
    ON threads USING GIN (search_vector);
    CREATE INDEX IF NOT EXISTS idx_threads_title_gin
    ON threads USING GIN (to_tsvector('english', COALESCE(title, '')));

    UPDATE threads
    SET search_vector =
        setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(summary, '')), 'B')
    WHERE search_vector IS NULL;
END
$repair$;
"""

_CHAT_MESSAGES = """
DO $repair$
DECLARE
    column_type oid;
    index_name text;
BEGIN
    IF to_regclass('chat_messages') IS NULL THEN
        RAISE NOTICE 'sv01_search_vector_repair: chat_messages missing, nothing to repair';
        RETURN;
    END IF;

    SELECT atttypid INTO column_type
    FROM pg_attribute
    WHERE attrelid = to_regclass('chat_messages')
      AND attname = 'search_vector'
      AND NOT attisdropped;
    IF column_type IS NOT NULL AND column_type <> 'tsvector'::regtype THEN
        RAISE NOTICE 'sv01_search_vector_repair: chat_messages.search_vector is %, rebuilding',
            column_type::regtype;
        ALTER TABLE chat_messages DROP COLUMN search_vector;
        column_type := NULL;
    END IF;
    IF column_type IS NULL THEN
        ALTER TABLE chat_messages ADD COLUMN search_vector tsvector;
    END IF;

    CREATE OR REPLACE FUNCTION update_chat_message_search_vector()
    RETURNS TRIGGER AS $body$
    BEGIN
        NEW.search_vector := to_tsvector('english', COALESCE(NEW.content, ''));
        RETURN NEW;
    END;
    $body$ LANGUAGE plpgsql;

    DROP TRIGGER IF EXISTS trigger_update_chat_message_search_vector ON chat_messages;
    CREATE TRIGGER trigger_update_chat_message_search_vector
    BEFORE INSERT OR UPDATE OF content ON chat_messages
    FOR EACH ROW
    EXECUTE FUNCTION update_chat_message_search_vector();

    FOREACH index_name IN ARRAY
        ARRAY['idx_chat_messages_search_vector', 'idx_chat_messages_content_gin']
    LOOP
        IF EXISTS (
            SELECT 1 FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_am am ON am.oid = c.relam
            WHERE c.oid = to_regclass(index_name)
              AND (NOT i.indisvalid OR am.amname <> 'gin')
        ) THEN
            RAISE NOTICE 'sv01_search_vector_repair: dropping unusable index %', index_name;
            EXECUTE format('DROP INDEX %I', index_name);
        END IF;
    END LOOP;
    CREATE INDEX IF NOT EXISTS idx_chat_messages_search_vector
    ON chat_messages USING GIN (search_vector);
    CREATE INDEX IF NOT EXISTS idx_chat_messages_content_gin
    ON chat_messages USING GIN (to_tsvector('english', COALESCE(content, '')));

    UPDATE chat_messages
    SET search_vector = to_tsvector('english', COALESCE(content, ''))
    WHERE search_vector IS NULL;
END
$repair$;
"""


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_THREADS)
    op.execute(_CHAT_MESSAGES)


def downgrade() -> None:
    # Repair-only revision: the column, functions, triggers and indexes are
    # owned by b2c3d4e5f6g7_add_fulltext_search_for_threads, whose downgrade
    # removes them. Nothing to undo here.
    pass
