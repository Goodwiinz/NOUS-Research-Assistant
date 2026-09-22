"""Characterization freeze of transaction ownership in the threads services.

This is the safety net PR 3 stands on. PR 3 will move transaction ownership
out of the leaf services and up to the route / use-case layer (``async with
db.begin()`` or an explicit handler-end commit), leaving these leaf functions
``flush()``-only and deleting the ``commit: bool`` flags — so that composing
two services in one request can be atomic (Codex's warning: today *every*
service commits, which blocks that). Before any of that flipping happens,
this test pins EXACTLY what each function does today: who calls
``commit()`` / ``flush()`` / ``refresh()`` / ``rollback()`` / ``begin_nested()``
and what every commit-gating flag defaults to.

It uses lightweight AST inspection (no DB, no execution) — the pattern
precedent is ``tests/unit/architecture/test_workspace_boundaries.py`` — so it
is a pure static freeze: change the source, and the row for that function
must change with it, deliberately.

HOW THIS TEST EVOLVES (read before editing):

    This table is UPDATED, not deleted, as PR 3 Task 3.2 flips functions to
    flush-only. Each flip is ONE reviewed edit to ONE row here:
    e.g. when ``workspace_service.update_workspace`` stops owning its commit,
    change its expected transaction set from ``{"commit"}`` to ``{"flush"}``
    (or ``set()`` if the caller flushes) IN THE SAME PR that flips it. A
    diff that touches a service's commit behavior without touching its row
    here is the mistake this test exists to catch. Do not weaken the
    assertions to "any of"; the value is that every row is exact.

Scope frozen here:

  (a) Leaf persistence services (``workspace/conversation/thread/message/
      collection_service`` + the read-only ``workspace_access`` funnel):
      the exact set of session-transaction methods each public function
      calls directly, and every keyword-only bool flag's default.
  (b) ``ChatService`` (the compat delegates): which methods own a
      transaction DIRECTLY (``create_message``, ``create_assistant_message``,
      the two ``bulk_*`` methods — all explicitly out of scope for the 4.3
      consolidation and staying that way) versus which delegate to a leaf
      service and therefore touch no transaction method themselves.
  (c) The route layer owns NO transaction boundary today (post-#1218):
      ``workspace_routes/*`` calls zero commit/rollback/flush/refresh — PR 3
      moves ownership *here*, so this must be the clean starting line.
  (d) Session LIFECYCLE is owned by ``MultiTenancyMiddleware`` (creates the
      ``AsyncSession``, assigns ``request.state.db``) and reused by
      ``get_db`` — this is the seam PR 3's route-layer ``begin()`` plugs
      into, so its shape is pinned for Task 3.2's design.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, FrozenSet, List, Tuple

import pytest

pytestmark = pytest.mark.unit

BACKEND_DIR = Path(__file__).resolve().parents[4]
SERVICES_DIR = BACKEND_DIR / "src" / "services" / "threads"
ROUTES_DIR = BACKEND_DIR / "src" / "api" / "threads" / "workspace_routes"

# Session methods that own / participate in a transaction boundary. PR 3's
# whole point is that today the leaf services call these, and afterwards only
# the route/use-case layer will.
TXN_METHODS: FrozenSet[str] = frozenset(
    {"commit", "rollback", "flush", "refresh", "begin_nested"}
)


# =============================================================================
# AST helpers (no DB, no imports of the modules under test)
# =============================================================================


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _functions_by_name(
    tree: ast.Module,
) -> Dict[str, ast.AST]:
    """Map every def/async-def name -> its node (class methods included).

    Names are unique within each module under test (no overloads), so a flat
    name->node map is unambiguous.
    """
    out: Dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


def _txn_methods_called(node: ast.AST) -> FrozenSet[str]:
    """Set of TXN_METHODS invoked as attribute calls anywhere in ``node``."""
    hits = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in TXN_METHODS
        ):
            hits.add(child.func.attr)
    return frozenset(hits)


def _kwonly_bool_defaults(node: ast.AST) -> Dict[str, bool]:
    """Keyword-only args of ``node`` whose default is a bool literal."""
    out: Dict[str, bool] = {}
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return out
    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        if isinstance(default, ast.Constant) and isinstance(default.value, bool):
            out[arg.arg] = default.value
    return out


def _forbidden_txn_calls(tree: ast.Module) -> List[str]:
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in TXN_METHODS
        ):
            hits.append(f"{node.func.attr}() at line {node.lineno}")
    return hits


def _forbidden_txn_calls_attrs(tree: ast.Module) -> List[str]:
    """Bare TXN-method attr names called anywhere in ``tree`` (no line info)."""
    return [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in TXN_METHODS
    ]


# =============================================================================
# (a) Leaf-service transaction ownership table — the freeze
# =============================================================================

# module stem -> {function name -> exact frozenset of TXN methods it calls}.
# EDIT ONE ROW PER DELIBERATE FLIP (see module docstring).
LEAF_TXN: Dict[str, Dict[str, FrozenSet[str]]] = {
    "workspace_access": {
        # Read-only access funnel: no persistence op owns a transaction.
        "user_can_access_workspace": frozenset(),
        "get_workspace": frozenset(),
        "get_conversation": frozenset(),
        "get_thread": frozenset(),
        "get_message": frozenset(),
        "get_collection": frozenset(),
        "get_accessible_document_or_none": frozenset(),
        # Two SELECTs (caller's org, then owned ids) — the message-attachment
        # tenant filter, moved here from ChatService so the agent stream's
        # user-turn persist applies the identical predicate. The caller owns
        # the transaction the attachment rows are added to.
        "filter_owned_document_ids": frozenset(),
    },
    "workspace_service": {
        # PR 3 Task 3.2 (workspaces + members flips): commit -> flush; the
        # workspaces.py/members.py routes (+ ChatService delegates) own commit.
        "create_workspace": frozenset({"flush"}),
        "list_workspaces": frozenset(),
        "update_workspace": frozenset({"flush"}),  # deliberately no refresh()
        "delete_workspace": frozenset({"flush"}),
        "add_member": frozenset({"flush"}),
        "update_member_role": frozenset({"flush"}),
        "remove_member": frozenset({"flush"}),
    },
    "conversation_service": {
        # PR 3 Task 3.2 (conversations flip): commit -> flush; conversations.py
        # route + ChatService delegates own the request commit.
        "create_conversation": frozenset({"flush"}),
        "list_conversations": frozenset(),
        "update_conversation": frozenset({"flush"}),  # deliberately no refresh()
        "delete_conversation": frozenset({"flush"}),
    },
    "thread_service": {
        "last_message_preview_expression": frozenset(),  # query builder, no db
        # PR 3 Task 3.2 (threads flip): commit -> flush; the ``commit`` flag on
        # create_thread was DELETED (now unconditional flush + refresh). The
        # route / ChatService owns the request commit; update_thread's resolve-
        # summary enqueue moved to enqueue_after_commit (not a txn method).
        "create_thread": frozenset({"flush", "refresh"}),
        "list_threads": frozenset(),
        "list_workspace_threads": frozenset(),
        "update_thread": frozenset({"flush", "refresh"}),
        "delete_thread": frozenset({"flush"}),
    },
    "message_service": {
        # PR 3 Task 3.2 (messages flip): commit -> flush; messages.py route +
        # ChatService delegates own the request commit.
        "get_message": frozenset(),
        "list_messages": frozenset(),
        "update_message_feedback": frozenset({"flush"}),  # deliberately no refresh()
        "delete_message": frozenset({"flush"}),
    },
    "collection_service": {
        # PR 3 Task 3.2 (collections flip): commit -> flush; route/ChatService
        # owns the request commit. re-fetch-after-flush sees flushed state.
        "get_collection": frozenset(),
        "create_collection": frozenset({"flush"}),  # re-fetches, no refresh()
        "list_collections": frozenset(),
        "update_collection": frozenset({"flush", "refresh"}),
        "delete_collection": frozenset({"flush"}),
        "add_documents_to_collection": frozenset({"flush"}),
        "remove_documents_from_collection": frozenset({"flush"}),
    },
}

# Every commit-gating / divergence bool flag and its current default. PR 3
# DELETES these flags as ownership moves up — when a flag is removed, delete
# its row here in the same PR.
LEAF_FLAG_DEFAULTS: Dict[Tuple[str, str, str], bool] = {
    ("workspace_service", "create_workspace", "enforce_org_match"): True,
    ("workspace_service", "list_workspaces", "filter_deleted_memberships"): True,
    ("workspace_service", "delete_workspace", "stamp_deleted_at"): False,
    ("conversation_service", "list_conversations", "order_pinned_first"): True,
    ("conversation_service", "delete_conversation", "stamp_deleted_at"): False,
    ("conversation_service", "delete_conversation", "require_admin"): False,
    # PR 3 Task 3.2 deleted ("thread_service","create_thread","commit") — the
    # leaf is now unconditionally flush-only, so its row is gone here too.
    ("thread_service", "list_threads", "with_preview"): False,
    ("thread_service", "update_thread", "trigger_resolve_summary"): False,
    ("thread_service", "delete_thread", "stamp_deleted_at"): False,
    ("message_service", "delete_message", "require_author_or_admin"): False,
}


def _leaf_txn_ids() -> List[Tuple[str, str]]:
    return [(mod, fn) for mod, table in LEAF_TXN.items() for fn in table]


class TestLeafServiceTransactionOwnership:
    """(a) Every leaf-service function's exact transaction footprint, frozen."""

    @pytest.mark.parametrize(
        ("module", "func"),
        _leaf_txn_ids(),
        ids=lambda v: v if isinstance(v, str) else "",
    )
    def test_function_owns_exactly_these_txn_methods(
        self, module: str, func: str
    ) -> None:
        tree = _parse(SERVICES_DIR / f"{module}.py")
        node = _functions_by_name(tree).get(func)
        assert node is not None, f"{module}.{func} vanished — update this freeze."
        actual = _txn_methods_called(node)
        expected = LEAF_TXN[module][func]
        assert actual == expected, (
            f"{module}.{func} transaction footprint changed: expected "
            f"{sorted(expected)}, got {sorted(actual)}. If PR 3 Task 3.2 "
            "flipped this function to flush-only, update THIS row in the same "
            "PR — that is the intended workflow, not a failure to route around."
        )

    @pytest.mark.parametrize(
        ("key", "expected"),
        list(LEAF_FLAG_DEFAULTS.items()),
        ids=lambda v: "-".join(v) if isinstance(v, tuple) else "",
    )
    def test_commit_flag_default(
        self, key: Tuple[str, str, str], expected: bool
    ) -> None:
        module, func, flag = key
        tree = _parse(SERVICES_DIR / f"{module}.py")
        node = _functions_by_name(tree).get(func)
        assert node is not None, f"{module}.{func} vanished — update this freeze."
        defaults = _kwonly_bool_defaults(node)
        assert flag in defaults, (
            f"{module}.{func} no longer has bool kw-only flag `{flag}`. If PR 3 "
            "deleted it (ownership moved up), delete its row in "
            "LEAF_FLAG_DEFAULTS in the same PR."
        )
        assert defaults[flag] is expected, (
            f"{module}.{func}(`{flag}`) default changed from {expected!r} to "
            f"{defaults[flag]!r} — a silent behavior flip. Confirm and update."
        )

    @pytest.mark.parametrize("module", sorted(LEAF_TXN))
    def test_leaf_table_covers_every_public_function(self, module: str) -> None:
        """Completeness guard (mirrors ChatService's ``test_delegate_and_
        direct_sets_cover_all_methods``): every module-level public function in
        a leaf module must have a row in ``LEAF_TXN`` — a new/renamed leaf
        function can't slip the freeze unclassified.

        Only module-level public defs are classified. Nested defs (inner
        helpers/closures) are intentionally excluded via ``tree.body`` (not
        ``ast.walk``): the freeze pins each module's public persistence API, not
        its private internals, so an inner ``def`` introduced inside an existing
        public function does not spuriously demand its own row.
        """
        tree = _parse(SERVICES_DIR / f"{module}.py")
        public_top = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.name.startswith("_")
        }
        classified = set(LEAF_TXN[module])
        missing = public_top - classified
        assert not missing, (
            f"{module}.py public function(s) {sorted(missing)} have no row in "
            "LEAF_TXN — classify each one's exact txn footprint in the freeze, "
            "in the same PR that introduced it."
        )
        stale = classified - public_top
        assert not stale, (
            f"LEAF_TXN[{module!r}] has row(s) {sorted(stale)} with no matching "
            "public function — a function was removed/renamed; update the freeze."
        )

    def test_create_thread_is_unconditionally_flush_only(self) -> None:
        """PR 3 Task 3.2 removed ``create_thread``'s ``commit`` flag: it now
        flushes unconditionally (+ the always-on refresh) and never commits, so
        composing it with further work in one request (the live ``threads.py``
        create path: create_thread -> project auto-link -> WS broadcast -> one
        commit) is atomic. This pins that the flag and the commit() branch are
        gone — re-introducing either is a deliberate, reviewed edit here."""
        tree = _parse(SERVICES_DIR / "thread_service.py")
        node = _functions_by_name(tree)["create_thread"]
        called = _txn_methods_called(node)
        assert called == frozenset({"flush", "refresh"}), (
            "create_thread must flush (+ refresh) and own no commit after the "
            f"PR 3 flip; found {sorted(called)}."
        )
        assert "commit" not in _kwonly_bool_defaults(node), (
            "create_thread's `commit` kw-only flag was deleted in PR 3 — it must "
            "not come back (the leaf is unconditionally flush-only now)."
        )


