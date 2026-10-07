"""Offline checks of the hb03_workspace_grants migration (Plan 07, slice 1).

``alembic upgrade FROM:TO --sql`` renders a revision's DDL without a database,
so this test reads what the hand-written revision would send to PostgreSQL: the
CHECK expressions, the ``project_id`` nullability flips, the indexes and the
order and row removal of the downgrade. The model-metadata tests of the
binding rule (``test_context.py``) never run this file, and
``scripts/ci/check_alembic.py`` only checks for a single head.

NOT RUN here: executing the revision against PostgreSQL, which is what proves
the CHECKs refuse rows and that the downgrade survives real data. Offline
mode also skips the idempotence guards ``alembic/env.py`` installs only for an
online run. ``docs/engineering/harness-bridge.md`` records the one manual
PostgreSQL run.
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
# The head this revision chains after. #1784 (ic01) and then #1788 (ir01)
# merged after this branch was cut, so hb03 follows ir01: the revision ids
# are not ordered by name.
PARENT = "ir01_revocation_indexes"
REVISION = "hb03_workspace_grants"
# Upgrade order; the downgrade reverses it (a grant cites its request).
REQUESTS, GRANTS, ACTIONS = (
    "integration_grant_requests",
    "integration_grants",
    "integration_tool_actions",
)
TABLES = (REQUESTS, GRANTS, ACTIONS)


def _alembic(*alembic_args: str) -> str:
    """What ``python -m alembic ...`` prints, run in a child process.

    Never ``from alembic.config import Config`` here: six other migration
    tests put a stub in ``sys.modules["alembic"]`` when they are collected
    before the real package is imported, so a module-level import of it fails to
    collect after them. A child process sees the real package whatever this one
    has imported (``tests/unit/ci/test_migration_tests_collect_together.py``).
    """
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
    """The SQL alembic prints for a revision range, whitespace collapsed."""
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


def test_revision_follows_the_integration_context_head() -> None:
    shown = _alembic("show", REVISION)
    assert re.search(rf"^Rev: {REVISION}\b", shown, re.MULTILINE), shown
    parent = re.search(r"^Parent: (.+)$", shown, re.MULTILINE)
    assert parent is not None and parent.group(1).strip() == PARENT, shown
    assert len(REVISION) <= 32


def test_the_revision_chain_has_a_single_head() -> None:
    # check_alembic.py checks the same in CI; here a fork shows up as soon as
    # the unit tests run, before this revision is rebased onto another's head.
    heads = [line for line in _alembic("heads").splitlines() if line.strip()]
    assert len(heads) == 1, heads


@pytest.mark.parametrize("table", TABLES)
def test_upgrade_adds_a_nullable_workspace_fk_and_frees_project_id(
    upgrade_sql: str, table: str
) -> None:
    add = _position(upgrade_sql, f"ALTER TABLE {table} ADD COLUMN workspace_id UUID;")
    # Nullable (no NOT NULL on the new column) and a real foreign key.
    foreign_key = _position(
        upgrade_sql,
        f"ALTER TABLE {table} ADD FOREIGN KEY(workspace_id) "
        "REFERENCES workspaces (id);",
    )
    flip = _position(
        upgrade_sql, f"ALTER TABLE {table} ALTER COLUMN project_id DROP NOT NULL;"
    )
    index = _position(
        upgrade_sql,
        f"CREATE INDEX ix_{table}_workspace_id ON {table} (workspace_id);",
    )
    assert add < foreign_key and add < flip and add < index


@pytest.mark.parametrize("table", [REQUESTS, GRANTS])
def test_upgrade_binds_a_request_or_grant_to_exactly_one_target(
    upgrade_sql: str, table: str
) -> None:
    constraint = (
        f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_one_binding "
        "CHECK ((project_id IS NULL) <> (workspace_id IS NULL));"
    )
    # Added after both columns are settled, or the CHECK could not be created.
    assert _position(upgrade_sql, constraint) > _position(
        upgrade_sql, f"ALTER TABLE {table} ALTER COLUMN project_id DROP NOT NULL;"
    )


def test_upgrade_requires_an_action_to_name_at_least_one_target(
    upgrade_sql: str,
) -> None:
    # project_id is the target Collection and workspace_id the grant's
    # binding, so a workspace-grant action aimed at a Collection sets both:
    # OR, never the XOR of requests and grants.
    constraint = (
        f"ALTER TABLE {ACTIONS} ADD CONSTRAINT ck_{ACTIONS}_some_binding "
        "CHECK (project_id IS NOT NULL OR workspace_id IS NOT NULL);"
    )
    assert _position(upgrade_sql, constraint) > _position(
        upgrade_sql, f"ALTER TABLE {ACTIONS} ALTER COLUMN project_id DROP NOT NULL;"
    )
    assert f"ck_{ACTIONS}_one_binding" not in upgrade_sql


def test_upgrade_moves_the_alembic_version_and_changes_no_data(
    upgrade_sql: str,
) -> None:
    assert (
        f"UPDATE alembic_version SET version_num='{REVISION}' "
        f"WHERE alembic_version.version_num = '{PARENT}';"
    ) in upgrade_sql
    # Per table: add column, foreign key, drop NOT NULL, CHECK; plus the index.
    assert upgrade_sql.count("ALTER TABLE") == 3 * 4
    assert upgrade_sql.count("CREATE INDEX") == 3
    assert "INSERT" not in upgrade_sql and "DELETE" not in upgrade_sql


@pytest.mark.parametrize("table", TABLES)
def test_downgrade_deletes_projectless_rows_before_restoring_not_null(
    downgrade_sql: str, table: str
) -> None:
    name = "some_binding" if table == ACTIONS else "one_binding"
    steps = [
        f"DROP INDEX ix_{table}_workspace_id;",
        f"ALTER TABLE {table} DROP CONSTRAINT ck_{table}_{name};",
        f"DELETE FROM {table} WHERE project_id IS NULL;",
        f"ALTER TABLE {table} ALTER COLUMN project_id SET NOT NULL;",
        f"ALTER TABLE {table} DROP COLUMN workspace_id;",
    ]
    positions = [_position(downgrade_sql, step) for step in steps]
    assert positions == sorted(positions)


def test_downgrade_removes_children_before_the_requests_they_cite(
    downgrade_sql: str,
) -> None:
    dropped = [
        _position(downgrade_sql, f"ALTER TABLE {table} DROP COLUMN workspace_id;")
        for table in (ACTIONS, GRANTS, REQUESTS)
    ]
    assert dropped == sorted(dropped)
    assert (
        f"UPDATE alembic_version SET version_num='{PARENT}' "
        f"WHERE alembic_version.version_num = '{REVISION}';"
    ) in downgrade_sql
