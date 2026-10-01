"""Architectural guard for ``backend/src/api/agent`` (agent audit round 8, D3).

``test_workspace_boundaries.py`` holds ``api/threads/workspace_routes`` to the
router -> service -> transaction contract, but nothing scanned ``api/agent``.
That gap let three read routes hand-roll a ``Workspace.owner_id == me`` join
that skipped ancestor soft-deletes and membership (R8-D3), and let the Stop
route own its own commits. Rules, per top-level function of every module:

  (a) no ``commit``/``rollback``/``flush``/``refresh`` call — services own the
      transaction boundary;
  (b) no ``AsyncSessionLocal``/``SessionLocal`` — a router does not open
      sessions of its own;
  (c) no ``Thread``/``Conversation``/``Workspace``/``WorkspaceMember`` column
      references — thread and workspace scope goes through
      ``services/threads/workspace_access`` (or a threads service built on it);
  (d) helper modules do not import the ``execute`` router module back.

``ALLOWLIST`` is recorded debt, not permission: each entry names the function,
the rule, the date and why it is still there. It may only shrink — a fixed
entry fails ``test_allowlist_has_no_stale_entries`` until it is removed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND_DIR = Path(__file__).resolve().parents[3]
AGENT_API_DIR = BACKEND_DIR / "src" / "api" / "agent"

TXN_METHODS = frozenset({"commit", "rollback", "flush", "refresh"})
SESSION_FACTORIES = frozenset({"AsyncSessionLocal", "SessionLocal"})
SCOPE_MODELS = frozenset({"Thread", "Conversation", "Workspace", "WorkspaceMember"})
ROUTER_MODULE = "execute"

_STREAMING_DEBT = (
    "2026-10-01: streaming.py is ~4.5k lines of run orchestration living in "
    "the API layer; it owns its sessions until it moves under services/agent "
    "(round-8 deferred item)."
)

# (module stem, top-level function, rule) -> reason.
ALLOWLIST: dict[tuple[str, str, str], str] = {
    ("execute", "_celery_dispatch", "session"): (
        "2026-10-01: the durable agent_runs row must commit on its own session "
        "before the broker publish; move into agent_run_service with the "
        "dispatch ordering intact."
    ),
    ("execute", "execute_agent", "txn"): (
        "2026-10-01: cleanup rollback after upsert_run (which commits) raises; "
        "belongs in upsert_run's own error path (agent_run_service, S8 area)."
    ),
    ("execute", "confirm_agent_action", "txn"): (
        "2026-10-01: rollback_confirmation_session cleans the request session "
        "after claim/projection failures; the durable confirmation claim "
        "should own it in agent_run_service."
    ),
    ("streaming", "stream_event_generator", "session"): _STREAMING_DEBT,
    ("streaming", "stream_event_generator", "txn"): _STREAMING_DEBT,
    ("streaming", "stream_event_generator", "router_import"): (
        "2026-10-01: late import of compatibility names re-exported by "
        "execute.py so mock.patch on the router module keeps working; import "
        "from the service modules when streaming.py moves."
    ),
    ("streaming", "stream_confirm_event_generator", "session"): _STREAMING_DEBT,
    ("streaming", "stream_confirm_event_generator", "txn"): _STREAMING_DEBT,
    ("streaming", "stream_confirm_event_generator", "router_import"): (
        "2026-10-01: same late compatibility import as stream_event_generator."
    ),
    ("harness_streaming", "stream_harness_run", "session"): (
        "2026-10-01: polls the run event ledger on short-lived sessions every "
        "250 ms; belongs in a services/harness observation helper."
    ),
}


def _module_files() -> list[Path]:
    return sorted(AGENT_API_DIR.glob("*.py"))


def _violations(path: Path) -> set[tuple[str, str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[tuple[str, str, str]] = set()
    for top in tree.body:
        owner = (
            top.name
            if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            else "<module>"
        )
        for node in ast.walk(top):
            rule = None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in TXN_METHODS
            ):
                rule = "txn"
            elif (
                isinstance(node, ast.Name)
                and node.id in SESSION_FACTORIES
                and owner != "<module>"
            ):
                rule = "session"
            elif (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in SCOPE_MODELS
            ):
                rule = "scope"
            elif (
                isinstance(node, ast.ImportFrom)
                and path.stem not in {ROUTER_MODULE, "__init__"}
                and (
                    (node.level == 1 and node.module == ROUTER_MODULE)
                    or node.module == f"src.api.agent.{ROUTER_MODULE}"
                )
            ):
                rule = "router_import"
            if rule is not None:
                found.add((path.stem, owner, rule))
    return found


def _all_violations() -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for path in _module_files():
        found |= _violations(path)
    return found


def test_guard_scans_the_agent_api_package() -> None:
    stems = {p.stem for p in _module_files()}
    assert {
        ROUTER_MODULE,
        "streaming",
    } <= stems, (
        f"{AGENT_API_DIR} no longer holds execute.py/streaming.py; update this guard."
    )


@pytest.mark.parametrize("path", _module_files(), ids=lambda p: p.name)
def test_module_respects_router_boundaries(path: Path) -> None:
    unexpected = sorted(_violations(path) - ALLOWLIST.keys())
    assert not unexpected, (
        f"{path} breaks the agent API boundary: {unexpected}. Move the "
        "transaction/session/scope query into a service (thread and workspace "
        "access goes through services/threads/workspace_access.py). Allowlisting "
        "is for pre-existing debt only, with a dated reason."
    )


def test_allowlist_has_no_stale_entries() -> None:
    stale = sorted(ALLOWLIST.keys() - _all_violations())
    assert not stale, (
        f"ALLOWLIST entries {stale} no longer match a violation; the debt was "
        "paid, so drop them in the same change."
    )


def test_allowlist_entries_are_dated() -> None:
    undated = sorted(key for key, why in ALLOWLIST.items() if not why[:4].isdigit())
    assert not undated, f"Allowlist entries need a dated reason: {undated}"


def test_no_scope_query_is_allowlisted() -> None:
    """R8-D3 class: hand-rolled thread/workspace scope has no grandfathering."""
    assert not [key for key in ALLOWLIST if key[2] == "scope"]
