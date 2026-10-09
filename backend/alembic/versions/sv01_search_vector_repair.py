"""Repair the thread/message full-text search objects

Revision ID: sv01_search_vector_repair
Revises: rp01_thread_fk_ondelete
Create Date: 2026-10-09

Q-P3. On the dev database every full-text search route (``/api/v2/search/
threads``, ``/messages``, ``/combined``) answered 500 and ``/api/v2/search/
health`` reported ``search_functional: false``: its probe,
``SELECT COUNT(*) FROM threads WHERE search_vector @@ plainto_tsquery(...)``,
failed, so ``threads.search_vector`` was missing or not a tsvector, although
``alembic_version`` sat at head. The database was restored with ``pg_restore
--no-owner`` during the DO -> AWS move
(docs/plans/2026-09-14-do-to-aws-migration.md); the ORM models never declared
these objects, so nothing else recreates them.

This revision restores, idempotently and for every drift state, what
``b2c3d4e5f6g7_add_fulltext_search_for_threads`` defines:

- ``threads.search_vector`` / ``chat_messages.search_vector`` tsvector: added
  when absent; dropped and re-added when present under another type (it is
  derived data, rebuilt by the backfill below).
- the trigger functions ``update_thread_search_vector()`` (title weight A,
  summary weight B, config ``english``) and
  ``update_chat_message_search_vector()`` (content, config ``english``), via
  ``CREATE OR REPLACE``;
- the ``BEFORE INSERT OR UPDATE OF ...`` row triggers, recreated only when
  no trigger of that name is bound to that function with those events;
- the indexes ``idx_threads_search_vector``, ``idx_threads_title_gin``,
  ``idx_threads_conversation_status``, ``idx_chat_messages_search_vector``,
  ``idx_chat_messages_content_gin``, ``idx_chat_messages_thread_role`` and
  ``idx_citations_snippet_gin``, each created only when no relation of that
  name exists. A namesake that is INVALID (an interrupted build) or uses the
  wrong access method is dropped first. A valid namesake with the right
  method but another definition is kept (not checked).
- the backfill, with the triggers' own expressions, only when the column was
  just created or some row still has a NULL vector, and then only for those
  rows.

On a correct schema the only statement that runs is ``CREATE OR REPLACE
FUNCTION`` (a catalog write on the function; no table is locked, no row is
rewritten, triggers and indexes keep their OIDs).

Ownership. The restore left the objects owned by whichever role ran it, and
every ALTER TABLE / DROP TRIGGER / CREATE INDEX here needs the table's owner,
``CREATE OR REPLACE`` needs the function's owner, and creating a missing
function needs CREATE on its schema. Each block checks that first and raises
a plain error naming the object, its owner, ``current_user`` and the
``ALTER ... OWNER TO`` remedy, before any DDL; the deploy init container
(``alembic upgrade heads``) then fails on that message instead of a
mid-block "must be owner of ...". The check runs even when nothing needs
repair: a migration runner that cannot own these tables fails the next
ALTER the same way.

Preflight, read-only, to run on the target as the application role before
deploying (every ``usable`` must be true, and ``search_vector`` must read
``tsvector`` or be absent)::

    SELECT c.relname AS object, pg_get_userbyid(c.relowner) AS owner,
           pg_has_role(current_user, c.relowner, 'USAGE') AS usable
    FROM pg_class c
    WHERE c.oid IN (to_regclass('threads'), to_regclass('chat_messages'),
                    to_regclass('citations'))
    UNION ALL
    SELECT p.proname || '()', pg_get_userbyid(p.proowner),
           pg_has_role(current_user, p.proowner, 'USAGE')
    FROM pg_proc p
    WHERE p.oid IN (to_regprocedure('update_thread_search_vector()'),
                    to_regprocedure('update_chat_message_search_vector()'))
    UNION ALL
    SELECT 'schema ' || n.nspname, pg_get_userbyid(n.nspowner),
           has_schema_privilege(current_user, n.oid, 'CREATE')
    FROM pg_namespace n
    WHERE n.oid = (SELECT relnamespace FROM pg_class
                   WHERE oid = to_regclass('threads'));

    SELECT table_name, udt_name FROM information_schema.columns
    WHERE table_name IN ('threads', 'chat_messages')
      AND column_name = 'search_vector';

Cost on a live database: index builds are not CONCURRENTLY (the originals
were not either, and the chain runs in one transaction), so each missing
index takes a SHARE lock on its table while it builds; the backfill rewrites
only rows whose vector is NULL. Expect one pass over threads and
chat_messages when the column had to be recreated. A dependent view on a
wrongly typed column makes ``DROP COLUMN`` fail loudly rather than cascade.

Each table is one ``DO`` block, so the whole repair renders offline
(``alembic upgrade --sql``) and skips with a NOTICE when the table is absent.
PostgreSQL only; other dialects return without doing anything. downgrade()
is a no-op: the objects belong to b2c3d4e5f6g7, whose own downgrade removes
them.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "sv01_search_vector_repair"
down_revision = "rp01_thread_fk_ondelete"
branch_labels = None
depends_on = None

# pg_trigger.tgtype of "BEFORE INSERT OR UPDATE ... FOR EACH ROW":
# TRIGGER_TYPE_ROW (1) | BEFORE (2) | INSERT (4) | UPDATE (16).
_BEFORE_INSERT_OR_UPDATE_ROW = 23

# Dollar-quote tags: the DO body ($repair$) nests the function body ($body$).
# Object names are unqualified like the original so they resolve through
# search_path.
_THREADS = f"""
DO $repair$
DECLARE
    table_oid oid := to_regclass('threads');
    owner_oid oid;
    schema_oid oid;
    function_oid oid := to_regprocedure('update_thread_search_vector()');
    column_type oid;
    column_added boolean := false;
    idx record;
