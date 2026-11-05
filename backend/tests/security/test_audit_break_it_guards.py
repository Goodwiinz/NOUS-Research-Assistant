"""Regression guards from the 2026-11 security audit.

Each test asserts the *safe* behavior for a finding, so it fails if the hole
reopens. They are introspection / source guards: no DB, network, or live infra.

Findings covered:
  F3  — POST /api/v1/arxiv/search is anonymous *by design*
        (middleware/multi_tenancy.py exempt list). The accepted-risk control is
        the per-client "arxiv" rate-limit bucket; guard that it stays applied.
  F5  — document/processing/realtime routers must not return raw str(e) in
        HTTP detail (leaks SQL text and bound parameters).
  F2  — collection attach must authorize documents against the caller's org,
        not the workspace's org (cross-tenant document disclosure).
"""

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SRC = BACKEND_ROOT / "src"


# ---------------------------------------------------------------------------
# F3 — anonymous arXiv search must stay behind the per-client arxiv bucket
# ---------------------------------------------------------------------------


def test_anonymous_arxiv_search_stays_rate_limited() -> None:
    """F3 (accepted risk): the endpoint is deliberately unauthenticated, so the
    only thing stopping an anonymous flood from draining the shared arXiv
    upstream gate is ApiRateLimitMiddleware's "arxiv" bucket. Fail if the path
    becomes skipped or falls through to a looser bucket.
    """
    from src.middleware.rate_limiting import is_skipped_path, resolve_bucket

    path = "/api/v1/arxiv/search"
    assert not is_skipped_path(path), "arxiv search must be rate limited"

    bucket = resolve_bucket(path, "POST")
    assert bucket.name == "arxiv", f"arxiv search uses bucket {bucket.name!r}"
    assert (
        bucket.requests / bucket.window <= 10 / 60
    ), "anonymous arxiv search budget loosened beyond 10 req/min per client"


# ---------------------------------------------------------------------------
# F5 — no raw str(e) / exception interpolation in HTTP error detail
# ---------------------------------------------------------------------------

_LEAK_ROUTERS = [
    SRC / "api" / "documents" / "documents.py",
    SRC / "api" / "documents" / "files.py",
    SRC / "api" / "documents" / "processing.py",
    SRC / "api" / "realtime" / "realtime_document_status.py",
]


def _httpexception_detail_leaks(tree: ast.AST) -> list[int]:
    """Return line numbers where an HTTPException detail interpolates an
    exception variable (str(e) or an f-string referencing the caught name)."""
    leaks: list[int] = []

    class Visitor(ast.NodeVisitor):
        def visit_Raise(self, node: ast.Raise) -> None:
            exc = node.exc
            if isinstance(exc, ast.Call) and _is_http_exception(exc.func):
                for kw in exc.keywords:
                    if kw.arg == "detail" and _value_interpolates_exc(kw.value):
                        leaks.append(kw.value.lineno)
            self.generic_visit(node)

    def _is_http_exception(func: ast.expr) -> bool:
        name = ""
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        return name == "HTTPException"

    def _value_interpolates_exc(value: ast.expr) -> bool:
        # detail=f"...{str(e)}..." or detail=f"...{e}..."
        if isinstance(value, ast.JoinedStr):
            for part in value.values:
                if isinstance(part, ast.FormattedValue):
                    inner = part.value
                    if isinstance(inner, ast.Call) and _calls_str(inner):
                        return True
                    if isinstance(inner, ast.Name) and inner.id in {
                        "e",
                        "exc",
                        "ex",
                        "error",
                        "err",
                    }:
                        return True
        # detail=str(e)
        if isinstance(value, ast.Call) and _calls_str(value):
            return True
        return False

    def _calls_str(call: ast.Call) -> bool:
        return (
            isinstance(call.func, ast.Name)
            and call.func.id == "str"
            and len(call.args) == 1
            and isinstance(call.args[0], ast.Name)
            and call.args[0].id in {"e", "exc", "ex", "error", "err"}
        )

    Visitor().visit(tree)
    return leaks


def test_error_responses_do_not_leak_exception_text() -> None:
    """F5: HTTP 500 detail must not echo raw exception text.

    GET /api/v2/realtime/documents/not-a-uuid/status raises an asyncpg
    DataError whose str(e) contains '[SQL: ...] [parameters: ...]'. Returning
    that in the response detail leaks schema and bound values. Fix: log with
    exc_info and return a fixed detail string.
    """
    offenders: dict[str, list[int]] = {}
    for path in _LEAK_ROUTERS:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text())
        lines = _httpexception_detail_leaks(tree)
        if lines:
            offenders[str(path.relative_to(BACKEND_ROOT))] = sorted(set(lines))

    assert (
        not offenders
    ), "HTTPException detail interpolates raw exception text (audit F5): " + "; ".join(
        f"{p}:{ls}" for p, ls in offenders.items()
    )