# =============================================================================
# (b) ChatService: direct-txn owners vs pure delegates
# =============================================================================

# Methods that own a transaction DIRECTLY today and are OUT OF SCOPE for the
# 4.3 consolidation (they will keep owning it until a later, explicit pass).
CHAT_DIRECT_TXN: Dict[str, FrozenSet[str]] = {
    "create_message": frozenset({"commit", "flush", "refresh"}),
    "create_assistant_message": frozenset({"commit", "flush", "refresh"}),
    "bulk_update_threads": frozenset({"begin_nested", "commit", "rollback", "refresh"}),
    "bulk_delete_threads": frozenset({"begin_nested", "commit", "rollback"}),
    # PR 3 Task 3.2 delegate-boundary commits: these methods delegate to a
    # now-flush-only leaf and own the request commit for their own callers
    # (delegate-then-commit). Each moved here from CHAT_PURE_DELEGATES in the
    # same commit that flipped its resource's leaf + route.
    # -- collections flip --
    "create_collection": frozenset({"commit"}),
    "add_documents_to_collection": frozenset({"commit"}),
    "remove_documents_from_collection": frozenset({"commit"}),
    # -- workspaces flip --
    "create_workspace": frozenset({"commit"}),
    "update_workspace": frozenset({"commit"}),
    "delete_workspace": frozenset({"commit"}),
    # -- conversations flip --
    "create_conversation": frozenset({"commit"}),
    "update_conversation": frozenset({"commit"}),
    "delete_conversation": frozenset({"commit"}),
    # -- threads flip (create_thread stays a pure delegate: flush-only, its
    #    threads.py caller owns the commit) --
    "update_thread": frozenset({"commit"}),
    "delete_thread": frozenset({"commit"}),
    # -- messages flip --
    # R5-M11: flush-only; the route commits after its thread-match check.
    "update_message_feedback": frozenset({"flush"}),
    "delete_message": frozenset({"commit"}),
}

