# backend/tests/unit/test_account_bindings_migration.py
"""Offline checks of the ac01_account_bindings migration (account connections).

``alembic upgrade FROM:TO --sql`` renders the revision's DDL without a
database. This proves what the revision sends to PostgreSQL. It does not prove
that PostgreSQL accepts it: that is
``tests/integration/test_account_bindings_migration_postgres.py``. Offline mode
also skips the downgrade's guard against account-bound harness sessions (it
needs a live connection).
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
PARENT = "aw03_artifact_completion"
REVISION = "ac01_account_bindings"
REQUESTS, GRANTS, ACTIONS = (
    "integration_grant_requests",
    "integration_grants",
    "integration_tool_actions",
)
BINDINGS = "bridge_workspace_bindings"
KIND_CHECK = (
    "(binding_kind = 'project' AND project_id IS NOT NULL AND workspace_id IS NULL)"
    " OR (binding_kind = 'workspace' AND project_id IS NULL AND workspace_id IS NOT NULL)"
    " OR (binding_kind = 'account' AND project_id IS NULL AND workspace_id IS NULL)"
)
ACTION_KIND_CHECK = (
    "(binding_kind = 'project' AND project_id IS NOT NULL AND workspace_id IS NULL"
    " AND target_workspace_id IS NULL)"
    " OR (binding_kind = 'workspace' AND workspace_id IS NOT NULL"
    " AND target_workspace_id IS NULL)"
    " OR (binding_kind = 'account' AND workspace_id IS NULL"
    " AND (project_id IS NULL) <> (target_workspace_id IS NULL))"
)


def _alembic(*alembic_args: str) -> str:
    """What ``python -m alembic ...`` prints, run in a child process (never
    import alembic here: see test_migration_tests_collect_together.py)."""
    env = {k: v for k, v in os.environ.items() if k != "SUPABASE_DB_URL"}
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


def test_revision_follows_the_head_and_fits_the_version_column() -> None:
    shown = _alembic("show", REVISION)
    parent = re.search(r"^Parent: (.+)$", shown, re.MULTILINE)
    assert parent is not None and parent.group(1).strip() == PARENT, shown
    assert len(REVISION) <= 32


def test_the_revision_chain_has_a_single_head() -> None:
    heads = [line for line in _alembic("heads").splitlines() if line.strip()]
    assert len(heads) == 1, heads


@pytest.mark.parametrize("table", [REQUESTS, GRANTS, ACTIONS])
def test_upgrade_backfills_the_kind_then_drops_the_default(
    upgrade_sql: str, table: str
) -> None:
    steps = [
        f"ALTER TABLE {table} ADD COLUMN binding_kind VARCHAR(16) "
        "DEFAULT 'project' NOT NULL;",
        f"UPDATE {table} SET binding_kind = 'workspace' WHERE workspace_id IS NOT NULL;",
        f"ALTER TABLE {table} ALTER COLUMN binding_kind DROP DEFAULT;",
    ]
    positions = [_position(upgrade_sql, step) for step in steps]
    assert positions == sorted(positions)


@pytest.mark.parametrize("table", [REQUESTS, GRANTS])
def test_upgrade_replaces_one_binding_with_one_check_per_kind(
    upgrade_sql: str, table: str
) -> None:
    dropped = _position(
        upgrade_sql, f"ALTER TABLE {table} DROP CONSTRAINT ck_{table}_one_binding;"
    )
    added = _position(
        upgrade_sql,
        f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_binding_kind "
        f"CHECK ({KIND_CHECK});",
    )
    backfilled = _position(
        upgrade_sql, f"ALTER TABLE {table} ALTER COLUMN binding_kind DROP DEFAULT;"
    )
    assert backfilled < dropped < added


def test_upgrade_gives_actions_a_target_workspace_and_a_kind_aware_check(
    upgrade_sql: str,
) -> None:
    column = _position(
        upgrade_sql, f"ALTER TABLE {ACTIONS} ADD COLUMN target_workspace_id UUID;"
    )
    foreign_key = _position(
        upgrade_sql,
        f"ALTER TABLE {ACTIONS} ADD FOREIGN KEY(target_workspace_id) "
        "REFERENCES workspaces (id);",
    )
    index = _position(
        upgrade_sql,
        f"CREATE INDEX ix_{ACTIONS}_target_workspace_id ON {ACTIONS} "
        "(target_workspace_id);",
    )
    dropped = _position(
        upgrade_sql, f"ALTER TABLE {ACTIONS} DROP CONSTRAINT ck_{ACTIONS}_some_binding;"
    )
    added = _position(
        upgrade_sql,
        f"ALTER TABLE {ACTIONS} ADD CONSTRAINT ck_{ACTIONS}_binding_kind "
        f"CHECK ({ACTION_KIND_CHECK});",
    )
    assert column < foreign_key < index < added and dropped < added


def test_upgrade_frees_the_folder_project_and_deletes_nothing(
    upgrade_sql: str,
) -> None:
    assert (
        f"ALTER TABLE {BINDINGS} ALTER COLUMN project_id DROP NOT NULL;" in upgrade_sql
    )
    assert "DELETE" not in upgrade_sql
    assert (
        f"UPDATE alembic_version SET version_num='{REVISION}' "
        f"WHERE alembic_version.version_num = '{PARENT}';"
    ) in upgrade_sql


def test_downgrade_deletes_account_rows_children_first(downgrade_sql: str) -> None:
    steps = [
        f"DELETE FROM {BINDINGS} WHERE project_id IS NULL;",
        f"ALTER TABLE {BINDINGS} ALTER COLUMN project_id SET NOT NULL;",
        f"ALTER TABLE {ACTIONS} DROP CONSTRAINT ck_{ACTIONS}_binding_kind;",
        f"DELETE FROM {ACTIONS} WHERE binding_kind = 'account';",
        f"ALTER TABLE {ACTIONS} DROP COLUMN target_workspace_id;",
        f"ALTER TABLE {ACTIONS} DROP COLUMN binding_kind;",
        f"ALTER TABLE {ACTIONS} ADD CONSTRAINT ck_{ACTIONS}_some_binding "
        "CHECK (project_id IS NOT NULL OR workspace_id IS NOT NULL);",
        f"ALTER TABLE {GRANTS} DROP CONSTRAINT ck_{GRANTS}_binding_kind;",
        f"DELETE FROM {GRANTS} WHERE binding_kind = 'account' OR request_id IN "
        f"(SELECT id FROM {REQUESTS} WHERE binding_kind = 'account');",
        f"ALTER TABLE {GRANTS} DROP COLUMN binding_kind;",
        f"ALTER TABLE {GRANTS} ADD CONSTRAINT ck_{GRANTS}_one_binding "
        "CHECK ((project_id IS NULL) <> (workspace_id IS NULL));",
        f"ALTER TABLE {REQUESTS} DROP CONSTRAINT ck_{REQUESTS}_binding_kind;",
        f"DELETE FROM {REQUESTS} WHERE binding_kind = 'account';",
        f"ALTER TABLE {REQUESTS} DROP COLUMN binding_kind;",
        f"ALTER TABLE {REQUESTS} ADD CONSTRAINT ck_{REQUESTS}_one_binding "
        "CHECK ((project_id IS NULL) <> (workspace_id IS NULL));",
    ]
    positions = [_position(downgrade_sql, step) for step in steps]
    assert positions == sorted(positions)
