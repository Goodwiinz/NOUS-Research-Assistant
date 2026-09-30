"""Task 6.2 architecture/maintenance-contract guards.

The plan's Task 6.2 names three backend guards: "workspace routers cannot
own transactions or SQL queries", "scope helpers require organization/user
arguments", and "compatibility exports resolve". The first and third are
already enforced by ``test_workspace_boundaries.py`` (Task 4.4) —
``TestNoRouterOwnedTransactions`` and ``TestCompositionExportsStillResolve``
— so this module does not re-implement them; duplicating an ``ast`` walk
that already exists would just be two places to update when the split
changes. ``TestOtherTask62GuardsLiveInWorkspaceBoundaries`` below only
cross-checks that those classes still exist, so a future deletion or rename
of that file surfaces as a failure here too, instead of a silent gap in
this doc's claim.

The guard this module actually implements is new: every scope/access
getter in ``src/services/threads/workspace_access.py`` (the canonical
funnel Task 4.3 consolidated onto — see its module docstring) must require
the caller's identity with no default, so it fails closed rather than
silently skipping the access check when a caller forgets to pass one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND_DIR = Path(__file__).resolve().parents[3]
WORKSPACE_ACCESS = BACKEND_DIR / "src" / "services" / "threads" / "workspace_access.py"
WORKSPACE_BOUNDARIES_TEST = (
    Path(__file__).resolve().parent / "test_workspace_boundaries.py"
)

# Public getters in workspace_access.py that resolve a resource against a
# caller's identity, and the identity argument(s) each must require with no
# default. get_accessible_document_or_none is the one org-scoped (tenant)
# helper in this module; every other getter is scoped by membership, so it
# requires only user_id (see workspace_access.py's module docstring for why
# workspace access is membership-based, not organization-based).
REQUIRED_IDENTITY_ARGS: dict[str, tuple[str, ...]] = {
    "get_workspace": ("user_id",),
    "get_conversation": ("user_id",),
    "get_thread": ("user_id",),
    "get_message": ("user_id",),
    "get_collection": ("user_id",),
    "get_accessible_document_or_none": ("user_id", "organization_id"),
}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function_defs(tree: ast.Module) -> dict[str, ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }


def _required_arg_names(fn: ast.AsyncFunctionDef) -> set[str]:
    """Names of parameters with no default -- positional or keyword-only."""
    args = fn.args
    positional = list(args.posonlyargs) + list(args.args)
    n_defaults = len(args.defaults)
    required_positional = (
        positional[: len(positional) - n_defaults] if n_defaults else positional
    )
    required_kwonly = [
        kw.arg
        for kw, default in zip(args.kwonlyargs, args.kw_defaults)
        if default is None
    ]
    return {a.arg for a in required_positional} | set(required_kwonly)


class TestScopeHelpersRequireIdentity:
    """Every workspace_access getter fails closed on a missing identity arg."""

    def test_covers_every_known_getter(self) -> None:
        tree = _parse(WORKSPACE_ACCESS)
        defined = set(_function_defs(tree))
        missing = set(REQUIRED_IDENTITY_ARGS) - defined
        assert not missing, (
            f"{WORKSPACE_ACCESS} no longer defines {sorted(missing)} -- "
            "update REQUIRED_IDENTITY_ARGS in this test to match the "
            "current scope/access funnel."
        )

    @pytest.mark.parametrize(
        "fn_name,identity_args",
        sorted(REQUIRED_IDENTITY_ARGS.items()),
        ids=lambda v: v if isinstance(v, str) else "-".join(v),
    )
    def test_getter_requires_identity_args_with_no_default(
        self, fn_name: str, identity_args: tuple[str, ...]
    ) -> None:
        tree = _parse(WORKSPACE_ACCESS)
        fn = _function_defs(tree)[fn_name]
        required = _required_arg_names(fn)
        missing = set(identity_args) - required
        assert not missing, (
            f"{fn_name}() in {WORKSPACE_ACCESS} must require {sorted(identity_args)} "
            f"with no default so it fails closed -- found required args "
            f"{sorted(required)}. Making an identity argument optional lets a "
            "caller skip the access check by omission."
        )


class TestOtherTask62GuardsLiveInWorkspaceBoundaries:
    """Cross-check, not a reimplementation, of the other two Task 6.2 guards."""

    def test_workspace_boundaries_file_still_defines_its_guard_classes(self) -> None:
        tree = _parse(WORKSPACE_BOUNDARIES_TEST)
        class_names = {
            node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        }
        required = {
            "TestNoRouterOwnedTransactions",
            "TestCompositionExportsStillResolve",
        }
        missing = required - class_names
        assert not missing, (
            f"{WORKSPACE_BOUNDARIES_TEST} no longer defines {sorted(missing)} -- "
            "Task 6.2's 'workspace routers own no transactions/SQL' and "
            "'compatibility exports resolve' guards live there. If they moved "
            "or were renamed, update this cross-check rather than letting "
            "coverage silently disappear."
        )


def test_no_agent_tool_writes_screening_decisions() -> None:
    """GOO-302: screening observations, adjudications and reopens are human
    ledger events; no agent tool may import the screening service."""
    agent = BACKEND_DIR / "src" / "services" / "agent"
    assert agent.is_dir() and any(agent.rglob("*.py"))
    assert not any("screening_service" in p.read_text() for p in agent.rglob("*.py"))
