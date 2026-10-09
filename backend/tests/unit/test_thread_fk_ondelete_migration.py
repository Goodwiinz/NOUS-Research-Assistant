"""Offline checks of the rp01_thread_fk_ondelete migration (audit HO-2).

``alembic upgrade FROM:TO --sql`` renders the revision's DDL without a
database: each foreign key is dropped by its column (its live name depends on
how the table was made) and recreated under one name with its ON DELETE rule;
the downgrade recreates it with none. Same subprocess pattern as
``test_workspace_grants_migration.py``: never import alembic in this process
(``tests/unit/ci/test_migration_tests_collect_together.py``).

NOT RUN here: executing the revision against PostgreSQL. That is
``tests/integration/test_retention_thread_purge_postgres.py``
(``test_migration_round_trip_sets_and_restores_the_rules``).
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
PARENT = "hb03_workspace_grants"
REVISION = "rp01_thread_fk_ondelete"
# (table, column, referenced table, ON DELETE); mirrors the revision.
FOREIGN_KEYS = (
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


def _alembic(*alembic_args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k != "SUPABASE_DB_URL"}
    # Only the dialect is read from the URL; nothing connects.
    env["DATABASE_URL"] = "postgresql://offline.invalid/nous"
    done = subprocess.run(
        [sys.executable, "-m", "alembic", *alembic_args],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _render(*alembic_args: str) -> str:
    return re.sub(r"\s+", " ", _alembic(*alembic_args, "--sql"))


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return _render("upgrade", f"{PARENT}:{REVISION}")


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    return _render("downgrade", f"{REVISION}:{PARENT}")


def _position(sql: str, statement: str) -> int:
    assert statement in sql, statement
    return sql.index(statement)


def _drop(table: str, column: str, referenced: str) -> str:
    """The column match of the DO block that drops the FK whatever its name."""
    return (
        f"c.conrelid = to_regclass('{table}') "
        f"AND c.confrelid = to_regclass('{referenced}') "
        f"AND array_length(c.conkey, 1) = 1 AND a.attname = '{column}'"
    )


def _add(table: str, column: str, referenced: str) -> str:
    return (
        f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_{column} "
        f"FOREIGN KEY({column}) REFERENCES {referenced} (id)"
    )


def test_revision_follows_the_head_it_was_cut_from() -> None:
    shown = _alembic("show", REVISION)
    parent = re.search(r"^Parent: (.+)$", shown, re.MULTILINE)
    assert parent is not None and parent.group(1).strip() == PARENT, shown
    assert len(REVISION) <= 32


def test_the_revision_chain_has_a_single_head() -> None:
    heads = [line for line in _alembic("heads").splitlines() if line.strip()]
    assert len(heads) == 1, heads


@pytest.mark.parametrize(("table", "column", "referenced", "rule"), FOREIGN_KEYS)
def test_upgrade_replaces_each_fk_with_its_rule(
    upgrade_sql: str, table: str, column: str, referenced: str, rule: str
) -> None:
    dropped = _position(upgrade_sql, _drop(table, column, referenced))
    added = _position(
        upgrade_sql, f"{_add(table, column, referenced)} ON DELETE {rule};"
    )
    assert dropped < added


@pytest.mark.parametrize(("table", "column", "referenced", "rule"), FOREIGN_KEYS)
def test_downgrade_restores_each_fk_without_a_rule(
    downgrade_sql: str, table: str, column: str, referenced: str, rule: str
) -> None:
    dropped = _position(downgrade_sql, _drop(table, column, referenced))
    added = _position(downgrade_sql, f"{_add(table, column, referenced)};")
    assert dropped < added


def test_the_revision_changes_constraints_only(
    upgrade_sql: str, downgrade_sql: str
) -> None:
    assert upgrade_sql.count("ADD CONSTRAINT") == len(FOREIGN_KEYS)
    assert "ON DELETE" not in downgrade_sql
    for sql in (upgrade_sql, downgrade_sql):
        assert "DELETE FROM" not in sql and "INSERT" not in sql
        assert "DROP TABLE" not in sql and "DROP COLUMN" not in sql
