"""Offline checks of the sv01_search_vector_repair migration (Q-P3).

``alembic upgrade FROM:TO --sql`` renders the revision without a database.
The repair is pure SQL (three ``DO`` blocks), so the render must carry every
object b2c3d4e5f6g7 defines, with the same builders, the ownership preflight
ahead of any DDL, the no-op guards, and the downgrade must render no DDL.
Same subprocess pattern as ``test_thread_fk_ondelete_migration``: never
import alembic in this process
(``tests/unit/ci/test_migration_tests_collect_together.py``).

NOT RUN here: executing the revision against PostgreSQL. That is
``tests/integration/test_search_vector_repair_postgres.py``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT = "rp01_thread_fk_ondelete"
REVISION = "sv01_search_vector_repair"


def _alembic(*alembic_args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k != "SUPABASE_DB_URL"}
    # Only the dialect is read from the URL; nothing connects.
    env["DATABASE_URL"] = "postgresql://offline.invalid/nous"
    done = subprocess.run(
        [sys.executable, "-m", "alembic", *alembic_args, "--sql"],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    # Drop the "-- Running upgrade ..." comment lines, join string literals
    # that PostgreSQL concatenates (adjacent, newline-separated), then squash
    # whitespace.
    code = "\n".join(
        line for line in done.stdout.splitlines() if not line.lstrip().startswith("--")
    )
    code = re.sub(r"'[ \t]*\n\s*'", "", code)
    return re.sub(r"\s+", " ", code).strip()


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return _alembic("upgrade", f"{PARENT}:{REVISION}")


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    return _alembic("downgrade", f"{REVISION}:{PARENT}")


def _block(upgrade_sql: str, table: str) -> str:
    """The DO block that repairs ``table``."""
    blocks = upgrade_sql.split("DO $repair$")
    matching = [b for b in blocks if f"to_regclass('{table}')" in b.split("BEGIN")[0]]
    assert len(matching) == 1, f"one DO block for {table}, found {len(matching)}"
    return matching[0]


def test_revision_id_fits_the_version_table() -> None:
    assert len(REVISION) <= 32


@pytest.mark.parametrize("table", ["threads", "chat_messages", "citations"])
def test_ownership_is_checked_before_any_ddl(upgrade_sql: str, table: str) -> None:
    block = _block(upgrade_sql, table)
    check = block.index("IF NOT pg_has_role(current_user, owner_oid, 'USAGE') THEN")
    assert f"table {table} is owned by %" in block
    assert f"Run: ALTER TABLE {table} OWNER TO %;" in block
    first_ddl = min(
        i
        for i in (
            block.find("ALTER TABLE "),
            block.find("CREATE OR REPLACE FUNCTION"),
            block.find("DROP TRIGGER"),
            block.find("CREATE INDEX"),
            block.find("DROP INDEX"),
            block.find("UPDATE "),
        )
        if i >= 0
    )
    assert check < first_ddl


@pytest.mark.parametrize(
    "table,function",
    [
        ("threads", "update_thread_search_vector"),
        ("chat_messages", "update_chat_message_search_vector"),
    ],
)
def test_function_ownership_and_schema_create_are_checked(
    upgrade_sql: str, table: str, function: str
) -> None:
    block = _block(upgrade_sql, table)
    assert f"function_oid oid := to_regprocedure('{function}()')" in block
    assert f"function {function}() is owned by %" in block
    assert f"Run: ALTER FUNCTION {function}() OWNER TO %;" in block
    assert "ELSIF NOT has_schema_privilege(current_user, schema_oid, 'CREATE')" in block
    assert "Run: GRANT CREATE ON SCHEMA % TO %;" in block


@pytest.mark.parametrize("table", ["threads", "chat_messages"])
def test_column_is_added_or_rebuilt_as_tsvector(upgrade_sql: str, table: str) -> None:
    block = _block(upgrade_sql, table)
    assert f"table_oid oid := to_regclass('{table}')" in block
    assert "IF table_oid IS NULL THEN" in block
    assert "column_type <> 'tsvector'::regtype" in block
    assert f"ALTER TABLE {table} DROP COLUMN search_vector" in block
    assert f"ALTER TABLE {table} ADD COLUMN search_vector tsvector" in block
    assert "column_added := true" in block


def test_trigger_functions_match_b2c3d4e5f6g7(upgrade_sql: str) -> None:
    assert "CREATE OR REPLACE FUNCTION update_thread_search_vector()" in upgrade_sql
    assert (
        "NEW.search_vector := setweight(to_tsvector('english', COALESCE(NEW.title, "
        "'')), 'A') || setweight(to_tsvector('english', COALESCE(NEW.summary, '')), "
        "'B');"
    ) in upgrade_sql
    assert (
        "CREATE OR REPLACE FUNCTION update_chat_message_search_vector()" in upgrade_sql
    )
    assert (
        "NEW.search_vector := to_tsvector('english', COALESCE(NEW.content, ''));"
        in upgrade_sql
    )


@pytest.mark.parametrize(
    "table,trigger,function,events",
    [
        (
            "threads",
            "trigger_update_thread_search_vector",
            "update_thread_search_vector",
            "title, summary",
        ),
        (
            "chat_messages",
            "trigger_update_chat_message_search_vector",
            "update_chat_message_search_vector",
            "content",
        ),
    ],
)
def test_triggers_are_recreated_only_when_not_already_bound(
    upgrade_sql: str, table: str, trigger: str, function: str, events: str
) -> None:
    assert (
        f"IF NOT EXISTS ( SELECT 1 FROM pg_trigger WHERE tgrelid = table_oid AND "
        f"tgname = '{trigger}' AND tgfoid = function_oid AND tgtype = 23 AND NOT "
        f"tgisinternal ) THEN DROP TRIGGER IF EXISTS {trigger} ON {table}; "
        f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE OF {events} ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {function}(); END IF;"
    ) in upgrade_sql


@pytest.mark.parametrize(
    "index,definition",
    [
        ("idx_threads_search_vector", "ON threads USING GIN (search_vector)"),
        (
            "idx_threads_title_gin",
            "ON threads USING GIN (to_tsvector('english', COALESCE(title, '')))",
        ),
        ("idx_threads_conversation_status", "ON threads (conversation_id, status)"),
        (
            "idx_chat_messages_search_vector",
            "ON chat_messages USING GIN (search_vector)",
        ),
        (
            "idx_chat_messages_content_gin",
            "ON chat_messages USING GIN (to_tsvector('english', COALESCE(content, "
            "'')))",
        ),
        ("idx_chat_messages_thread_role", "ON chat_messages (thread_id, role)"),
        (
            "idx_citations_snippet_gin",
            "ON citations USING GIN (to_tsvector('english', COALESCE(snippet, '')))",
        ),
    ],
)
def test_indexes_are_created_only_when_absent_and_unusable_ones_dropped(
    upgrade_sql: str, index: str, definition: str
) -> None:
    assert (
        f"IF to_regclass('{index}') IS NULL THEN CREATE INDEX {index} {definition}; "
        "END IF;"
    ) in upgrade_sql
    assert "CREATE INDEX IF NOT EXISTS" not in upgrade_sql  # touches the table
    assert f"'{index}'" in upgrade_sql  # named in the invalid/wrong-method sweep
    assert "AND (NOT i.indisvalid OR am.amname <> idx.am)" in upgrade_sql
    assert "EXECUTE format('DROP INDEX %I', idx.name)" in upgrade_sql


def test_backfill_runs_only_when_needed_and_touches_only_null_vectors(
    upgrade_sql: str,
) -> None:
    assert (
        "IF column_added OR EXISTS (SELECT 1 FROM threads WHERE search_vector IS "
        "NULL) THEN UPDATE threads SET search_vector = setweight(to_tsvector("
        "'english', COALESCE(title, '')), 'A') || setweight(to_tsvector('english', "
        "COALESCE(summary, '')), 'B') WHERE search_vector IS NULL; END IF;"
    ) in upgrade_sql
    assert (
        "IF column_added OR EXISTS (SELECT 1 FROM chat_messages WHERE search_vector "
        "IS NULL) THEN UPDATE chat_messages SET search_vector = to_tsvector("
        "'english', COALESCE(content, '')) WHERE search_vector IS NULL; END IF;"
    ) in upgrade_sql
    # never a blanket rewrite
    assert not re.search(
        r"UPDATE (threads|chat_messages) SET search_vector = "
        r"(?!.*WHERE search_vector IS NULL)[^;]*;",
        upgrade_sql,
    )


def test_downgrade_only_restamps(downgrade_sql: str) -> None:
    statements = [s.strip() for s in downgrade_sql.split(";") if s.strip()]
    assert [s for s in statements if s not in ("BEGIN", "COMMIT")] == [
        f"UPDATE alembic_version SET version_num='{PARENT}' "
        f"WHERE alembic_version.version_num = '{REVISION}'"
    ]