# Every other ChatService method delegates to a leaf service (or is read-only)
# and must touch NO transaction method itself — the property PR 3 relies on to
# move ownership without rewriting these.
CHAT_PURE_DELEGATES: FrozenSet[str] = frozenset(
    {
        "_escape_like",
        "get_workspace",
        "list_workspaces",
        "_user_can_access_workspace",
        "get_conversation",
        "list_conversations",
        "create_thread",
        "get_thread",
        "list_threads",
        "bulk_summarize_threads",
        "_filter_owned_document_ids",
        "get_message",
        "list_messages",
        "get_collection",
        "list_collections",
        "get_thread_context",
        "search_conversations",
        "get_workspace_stats",
    }
)


class TestChatServiceTransactionOwnership:
    """(b) ChatService's direct owners are pinned; delegates own nothing."""

    @pytest.mark.parametrize(
        ("method", "expected"),
        list(CHAT_DIRECT_TXN.items()),
        ids=lambda v: v if isinstance(v, str) else "",
    )
    def test_direct_owner_transaction_footprint(
        self, method: str, expected: FrozenSet[str]
    ) -> None:
        tree = _parse(SERVICES_DIR / "chat_service.py")
        node = _functions_by_name(tree).get(method)
        assert node is not None, f"ChatService.{method} vanished — update freeze."
        actual = _txn_methods_called(node)
        assert actual == expected, (
            f"ChatService.{method} (a known DIRECT transaction owner, out of "
            f"scope for 4.3) footprint changed: expected {sorted(expected)}, "
            f"got {sorted(actual)}."
        )

    @pytest.mark.parametrize("method", sorted(CHAT_PURE_DELEGATES))
    def test_delegate_owns_no_transaction(self, method: str) -> None:
        tree = _parse(SERVICES_DIR / "chat_service.py")
        node = _functions_by_name(tree).get(method)
        assert node is not None, f"ChatService.{method} vanished — update freeze."
        actual = _txn_methods_called(node)
        assert actual == frozenset(), (
            f"ChatService.{method} now calls transaction method(s) "
            f"{sorted(actual)} directly — it is supposed to delegate that to a "
            "leaf service. If this is intended, move it to CHAT_DIRECT_TXN "
            "explicitly."
        )

    def test_delegate_and_direct_sets_cover_all_methods(self) -> None:
        """Guard against a new ChatService method slipping the freeze."""
        tree = _parse(SERVICES_DIR / "chat_service.py")
        methods = {
            name
            for name, node in _functions_by_name(tree).items()
            if name not in {"__init__"}
        }
        # get_chat_service is a module-level factory, not a method.
        methods.discard("get_chat_service")
        classified = set(CHAT_DIRECT_TXN) | set(CHAT_PURE_DELEGATES)
        missing = methods - classified
        assert not missing, (
            f"New/renamed ChatService method(s) {sorted(missing)} are not in "
            "this freeze — add each to CHAT_DIRECT_TXN or CHAT_PURE_DELEGATES."
        )