# ---------------------------------------------------------------------------
# F2 — collection attach must scope document access to the caller's org
# ---------------------------------------------------------------------------


def test_collection_attach_scopes_to_caller_org() -> None:
    """F2: add_documents_to_collection / create_collection resolve document
    access using collection.workspace.organization_id instead of the caller's
    org. Because workspaces admit cross-org members, an org-B editor in an
    org-A workspace can attach (and read titles of) org-A documents never
    shared with them. Fix: use current_user.organization_id.

    Guard: the workspace org must not be the value passed to the
    document-access resolver in this module.
    """
    path = SRC / "services" / "threads" / "collection_service.py"
    tree = ast.parse(path.read_text())

    RESOLVERS = {"get_accessible_document_or_none", "filter_owned_document_ids"}

    def _is_workspace_org_expr(node: ast.AST) -> bool:
        # <anything>.workspace.organization_id
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "organization_id"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "workspace"
        )

    # Names bound to a workspace-org value, directly or via getattr().
    tainted: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        val = node.value
        is_ws_org = _is_workspace_org_expr(val)
        if (
            isinstance(val, ast.Call)
            and getattr(val.func, "id", "") == "getattr"
            and len(val.args) >= 2
            and isinstance(val.args[0], ast.Attribute)
            and val.args[0].attr == "workspace"
            and isinstance(val.args[1], ast.Constant)
            and val.args[1].value == "organization_id"
        ):
            is_ws_org = True
        if is_ws_org:
            for t in node.targets:
                if isinstance(t, ast.Name):
                    tainted.add(t.id)

    def _arg_is_workspace_org(arg: ast.expr) -> bool:
        if _is_workspace_org_expr(arg):
            return True
        return isinstance(arg, ast.Name) and arg.id in tainted

    bad: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = (
            func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        )
        if fname not in RESOLVERS:
            continue
        operands = list(node.args) + [kw.value for kw in node.keywords]
        if any(_arg_is_workspace_org(a) for a in operands):
            bad.append(node.lineno)

    assert not bad, (
        "collection document-access resolved against workspace org, not caller "
        f"org (audit F2) at collection_service.py lines {sorted(set(bad))}"
    )


# ---------------------------------------------------------------------------
# F16 — no new JWT decoders outside the verified auth paths
# ---------------------------------------------------------------------------

# Modules allowed to call jwt.decode. The live verifiers re-load role and org
# from the DB; a decoder that trusts token claims (tenant_id / org_id /
# permissions) is a full tenant bypass the moment someone wires it up, which is
# how the removed services/core/auth.py, auth/ab_testing_auth.py and
# WebSocketConnectionManager.authenticate_websocket were dangerous.
_JWT_DECODE_ALLOWLIST = {
    "src/core/security.py",  # verify_token: Supabase + CLI tokens
    "src/websocket/auth.py",  # WebSocket authenticator (DB-backed)
    # Unimported today and pinned only by tests/scripts; candidates for removal
    # in a follow-up. Do not wire these up without a DB-backed rewrite.
    "src/security/enhanced_auth.py",
    "src/services/infrastructure/api_gateway.py",
    "src/services/security/user_management.py",
}


def test_jwt_decode_only_in_allowlisted_modules() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "decode"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "jwt"
            ):
                rel = str(path.relative_to(BACKEND_ROOT))
                if rel not in _JWT_DECODE_ALLOWLIST:
                    offenders.append(f"{rel}:{node.lineno}")
    assert (
        not offenders
    ), "jwt.decode outside the verified auth paths (audit F16): " + ", ".join(offenders)


# ---------------------------------------------------------------------------
# F5b — bulk thread results must not carry raw exception text
# ---------------------------------------------------------------------------


def test_bulk_thread_results_do_not_leak_exception_text() -> None:
    """BulkThreadResult.error is returned to the client, so the bulk
    update/delete/summarize paths must append stable messages, never str(e)
    or an f-string over the caught exception."""
    path = SRC / "services" / "threads" / "chat_service.py"
    tree = ast.parse(path.read_text())
    exc_names = {"e", "exc", "ex", "error", "err"}
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "results"
        ):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "str"
                and sub.args
                and isinstance(sub.args[0], ast.Name)
                and sub.args[0].id in exc_names
            ) or (
                isinstance(sub, ast.FormattedValue)
                and isinstance(sub.value, ast.Name)
                and sub.value.id in exc_names
            ):
                offenders.append(node.lineno)
    assert not offenders, (
        "bulk results interpolate raw exception text (audit F5b) at "
        f"chat_service.py lines {sorted(set(offenders))}"
    )
