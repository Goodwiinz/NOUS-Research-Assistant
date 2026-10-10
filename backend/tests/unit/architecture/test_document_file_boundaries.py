"""Handlers in ``api/documents/files.py`` whose write lives in a service.

Plan 07 Slice 3 (review amendment 5) moved the ``PUT /files/{file_id}``
metadata write into ``services/documents/file_metadata_service.py``; the
handler used to commit, refresh and roll back inline. ``reprocess_file`` still
owns its transaction and was out of scope for that change, so this guard pins
only the handlers in ``SERVICE_OWNED_HANDLERS``: none of them may call
``commit``/``rollback``/``flush``/``refresh``/``begin``/``begin_nested``. The
set only grows, in the change that moves another handler's write into a
service.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

FILES_ROUTER = (
    Path(__file__).resolve().parents[3] / "src" / "api" / "documents" / "files.py"
)
TXN_METHODS = frozenset(
    {"commit", "rollback", "flush", "refresh", "begin", "begin_nested"}
)
SERVICE_OWNED_HANDLERS = frozenset({"update_file_metadata"})


def _handlers() -> dict[str, ast.AsyncFunctionDef]:
    tree = ast.parse(FILES_ROUTER.read_text(encoding="utf-8"), str(FILES_ROUTER))
    return {
        node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
    }


def _txn_calls(handler: ast.AsyncFunctionDef) -> list[str]:
    return [
        f"line {node.lineno}: .{node.func.attr}()"
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in TXN_METHODS
    ]


@pytest.mark.parametrize("name", sorted(SERVICE_OWNED_HANDLERS))
def test_service_owned_handler_makes_no_transaction_call(name: str) -> None:
    handler = _handlers().get(name)
    assert handler is not None, (
        f"{FILES_ROUTER} no longer defines {name}(); update SERVICE_OWNED_HANDLERS "
        "to the handler's new name rather than dropping the guard."
    )
    calls = _txn_calls(handler)
    assert not calls, (
        f"{name}() in {FILES_ROUTER} owns a transaction call ({calls}); its "
        "service owns the commit/rollback, the router is transport only."
    )


def test_guard_sees_an_inline_commit() -> None:
    """The scan must catch the pattern the extraction removed."""
    handler = ast.parse(
        "async def h(db):\n    await db.commit()\n    await db.refresh(x)\n"
    ).body[0]
    assert isinstance(handler, ast.AsyncFunctionDef)
    assert _txn_calls(handler) == ["line 2: .commit()", "line 3: .refresh()"]