BEGIN
    IF table_oid IS NULL THEN
        RAISE NOTICE 'sv01_search_vector_repair: threads missing, nothing to repair';
        RETURN;
    END IF;

    -- Ownership preflight: fail here, by name, rather than inside the DDL.
    SELECT relowner, relnamespace INTO owner_oid, schema_oid
    FROM pg_class WHERE oid = table_oid;
    IF NOT pg_has_role(current_user, owner_oid, 'USAGE') THEN
        RAISE EXCEPTION 'sv01_search_vector_repair: table threads is owned by % '
            'and this migration runs as %, which is not a member of that role. '
            'Run: ALTER TABLE threads OWNER TO %; (or run the migration as %) '
            'and retry.',
            pg_get_userbyid(owner_oid), current_user, current_user,
            pg_get_userbyid(owner_oid);
    END IF;
    IF function_oid IS NOT NULL THEN
        SELECT proowner INTO owner_oid FROM pg_proc WHERE oid = function_oid;
        IF NOT pg_has_role(current_user, owner_oid, 'USAGE') THEN
            RAISE EXCEPTION 'sv01_search_vector_repair: function '
                'update_thread_search_vector() is owned by % and this migration '
                'runs as %, which is not a member of that role. Run: ALTER '
                'FUNCTION update_thread_search_vector() OWNER TO %; (or run the '
                'migration as %) and retry.',
                pg_get_userbyid(owner_oid), current_user, current_user,
                pg_get_userbyid(owner_oid);
        END IF;
    ELSIF NOT has_schema_privilege(current_user, schema_oid, 'CREATE') THEN
        RAISE EXCEPTION 'sv01_search_vector_repair: function '
            'update_thread_search_vector() is missing and this migration runs '
            'as %, which cannot CREATE in schema %. Run: GRANT CREATE ON SCHEMA '
            '% TO %; and retry.',
            current_user, schema_oid::regnamespace, schema_oid::regnamespace,
            current_user;
    END IF;

    -- search_vector tsvector: add when absent, rebuild when wrongly typed.
    SELECT atttypid INTO column_type
    FROM pg_attribute
    WHERE attrelid = table_oid
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
        column_added := true;
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
    function_oid := to_regprocedure('update_thread_search_vector()');

    -- Recreate the trigger only when none of that name is bound to that
    -- function with those events (tgtype {_BEFORE_INSERT_OR_UPDATE_ROW}).
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = table_oid
          AND tgname = 'trigger_update_thread_search_vector'
          AND tgfoid = function_oid
          AND tgtype = {_BEFORE_INSERT_OR_UPDATE_ROW}
          AND NOT tgisinternal
    ) THEN
        DROP TRIGGER IF EXISTS trigger_update_thread_search_vector ON threads;
        CREATE TRIGGER trigger_update_thread_search_vector
        BEFORE INSERT OR UPDATE OF title, summary ON threads
        FOR EACH ROW
        EXECUTE FUNCTION update_thread_search_vector();
    END IF;

    -- A namesake that is invalid or uses the wrong access method would
    -- otherwise survive the IS NULL guards below.
    FOR idx IN
        SELECT * FROM (VALUES
            ('idx_threads_search_vector', 'gin'),
            ('idx_threads_title_gin', 'gin'),
            ('idx_threads_conversation_status', 'btree')
        ) AS expected(name, am)
    LOOP
        IF EXISTS (
            SELECT 1 FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_am am ON am.oid = c.relam
            WHERE c.oid = to_regclass(idx.name)
              AND (NOT i.indisvalid OR am.amname <> idx.am)
        ) THEN
            RAISE NOTICE 'sv01_search_vector_repair: dropping unusable index %', idx.name;
            EXECUTE format('DROP INDEX %I', idx.name);
        END IF;
    END LOOP;
    IF to_regclass('idx_threads_search_vector') IS NULL THEN
        CREATE INDEX idx_threads_search_vector
        ON threads USING GIN (search_vector);
    END IF;
    IF to_regclass('idx_threads_title_gin') IS NULL THEN
        CREATE INDEX idx_threads_title_gin
        ON threads USING GIN (to_tsvector('english', COALESCE(title, '')));
    END IF;
    IF to_regclass('idx_threads_conversation_status') IS NULL THEN
        CREATE INDEX idx_threads_conversation_status
        ON threads (conversation_id, status);
    END IF;

    IF column_added OR EXISTS (SELECT 1 FROM threads WHERE search_vector IS NULL) THEN
        UPDATE threads
        SET search_vector =
            setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(summary, '')), 'B')
        WHERE search_vector IS NULL;
    END IF;
