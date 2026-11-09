"""The ``alembic check`` exclusions in ``alembic/env.py`` stay narrow and true.

The CI drift check (``alembic check`` against a from-empty database) is
blocking. Everything the migrations build must be declared on the models,
except the objects ``env.py`` lists as migration-owned and the four
``server_default="now()"`` columns it compares by declaration. An entry that
names a mapped object, or a revision that does not exist, would hide real
drift, so these tests pin each entry to the facts it claims.

Source-level: ``env.py`` runs migrations when imported, and alembic must not be
imported in this process (``tests/unit/ci/test_migration_tests_collect_together``),
so the maps are read with ``ast`` and the revision ids with a regex. The model
metadata is read in a fresh interpreter that imports only ``src.models``, as
``env.py`` does: in the test process other imports register more models (the
DO KB backfill model, for one) than ``alembic check`` ever sees.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ENV_PY = BACKEND_ROOT / "alembic" / "env.py"
VERSIONS = BACKEND_ROOT / "alembic" / "versions"
REVISION_RE = re.compile(r"""^revision\s*=\s*["']([^"']+)["']""", re.MULTILINE)


def _env_literal(name: str) -> Any:
    """The literal entries of a module-level assignment in env.py.

    ``**`` splats in a dict (the document_embeddings entry, built from
    ``MIGRATION_OWNED_TABLES``) are skipped; that table has its own test in
    ``test_document_embeddings_migration.py``.
    """
    tree = ast.parse(ENV_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ):
            value = node.value
            if isinstance(value, ast.Dict):
                return {
                    ast.literal_eval(key): ast.literal_eval(val)
                    for key, val in zip(value.keys, value.values)
                    if key is not None
                }
            if isinstance(value, ast.Call) and value.args:  # frozenset({...})
                return frozenset(ast.literal_eval(value.args[0]))
            return ast.literal_eval(value)
    raise AssertionError(f"{name} is not assigned in alembic/env.py")


def _revision_ids() -> set[str]:
    ids: set[str] = set()
    for path in VERSIONS.glob("*.py"):
        ids.update(REVISION_RE.findall(path.read_text(encoding="utf-8")))
    return ids


_DUMP_METADATA = """
import json
import src.models  # registers the models exactly as alembic/env.py does
from src.models.base import Base

print(json.dumps({
    name: {
        "columns": {
            column.name: getattr(column.server_default, "arg", None)
            if isinstance(getattr(column.server_default, "arg", None), str)
            else None
            for column in table.columns
        },
        "indexes": sorted(index.name for index in table.indexes),
    }
    for name, table in Base.metadata.tables.items()
}))
"""


@lru_cache(maxsize=1)
def _metadata() -> dict[str, Any]:
    """{table: {"columns": {name: str server_default or None}, "indexes": [...]}}."""
    done = subprocess.run(
        [sys.executable, "-c", _DUMP_METADATA],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return dict(json.loads(done.stdout.strip().splitlines()[-1]))


def test_every_owner_is_a_real_revision() -> None:
    revisions = _revision_ids()
    for name in (
        "MIGRATION_OWNED_TABLE_REVISIONS",
        "MIGRATION_OWNED_COLUMN_REVISIONS",
        "MIGRATION_OWNED_INDEX_REVISIONS",
    ):
        owners = _env_literal(name)
        assert owners, f"{name} lost its entries"
        for obj, revision in owners.items():
            assert revision in revisions, f"{name}[{obj!r}] names {revision!r}"


def test_migration_owned_tables_are_not_mapped() -> None:
    metadata = _metadata()
    for table in _env_literal("MIGRATION_OWNED_TABLE_REVISIONS"):
        assert table not in metadata, (
            f"{table} is on Base.metadata; excluding it would hide its drift. "
            "Remove it from MIGRATION_OWNED_TABLE_REVISIONS."
        )


def test_migration_owned_columns_are_not_mapped() -> None:
    metadata = _metadata()
    for table, column in _env_literal("MIGRATION_OWNED_COLUMN_REVISIONS"):
        assert table in metadata, f"{table} is no longer a model table"
        assert column not in metadata[table]["columns"], (
            f"{table}.{column} is mapped now; remove it from "
            "MIGRATION_OWNED_COLUMN_REVISIONS so the drift check compares it"
        )


def test_migration_owned_indexes_are_not_declared() -> None:
    declared = {index for table in _metadata().values() for index in table["indexes"]}
    for index in _env_literal("MIGRATION_OWNED_INDEX_REVISIONS"):
        assert index not in declared, (
            f"{index} is declared on a model; remove it from "
            "MIGRATION_OWNED_INDEX_REVISIONS so the drift check compares it"
        )


def test_frozen_now_defaults_still_declare_the_string() -> None:
    """Once a column moves to ``func.now()`` its exemption must go too."""
    metadata = _metadata()
    columns = _env_literal("FROZEN_NOW_DEFAULT_COLUMNS")
    assert len(columns) == 4
    for table, column in columns:
        assert metadata[table]["columns"][column] == "now()", (
            f"{table}.{column} no longer declares server_default='now()'; "
            "remove it from FROZEN_NOW_DEFAULT_COLUMNS"
        )
