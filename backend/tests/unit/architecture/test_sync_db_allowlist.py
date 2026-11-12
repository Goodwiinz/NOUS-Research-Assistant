"""Freeze the set of modules on the synchronous DB session (audit B3).

Owner decision: wontfix-with-guard. ``src/core/database.py`` still exports a
sync ``SessionLocal`` factory and the sync FastAPI dependency
``get_db_sync`` (``get_db`` is the async one). Migrating every caller is
accepted design debt; adding new callers is not.

A module "uses the sync session" when it names ``SessionLocal`` or
``get_db_sync`` anywhere (import, call, ``Depends(...)``, attribute access).
``AsyncSessionLocal`` and ``get_db`` are async and are not counted.

``SYNC_DB_ALLOWLIST`` may only shrink: a new module fails with a pointer to
the async session, and a module that stopped using the sync session fails
until it is removed from the list.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
SYNC_NAMES = frozenset({"SessionLocal", "get_db_sync"})
# The module that defines the sync factory and dependency.
DEFINING_MODULE = "core/database.py"

# Frozen 2026-11-11 against origin/develop. Paths are relative to backend/src.
SYNC_DB_ALLOWLIST: frozenset[str] = frozenset(
    {
        "api/evidence/router.py",
        "api/infrastructure/evaluation.py",
        "api/quality/user_behavior.py",
        "api/research/chat.py",
        "api/search/knowledge_graph.py",
        "api/search/search.py",
        "api/security/compliance.py",
        "api/security/encryption.py",
        "api/threads/thread_search.py",
        "api/threads/threads.py",
        "core/init_db.py",
        "middleware/rbac.py",
        "services/evaluation/rag_evaluation_service.py",
        "services/quality/quality_metrics_service.py",
        "services/quality/quality_recommendations_service.py",
        "services/quality/user_behavior_service.py",
        "services/search/fulltext_search_service.py",
        "services/search/search_quality_service.py",
        "services/security/rbac_service.py",
        "services/threads/thread_message_search_service.py",
        "tasks/document_processing_tasks.py",
        "tasks/evaluation_tasks.py",
        "tasks/processing_tasks.py",
        "tasks/reconcile_jobs.py",
        "tasks/reconcile_tasks.py",
        "tasks/research_run_tasks.py",
        "tasks/research_tasks.py",
        "tasks/retention_tasks.py",
        "tasks/summarize_thread_task.py",
    }
)


def _referenced_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
    return names


def _sync_session_modules(root: Path) -> set[str]:
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel == DEFINING_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _referenced_names(tree) & SYNC_NAMES:
            found.add(rel)
    return found


def test_no_new_sync_db_session_users() -> None:
    new = sorted(_sync_session_modules(SRC) - SYNC_DB_ALLOWLIST)
    assert not new, (
        "These modules newly use the synchronous DB session "
        "(SessionLocal / Depends(get_db_sync)). Use the async session instead: "
        "`db: AsyncSession = Depends(get_db)` in routes, or "
        "`async with AsyncSessionLocal() as session` elsewhere "
        "(src/core/database.py). Audit B3:\n  " + "\n  ".join(new)
    )


def test_sync_db_allowlist_has_no_stale_entries() -> None:
    stale = sorted(SYNC_DB_ALLOWLIST - _sync_session_modules(SRC))
    assert not stale, (
        "These allowlisted modules no longer use the synchronous DB session. "
        "Remove them from SYNC_DB_ALLOWLIST so the list only shrinks:\n  "
        + "\n  ".join(f"remove {m} from allowlist" for m in stale)
    )