END
$repair$;
"""

_CHAT_MESSAGES = f"""
DO $repair$
DECLARE
    table_oid oid := to_regclass('chat_messages');
    owner_oid oid;
    schema_oid oid;
    function_oid oid := to_regprocedure('update_chat_message_search_vector()');
    column_type oid;
    column_added boolean := false;
    idx record;
BEGIN
    IF table_oid IS NULL THEN
        RAISE NOTICE 'sv01_search_vector_repair: chat_messages missing, nothing to repair';
        RETURN;
    END IF;

    SELECT relowner, relnamespace INTO owner_oid, schema_oid
    FROM pg_class WHERE oid = table_oid;
    IF NOT pg_has_role(current_user, owner_oid, 'USAGE') THEN
        RAISE EXCEPTION 'sv01_search_vector_repair: table chat_messages is owned by '
            '% and this migration runs as %, which is not a member of that role. '
            'Run: ALTER TABLE chat_messages OWNER TO %; (or run the migration as '
            '%) and retry.',
            pg_get_userbyid(owner_oid), current_user, current_user,
            pg_get_userbyid(owner_oid);
    END IF;
    IF function_oid IS NOT NULL THEN
        SELECT proowner INTO owner_oid FROM pg_proc WHERE oid = function_oid;
        IF NOT pg_has_role(current_user, owner_oid, 'USAGE') THEN
            RAISE EXCEPTION 'sv01_search_vector_repair: function '
                'update_chat_message_search_vector() is owned by % and this '
                'migration runs as %, which is not a member of that role. Run: '
                'ALTER FUNCTION update_chat_message_search_vector() OWNER TO %; '
                '(or run the migration as %) and retry.',
                pg_get_userbyid(owner_oid), current_user, current_user,
                pg_get_userbyid(owner_oid);
        END IF;
    ELSIF NOT has_schema_privilege(current_user, schema_oid, 'CREATE') THEN
        RAISE EXCEPTION 'sv01_search_vector_repair: function '
            'update_chat_message_search_vector() is missing and this migration '
            'runs as %, which cannot CREATE in schema %. Run: GRANT CREATE ON '
            'SCHEMA % TO %; and retry.',
            current_user, schema_oid::regnamespace, schema_oid::regnamespace,
            current_user;
    END IF;

    SELECT atttypid INTO column_type
    FROM pg_attribute
    WHERE attrelid = table_oid
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
        column_added := true;
    END IF;

    CREATE OR REPLACE FUNCTION update_chat_message_search_vector()
    RETURNS TRIGGER AS $body$
    BEGIN
        NEW.search_vector := to_tsvector('english', COALESCE(NEW.content, ''));
        RETURN NEW;
    END;
    $body$ LANGUAGE plpgsql;
    function_oid := to_regprocedure('update_chat_message_search_vector()');

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = table_oid
          AND tgname = 'trigger_update_chat_message_search_vector'
          AND tgfoid = function_oid
          AND tgtype = {_BEFORE_INSERT_OR_UPDATE_ROW}
          AND NOT tgisinternal
    ) THEN
        DROP TRIGGER IF EXISTS trigger_update_chat_message_search_vector ON chat_messages;
        CREATE TRIGGER trigger_update_chat_message_search_vector
        BEFORE INSERT OR UPDATE OF content ON chat_messages
        FOR EACH ROW
        EXECUTE FUNCTION update_chat_message_search_vector();
    END IF;

    FOR idx IN
        SELECT * FROM (VALUES
            ('idx_chat_messages_search_vector', 'gin'),
            ('idx_chat_messages_content_gin', 'gin'),
            ('idx_chat_messages_thread_role', 'btree')
        ) AS expected(name, am)
    LOOP
        IF EXISTS (
            SELECT 1 FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_am am ON am.oid = c.relam
            WHERE c.oid = to_regclass(idx.name)
              AND (NOT i.indisvalid OR am.amname <> idx.am)
        ) THEN
            RAISE NOTICE 'sv01_search_vector_repair: dropping unusable index %', idx.name;
            EXECUTE format('DROP INDEX %I', idx.name);
        END IF;
    END LOOP;
    IF to_regclass('idx_chat_messages_search_vector') IS NULL THEN
        CREATE INDEX idx_chat_messages_search_vector
        ON chat_messages USING GIN (search_vector);
    END IF;
    IF to_regclass('idx_chat_messages_content_gin') IS NULL THEN
        CREATE INDEX idx_chat_messages_content_gin
        ON chat_messages USING GIN (to_tsvector('english', COALESCE(content, '')));
    END IF;
    IF to_regclass('idx_chat_messages_thread_role') IS NULL THEN
        CREATE INDEX idx_chat_messages_thread_role
        ON chat_messages (thread_id, role);
    END IF;

    IF column_added OR EXISTS (SELECT 1 FROM chat_messages WHERE search_vector IS NULL) THEN
        UPDATE chat_messages
        SET search_vector = to_tsvector('english', COALESCE(content, ''))
        WHERE search_vector IS NULL;
    END IF;