# =============================================================================
# (c) Route layer owns no transaction boundary (the clean starting line)
# =============================================================================


# Route modules (stems) whose handlers have been flipped to own their single
# handler-end ``commit`` (PR 3 Task 3.2). Each resource flip appends its module
# here IN THE SAME COMMIT that adds the ``await db.commit()`` to its handlers and
# flips its leaf service to flush-only. Empty = pre-move baseline (every router
# still owns nothing). This is the freeze twin of ``MIGRATED_TO_UOW`` in
# ``tests/unit/architecture/test_workspace_boundaries.py``; both advance together.
MIGRATED_ROUTE_MODULES: FrozenSet[str] = frozenset(
    {
        "collections",
        "members",
        "workspaces",
        "conversations",
        "threads",
        "messages",
    }
)


class TestRouteLayerOwnsNoTransaction:
    """(c) ``workspace_routes/*`` transaction ownership, frozen and advancing.

    Post-#1218 every router delegated all commits to the service layer (zero
    txn calls). PR 3 Task 3.2 moves ownership up to this layer one resource at a
    time: a migrated module owns EXACTLY ``commit`` (one per mutating handler,
    at handler end) and never rollback/flush/refresh — those stay with the
    middleware-owned session. Unmigrated modules must still own nothing. This
    freeze is UPDATED per flip (append to ``MIGRATED_ROUTE_MODULES``), never
    weakened."""

    def test_route_dir_exists(self) -> None:
        assert ROUTES_DIR.is_dir(), f"{ROUTES_DIR} missing — update this freeze."

    @pytest.mark.parametrize(
        "path",
        sorted(p for p in ROUTES_DIR.glob("*.py")),
        ids=lambda p: p.name if isinstance(p, Path) else "",
    )
    def test_route_module_owns_no_transaction(self, path: Path) -> None:
        called = set(_forbidden_txn_calls_attrs(_parse(path)))
        if path.stem in MIGRATED_ROUTE_MODULES:
            illegal = sorted(called - {"commit"})
            assert not illegal, (
                f"{path} is migrated to UoW but owns {illegal} — a migrated "
                "route may only call commit() (one per mutating handler at its "
                "end); rollback/flush/refresh stay with the middleware session."
            )
            assert "commit" in called, (
                f"{path} is in MIGRATED_ROUTE_MODULES but owns no commit() — "
                "either it was appended prematurely or a handler-end commit is "
                "missing."
            )
        else:
            assert not called, (
                f"{path} now owns a transaction boundary: {sorted(called)}. "
                "Pre-move, route handlers delegate all commits to the service "
                "layer. When PR 3 moves ownership here, append this module to "
                "MIGRATED_ROUTE_MODULES in the same commit."
            )


