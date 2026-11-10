"""A4-RLS1: agent_memories and search_analytics get the same RLS boundary.

``h8i9j0k1l2m3`` enumerated eight backend-only tables and missed the two
Alembic-created tables that postdate the blanket Supabase migrations. This
revision closes that gap and must tolerate either table being absent (live
dev never created them).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_VERSIONS = _BACKEND_ROOT / "alembic" / "versions"
_MODULE_PATH = _VERSIONS / "rl01_secure_late_internal_tables_rls.py"


def _load(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    if "alembic" not in sys.modules:
        fake_alembic = types.ModuleType("alembic")
        fake_alembic.op = SimpleNamespace(  # type: ignore[attr-defined]
            execute=lambda sql: None
        )
        sys.modules["alembic"] = fake_alembic
    spec.loader.exec_module(module)
    return module


rls_migration = _load(_MODULE_PATH, "_late_internal_tables_rls_migration")
earlier_rls = _load(
    _VERSIONS / "h8i9j0k1l2m3_secure_internal_agent_tables_rls.py",
    "_internal_agent_tables_rls_migration_for_late",
)


@pytest.fixture
def statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []

    def execute(sql: str) -> None:
        captured.append(str(sql))

    monkeypatch.setattr(rls_migration, "op", SimpleNamespace(execute=execute))
    return captured


def _normalize(sql: str) -> str:
    return " ".join(sql.lower().replace('"', "").split())


def test_covers_the_tables_the_first_rls_revision_missed() -> None:
    assert set(rls_migration._TABLES) == {"agent_memories", "search_analytics"}
    assert not set(rls_migration._TABLES) & set(earlier_rls._TABLES)
    assert len(rls_migration.revision) <= 32


def test_tables_are_created_by_this_alembic_chain() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in _VERSIONS.glob("*.py")
    )
    for table in rls_migration._TABLES:
        assert f'"{table}"' in sources


def test_each_table_is_guarded_secured_and_revoked(statements: list[str]) -> None:
    rls_migration.upgrade()

    assert len(statements) == len(rls_migration._TABLES)
    for table, statement in zip(rls_migration._TABLES, statements):
        sql = _normalize(statement)
        guard = sql.index(f"to_regclass('public.{table}') is null")
        enable = sql.index(f"alter table public.{table} enable row level security")
        assert guard < enable, "the missing-table guard must run first"
        assert "return;" in sql[guard:enable]
        for role in ("anon", "authenticated"):
            assert f"rolname = '{role}'" in sql
            assert f"revoke all on table public.{table} from {role}" in sql
        assert "create policy" not in sql


def test_downgrade_preserves_the_security_boundary(statements: list[str]) -> None:
    rls_migration.downgrade()
    assert statements == []
