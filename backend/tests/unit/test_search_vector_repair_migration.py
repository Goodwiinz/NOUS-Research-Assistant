"""Offline checks of the sv01_search_vector_repair migration (Q-P3).

``alembic upgrade FROM:TO --sql`` renders the revision without a database.
The repair is pure SQL (two ``DO`` blocks), so the render must carry every
object b2c3d4e5f6g7 defines, with the same builders, and the downgrade must
render no DDL. Same subprocess pattern as ``test_thread_fk_ondelete_migration``:
never import alembic in this process
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
    # Drop the "-- Running upgrade ..." comment lines, then squash whitespace.
    code = "\n".join(
        line for line in done.stdout.splitlines() if not line.lstrip().startswith("--")
    )
    return re.sub(r"\s+", " ", code).strip()


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return _alembic("upgrade", f"{PARENT}:{REVISION}")


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    return _alembic("downgrade", f"{REVISION}:{PARENT}")


def test_revision_id_fits_the_version_table() -> None:
    assert len(REVISION) <= 32


@pytest.mark.parametrize("table", ["threads", "chat_messages"])
def test_column_is_added_or_rebuilt_as_tsvector(upgrade_sql: str, table: str) -> None:
    assert f"IF to_regclass('{table}') IS NULL THEN" in upgrade_sql
    assert f"attrelid = to_regclass('{table}')" in upgrade_sql
    assert "column_type <> 'tsvector'::regtype" in upgrade_sql
    assert f"ALTER TABLE {table} DROP COLUMN search_vector" in upgrade_sql
    assert f"ALTER TABLE {table} ADD COLUMN search_vector tsvector" in upgrade_sql


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


def test_triggers_are_dropped_then_created(upgrade_sql: str) -> None:
    assert (
        "DROP TRIGGER IF EXISTS trigger_update_thread_search_vector ON threads; "
        "CREATE TRIGGER trigger_update_thread_search_vector BEFORE INSERT OR UPDATE "
        "OF title, summary ON threads FOR EACH ROW EXECUTE FUNCTION "
        "update_thread_search_vector();"
    ) in upgrade_sql
    assert (
        "DROP TRIGGER IF EXISTS trigger_update_chat_message_search_vector ON "
        "chat_messages; CREATE TRIGGER trigger_update_chat_message_search_vector "
        "BEFORE INSERT OR UPDATE OF content ON chat_messages FOR EACH ROW EXECUTE "
        "FUNCTION update_chat_message_search_vector();"
    ) in upgrade_sql


@pytest.mark.parametrize(
    "index,definition",
    [
        ("idx_threads_search_vector", "ON threads USING GIN (search_vector)"),
        (
            "idx_threads_title_gin",
            "ON threads USING GIN (to_tsvector('english', COALESCE(title, '')))",
        ),
        (
            "idx_chat_messages_search_vector",
            "ON chat_messages USING GIN (search_vector)",
        ),
        (
            "idx_chat_messages_content_gin",
            "ON chat_messages USING GIN (to_tsvector('english', COALESCE(content, "
            "'')))",
        ),
    ],
)
def test_gin_indexes_are_created_if_missing_and_unusable_ones_dropped(
    upgrade_sql: str, index: str, definition: str
) -> None:
    assert f"CREATE INDEX IF NOT EXISTS {index} {definition}" in upgrade_sql
    assert f"'{index}'" in upgrade_sql  # named in the invalid/not-GIN sweep
    assert "AND (NOT i.indisvalid OR am.amname <> 'gin')" in upgrade_sql
    assert "EXECUTE format('DROP INDEX %I', index_name)" in upgrade_sql


def test_backfill_touches_only_null_vectors(upgrade_sql: str) -> None:
    assert (
        "UPDATE threads SET search_vector = setweight(to_tsvector('english', "
        "COALESCE(title, '')), 'A') || setweight(to_tsvector('english', "
        "COALESCE(summary, '')), 'B') WHERE search_vector IS NULL;"
    ) in upgrade_sql
    assert (
        "UPDATE chat_messages SET search_vector = to_tsvector('english', "
        "COALESCE(content, '')) WHERE search_vector IS NULL;"
    ) in upgrade_sql
    # never a blanket rewrite
    assert not re.search(
        r"UPDATE (threads|chat_messages) SET search_vector = (?!.*WHERE search_vector IS NULL)[^;]*;",
        upgrade_sql,
    )


def test_downgrade_only_restamps(downgrade_sql: str) -> None:
    statements = [s.strip() for s in downgrade_sql.split(";") if s.strip()]
    assert [s for s in statements if s not in ("BEGIN", "COMMIT")] == [
        f"UPDATE alembic_version SET version_num='{PARENT}' "
        f"WHERE alembic_version.version_num = '{REVISION}'"
    ]