# =============================================================================
# (d) Session lifecycle ownership (feeds PR 3 Task 3.2's design)
# =============================================================================


class TestSessionLifecycleOwnership:
    """(d) The middleware creates+owns the AsyncSession; ``get_db`` reuses it.
    PR 3's route-layer ``begin()`` plugs into exactly this seam, so its shape
    is frozen here as the design reference (not a behavior it may silently
    change)."""

    def test_middleware_creates_and_assigns_request_state_db(self) -> None:
        src = (BACKEND_DIR / "src" / "middleware" / "multi_tenancy.py").read_text(
            encoding="utf-8"
        )
        assert "async with AsyncSessionLocal() as db:" in src, (
            "MultiTenancyMiddleware no longer creates the request-scoped "
            "AsyncSession via `async with AsyncSessionLocal() as db:` — the "
            "session-lifecycle owner PR 3 depends on moved."
        )
        assert "request.state.db = db" in src, (
            "MultiTenancyMiddleware no longer assigns `request.state.db` — the "
            "route/service session seam moved."
        )

    def test_get_db_reuses_the_middleware_session(self) -> None:
        src = (BACKEND_DIR / "src" / "core" / "database.py").read_text(encoding="utf-8")
        # get_db yields request.state.db when present, so route handlers and
        # the services they call share the ONE middleware-owned transaction —
        # the invariant that makes PR 3's single route-level begin() possible.
        assert 'getattr(request.state, "db", None)' in src, (
            "get_db no longer reuses the middleware's request.state.db session; "
            "route handlers would get a second, independent transaction and "
            "PR 3's single-transaction-per-request assumption would break."
        )
