"""Contract tests for the append-only Daily Research Brief review migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_PATH = (
    BACKEND_ROOT / "alembic" / "versions" / "20260927_daily_research_brief_reviews.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_daily_research_brief_reviews_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingOperations:
    def __init__(self) -> None:
        self.tables: list[tuple[str, tuple[Any, ...]]] = []
        self.indexes: list[tuple[str, str, tuple[str, ...], bool]] = []
        self.dropped_indexes: list[tuple[str, str | None]] = []
        self.dropped_tables: list[str] = []

    def create_table(self, name: str, *items: Any, **_kwargs: Any) -> None:
        self.tables.append((name, items))

    def create_index(
        self,
        name: str,
        table_name: str,
        columns: list[str],
        *,
        unique: bool = False,
        **_kwargs: Any,
    ) -> None:
        self.indexes.append((name, table_name, tuple(columns), unique))

    def drop_index(self, name: str, *, table_name: str | None = None) -> None:
        self.dropped_indexes.append((name, table_name))

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)


def _upgrade() -> tuple[ModuleType, RecordingOperations]:
    migration = _load_migration()
    operations = RecordingOperations()
    setattr(migration, "op", operations)
    migration.upgrade()
    return migration, operations


def test_revision_chains_from_agent_operations_and_is_the_only_head() -> None:
    migration = _load_migration()
    assert migration.revision == "daily_brief_reviews_20260927"
    assert migration.down_revision == "agent_ops_20260925"

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    assert heads == ["aw01_artifact_workspace"]
    revisions = {
        revision.revision
        for revision in scripts.walk_revisions(base="base", head=heads[0])
    }
    assert {"merge_research_heads_20260928", "merge_harness_heads"} <= revisions


def test_upgrade_creates_review_ledger_columns_foreign_keys_and_unique_gate() -> None:
    _migration, operations = _upgrade()
    assert len(operations.tables) == 1
    table_name, items = operations.tables[0]
    assert table_name == "research_stage_reviews"

    columns = {item.name: item for item in items if isinstance(item, sa.Column)}
    assert set(columns) == {
        "id",
        "owner_id",
        "organization_id",
        "run_id",
        "step_index",
        "stage_type",
        "review_kind",
        "reviewer_id",
        "output_hash",
        "decision",
        "decision_payload",
        "note",
        "created_at",
    }
    assert columns["organization_id"].nullable is True
    for required in set(columns) - {"organization_id", "note"}:
        assert columns[required].nullable is False, required

    foreign_keys = {
        (
            tuple(item.column_keys),
            tuple(element.target_fullname for element in item.elements),
        )
        for item in items
        if isinstance(item, sa.ForeignKeyConstraint)
    }
    assert foreign_keys == {
        (("owner_id",), ("users.id",)),
        (("organization_id",), ("organizations.id",)),
        (("run_id",), ("research_runs.id",)),
        (("reviewer_id",), ("users.id",)),
    }

    unique_constraints = {
        tuple(
            column if isinstance(column, str) else cast(str, cast(Any, column).name)
            for column in item._pending_colargs
        )
        for item in items
        if isinstance(item, sa.UniqueConstraint)
    }
    assert unique_constraints == {
        ("run_id", "step_index", "output_hash", "review_kind")
    }


def test_upgrade_creates_owner_and_review_lookup_indexes() -> None:
    _migration, operations = _upgrade()
    assert {
        (table, columns, unique) for _, table, columns, unique in operations.indexes
    } == {
        ("research_stage_reviews", ("run_id", "step_index"), False),
        ("research_stage_reviews", ("owner_id",), False),
        ("research_stage_reviews", ("reviewer_id",), False),
        ("research_stage_reviews", ("organization_id",), False),
    }


def test_downgrade_reverses_indexes_and_table() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    setattr(migration, "op", operations)

    migration.downgrade()

    assert {name for name, _ in operations.dropped_indexes} == {
        "ix_research_stage_reviews_run_step",
        "ix_research_stage_reviews_owner_id",
        "ix_research_stage_reviews_reviewer_id",
        "ix_research_stage_reviews_organization_id",
    }
    assert operations.dropped_tables == ["research_stage_reviews"]
