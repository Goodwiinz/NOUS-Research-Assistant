"""GOO-316 structural guards: authorship never grants permission, the ORCID
token exchange is the only outbound call, and only ``record_orcid`` builds
an ORCID receipt.

Mutation verification: importing ``statements_service`` into
``project_access`` fails ``-k permission``; a second ``client.get`` in
``statements_service`` fails ``-k outbound``; an ``OrcidAuthentication(``
built in ``orcid_callback`` fails ``-k receipt``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
PERMISSION_MODULES = (
    SRC / "services/research_engine/project_access.py",
    # The role-assignment routes.
    SRC / "api/research_engine/projects.py",
)
OUTBOUND_MODULES = (
    SRC / "services/research/statements_service.py",
    SRC / "api/auth_orcid.py",
)
HTTP_VERBS = {"get", "post", "put", "patch", "delete", "request", "stream", "send"}
BANNED_CLIENTS = {"requests", "aiohttp", "urllib3", "http.client", "urllib.request"}


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_contributions_grant_no_permission() -> None:
    for path in PERMISSION_MODULES:
        imported = _imports(ast.parse(path.read_text()))
        assert not any(
            "manuscript_statements" in name or "statements_service" in name
            for name in imported
        ), f"{path.name} reads authorship"


def _client_names(tree: ast.AST) -> set[str]:
    """Names bound by ``with httpx.AsyncClient(...) as name``."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                call = item.context_expr
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "httpx"
                    and isinstance(item.optional_vars, ast.Name)
                ):
                    names.add(item.optional_vars.id)
    return names


def test_one_outbound_call_site_the_orcid_token_post() -> None:
    sites: list[str] = []
    for path in OUTBOUND_MODULES:
        tree = ast.parse(path.read_text())
        assert not BANNED_CLIENTS & _imports(tree), path.name
        clients = _client_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            receiver = node.func.value
            if isinstance(receiver, ast.Name) and receiver.id == "httpx":
                # Only the client constructor; never httpx.get/post shortcuts.
                assert node.func.attr == "AsyncClient", f"{path.name}: httpx call"
            elif (
                isinstance(receiver, ast.Name)
                and receiver.id in clients
                and node.func.attr in HTTP_VERBS
            ):
                sites.append(f"{path.name}:{node.func.attr}")
    assert sites == ["statements_service.py:post"]
    source = (SRC / "services/research/statements_service.py").read_text()
    assert '/oauth/token"' in source


def test_only_record_orcid_builds_a_receipt() -> None:
    builders: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "OrcidAuthentication"
                ):
                    builders.append(f"{path.relative_to(SRC)}:{function.name}")
    assert builders == ["services/research/statements_service.py:record_orcid"]