END
$repair$;
"""

_CITATIONS = """
DO $repair$
DECLARE
    table_oid oid := to_regclass('citations');
    owner_oid oid;
BEGIN
    IF table_oid IS NULL THEN
        RAISE NOTICE 'sv01_search_vector_repair: citations missing, nothing to repair';
        RETURN;
    END IF;

    SELECT relowner INTO owner_oid FROM pg_class WHERE oid = table_oid;
    IF NOT pg_has_role(current_user, owner_oid, 'USAGE') THEN
        RAISE EXCEPTION 'sv01_search_vector_repair: table citations is owned by % '
            'and this migration runs as %, which is not a member of that role. '
            'Run: ALTER TABLE citations OWNER TO %; (or run the migration as %) '
            'and retry.',
            pg_get_userbyid(owner_oid), current_user, current_user,
            pg_get_userbyid(owner_oid);
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_am am ON am.oid = c.relam
        WHERE c.oid = to_regclass('idx_citations_snippet_gin')
          AND (NOT i.indisvalid OR am.amname <> 'gin')
    ) THEN
        RAISE NOTICE 'sv01_search_vector_repair: dropping unusable index idx_citations_snippet_gin';
        DROP INDEX idx_citations_snippet_gin;
    END IF;
    IF to_regclass('idx_citations_snippet_gin') IS NULL THEN
        CREATE INDEX idx_citations_snippet_gin
        ON citations USING GIN (to_tsvector('english', COALESCE(snippet, '')));
    END IF;
END
$repair$;
"""


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_THREADS)
    op.execute(_CHAT_MESSAGES)
    op.execute(_CITATIONS)


def downgrade() -> None:
    # Repair-only revision: the column, functions, triggers and indexes are
    # owned by b2c3d4e5f6g7_add_fulltext_search_for_threads, whose downgrade
    # removes them. Nothing to undo here.
    pass
