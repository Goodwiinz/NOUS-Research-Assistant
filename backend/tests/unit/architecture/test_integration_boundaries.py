"""Architectural guard for the integration, artifact and harness routers.

``test_workspace_boundaries.py`` and ``test_agent_api_boundaries.py`` hold the
thread and agent routers to the router -> service -> transaction contract, but
nothing scanned ``api/integrations/*``, ``api/artifacts.py`` or
``api/harness.py`` (harness remediation review, rec #4). Two rules:

  (a) no ``commit``/``rollback``/``flush``/``refresh``/``begin``/
      ``begin_nested`` call in those router modules: the services own the
      transaction boundary, and ``async with db.begin():`` or a savepoint
      would make the router own one. Only the router files are scanned, so a
      service that commits (for example ``services/integrations/context.py``)
      is fine;
  (b) the access getters keep requiring the caller's identity: every
      identity parameter stays present with no default, so a caller can never
      fall through to an unscoped lookup.

Skipped: "every route has a Depends(...)". ``get_db`` alone would satisfy it,
so it proves nothing about identity, and the harness websocket authenticates
in its body, so it would need an allowlist.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from src.services.artifacts.service import authorize_artifact
from src.services.integrations.context import (
    authorized_project,
    authorized_scope,
    authorized_scope_filter,
    authorized_workspace,
    resolve_integration_context,
)

pytestmark = pytest.mark.unit

BACKEND_DIR = Path(__file__).resolve().parents[3]
API_DIR = BACKEND_DIR / "src" / "api"
TXN_METHODS = frozenset(
    {"commit", "rollback", "flush", "refresh", "begin", "begin_nested"}
)

ROUTER_FILES = sorted(
    [
        *(API_DIR / "integrations").glob("*.py"),
        API_DIR / "artifacts.py",
        API_DIR / "harness.py",
    ]
)

KEYWORD_ONLY = inspect.Parameter.KEYWORD_ONLY
POSITIONAL = inspect.Parameter.POSITIONAL_OR_KEYWORD

# (getter, {identity parameter: required kind}). Pin what the code really
# has: authorized_project and authorized_workspace take identity positionally,
# authorized_scope and authorized_scope_filter take the resolved context that
# carries it, and resolve_integration_context derives identity from the grant
# token.
ACCESS_GETTERS: list[tuple[Callable[..., Any], dict[str, Any]]] = [
    (authorize_artifact, {"user_id": KEYWORD_ONLY, "organization_id": KEYWORD_ONLY}),
    (authorized_project, {"user_id": POSITIONAL, "organization_id": POSITIONAL}),
    (authorized_workspace, {"user_id": POSITIONAL, "organization_id": POSITIONAL}),
    (authorized_scope, {"context": POSITIONAL}),
    (authorized_scope_filter, {"context": POSITIONAL}),
    (
        resolve_integration_context,
        {"token": POSITIONAL, "required_scope": KEYWORD_ONLY},
    ),
]


def _txn_calls(source: str) -> list[str]:
    return [
        f"{node.func.attr}() at line {node.lineno}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in TXN_METHODS
    ]


def _identity_violations(fn: Callable[..., Any], required: dict[str, Any]) -> list[str]:
    params = inspect.signature(fn).parameters
    problems = []
    for name, kind in required.items():
        param = params.get(name)
        if param is None:
            problems.append(f"{name} missing")
        elif param.kind is not kind:
            problems.append(f"{name} is {param.kind.name}, expected {kind.name}")
        elif param.default is not inspect.Parameter.empty:
            problems.append(f"{name} has a default")
    return problems


def test_router_glob_covers_the_known_modules() -> None:
    names = {p.relative_to(API_DIR).as_posix() for p in ROUTER_FILES}
    assert {
        "integrations/actions.py",
        "integrations/devices.py",
        "integrations/grants.py",
        "integrations/tools.py",
        "artifacts.py",
        "harness.py",
    } <= names
    assert all(p.is_file() for p in ROUTER_FILES)


@pytest.mark.parametrize(
    "path", ROUTER_FILES, ids=lambda p: p.relative_to(API_DIR).as_posix()
)
def test_router_owns_no_transaction(path: Path) -> None:
    assert _txn_calls(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    ("method", "statement"),
    [
        ("commit", "await db.commit()"),
        ("begin", "async with db.begin(): pass"),
        ("begin_nested", "async with db.begin_nested(): pass"),
    ],
    ids=["commit", "async-with-begin", "async-with-begin-nested"],
)
def test_txn_scan_flags_injected_source(method: str, statement: str) -> None:
    # Mutation proof for test_router_owns_no_transaction: with no violation in
    # the real routers it can only pass, so show the scan fires on injected
    # source. begin()/begin_nested() sit in an AsyncWith item, not a bare
    # statement, so they get their own cases.
    source = f"async def route(db):\n    {statement}\n"
    assert _txn_calls(source) == [f"{method}() at line 2"]


@pytest.mark.parametrize(
    ("fn", "required"), ACCESS_GETTERS, ids=lambda v: getattr(v, "__name__", None)
)
def test_access_getter_requires_caller_identity(
    fn: Callable[..., Any], required: dict[str, Any]
) -> None:
    assert _identity_violations(inspect.unwrap(fn), required) == []
