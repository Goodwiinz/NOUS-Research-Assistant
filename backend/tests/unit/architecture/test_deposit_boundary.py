"""GOO-318 structural guards for archive deposits.

(a) Only ``archives/zenodo.py`` talks HTTP among the deposit modules.
(b) ``deposit_service`` never updates, deletes or merges an evidence row
    (approvals, attempts); only the outbox is mutable.
(c) Protocol registration stays distinct: neither service imports the other.
(d) Only ``zenodo.from_settings`` reads ``ZENODO_SANDBOX_TOKEN``.

Mutation verification: an ``import httpx`` in ``deposit_service`` fails
``-k http``; an ``update(ArchiveDepositAttempt)`` there fails ``-k evidence``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
ADAPTER = SRC / "services/research/archives/zenodo.py"
SERVICE = SRC / "services/research/deposit_service.py"
DEPOSIT_MODULES = (
    SRC / "services/research/deposit_rules.py",
    SERVICE,
    SRC / "services/research/archives/__init__.py",
    ADAPTER,
    SRC / "api/research/deposits.py",
    SRC / "tasks/deposit_tasks.py",
)
EVIDENCE = {"ArchiveDepositApproval", "ArchiveDepositAttempt"}
HTTP_CLIENTS = {"httpx", "requests", "aiohttp", "urllib3", "http.client"}


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(), filename=str(path))


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_only_the_adapter_talks_http() -> None:
    assert all(path.exists() for path in DEPOSIT_MODULES)
    talkers = [
        path.name
        for path in DEPOSIT_MODULES
        if any(
            name.split(".")[0] in HTTP_CLIENTS or name in HTTP_CLIENTS
            for name in _imports(_tree(path))
        )
    ]
    assert talkers == ["zenodo.py"]


def test_service_never_mutates_evidence_rows() -> None:
    found = []
    for node in ast.walk(_tree(SERVICE)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in ("update", "delete") and any(
            isinstance(arg, ast.Name) and arg.id in EVIDENCE for arg in node.args
        ):
            found.append(f"{name}:{node.lineno}")
        if name in ("merge", "delete") and isinstance(func, ast.Attribute):
            found.append(f"session.{name}:{node.lineno}")
    assert found == []


def test_protocol_registration_stays_distinct() -> None:
    protocol = SRC / "services/research_engine/protocol_service.py"
    assert not any("deposit" in name for name in _imports(_tree(protocol)))
    assert not any("protocol_service" in n for n in _imports(_tree(SERVICE)))


def test_only_the_adapter_factory_reads_the_token() -> None:
    readers = []
    for path in sorted(SRC.rglob("*.py")):
        if "ZENODO_SANDBOX_TOKEN" not in path.read_text():
            continue
        if path == SRC / "core/config.py":
            continue
        for function in ast.walk(_tree(path)):
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(n, ast.Attribute) and n.attr == "ZENODO_SANDBOX_TOKEN"
                for n in ast.walk(function)
            ):
                readers.append(f"{path.relative_to(SRC)}:{function.name}")
    assert readers == ["services/research/archives/zenodo.py:from_settings"]
