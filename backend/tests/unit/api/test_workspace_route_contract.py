"""Freeze the public workspace API contract before Task 4.2 splits the router.

``backend/src/api/threads/workspaces.py`` (2,517 lines) exports two
``APIRouter`` objects — ``router`` (nested ``/api/v2/workspaces/...`` paths)
and ``standalone_router`` (flat ``/api/v2/...`` paths used by the frontend) —
covering 47 endpoints across workspaces, members, conversations, threads,
messages, and collections. Task 4.2 moves this into
``workspace_routes/{dependencies,presenters,workspaces,members,...}.py``. This
test pins the CURRENT wire contract (method, full path, status code,
response_model, deprecated flag) by walking the live router objects — not by
regexing source — so a behavior-preserving split can be verified against it.

This is a characterization test: it freezes what the code does today, not
what it should do. A mismatch here on ``develop`` (pre-split) is a real bug to
fix in its own commit before any structural movement; a mismatch after 4.2 is
a split that changed the public contract.

Deep RBAC/tenant/rollback behaviors are already covered elsewhere (recon:
``test_workspace_tenant_scope.py``, ``test_workspace_member_readd.py``,
``test_message_feedback_edit_guard.py``, ``backend/tests/api/threads/*``) and
are intentionally not duplicated here.
"""

from __future__ import annotations

from typing import Any, List, Tuple

import pytest
from fastapi import APIRouter

from src.api.threads.threads import router as threads_router
from src.api.threads.workspaces import THREAD_PREVIEW_MAX_CHARS
from src.api.threads.workspaces import router as workspaces_router
from src.api.threads.workspaces import standalone_router as workspaces_standalone_router
from src.schemas.chat import (
    ChatMessageListResponse,
    ChatMessageResponse,
    CollectionDetailResponse,
    CollectionListResponse,
    CollectionResponse,
    ConversationListResponse,
    ConversationResponse,
    ThreadDetailResponse,
    ThreadListResponse,
    ThreadResponse,
    WorkspaceDetailResponse,
    WorkspaceMemberResponse,
    WorkspaceResponse,
)

pytestmark = pytest.mark.unit

# response_model may be None, a pydantic model class, or a typing generic
# alias like List[WorkspaceResponse] — heterogeneous enough that Any is the
# honest slot type here.
RouteRow = Tuple[str, str, Any, int, bool]

# Each row: (method, path-suffix-after-router-prefix, response_model, status_code, deprecated)
# Order matches definition order in workspaces.py (also route-matching order).
NESTED_ROUTES = [
    ("POST", "", WorkspaceResponse, 201, False),
    ("GET", "", List[WorkspaceResponse], 200, False),
    ("GET", "/{workspace_id}", WorkspaceDetailResponse, 200, False),
    ("PATCH", "/{workspace_id}", WorkspaceResponse, 200, False),
    ("DELETE", "/{workspace_id}", None, 204, False),
    ("POST", "/{workspace_id}/members", WorkspaceMemberResponse, 201, False),
    (
        "PATCH",
        "/{workspace_id}/members/{user_id}",
        WorkspaceMemberResponse,
        200,
        False,
    ),
    ("DELETE", "/{workspace_id}/members/{user_id}", None, 204, False),
    ("POST", "/{workspace_id}/conversations", ConversationResponse, 201, False),
    ("GET", "/{workspace_id}/conversations", ConversationListResponse, 200, False),
    (
        "GET",
        "/{workspace_id}/conversations/{conversation_id}",
        ConversationResponse,
        200,
        False,
    ),
    (
        "PATCH",
        "/{workspace_id}/conversations/{conversation_id}",
        ConversationResponse,
        200,
        False,
    ),
    (
        "DELETE",
        "/{workspace_id}/conversations/{conversation_id}",
        None,
        204,
        False,
    ),
    (
        "POST",
        "/{workspace_id}/conversations/{conversation_id}/threads",
        ThreadResponse,
        201,
        False,
    ),
    ("GET", "/{workspace_id}/threads", ThreadListResponse, 200, False),
    (
        "GET",
        "/{workspace_id}/conversations/{conversation_id}/threads",
        ThreadListResponse,
        200,
        False,
    ),
    (
        "GET",
        "/{workspace_id}/conversations/{conversation_id}/threads/{thread_id}",
        ThreadDetailResponse,
        200,
        False,
    ),
    (
        "PATCH",
        "/{workspace_id}/conversations/{conversation_id}/threads/{thread_id}",
        ThreadResponse,
        200,
        False,
    ),
    (
        "DELETE",
        "/{workspace_id}/conversations/{conversation_id}/threads/{thread_id}",
        None,
        204,
        False,
    ),
    (
        "POST",
        "/{workspace_id}/conversations/{conversation_id}/threads/{thread_id}/messages",
        ChatMessageResponse,
        201,
        True,  # audit C4: only public POST create-message route not called by any client
    ),
    (
        "GET",
        "/{workspace_id}/conversations/{conversation_id}/threads/{thread_id}/messages",
        ChatMessageListResponse,
        200,
        False,
    ),
    (
        "PATCH",
        "/{workspace_id}/conversations/{conversation_id}/threads/{thread_id}/messages/{message_id}",
        ChatMessageResponse,
        200,
        False,
    ),
    ("POST", "/{workspace_id}/collections", CollectionResponse, 201, False),
    ("GET", "/{workspace_id}/collections", CollectionListResponse, 200, False),
    (
        "GET",
        "/{workspace_id}/collections/{collection_id}",
        CollectionDetailResponse,
        200,
        False,
    ),
    (
        "PATCH",
        "/{workspace_id}/collections/{collection_id}",
        CollectionResponse,
        200,
        False,
    ),
    ("DELETE", "/{workspace_id}/collections/{collection_id}", None, 204, False),
    (
        "POST",
        "/{workspace_id}/collections/{collection_id}/documents",
        CollectionDetailResponse,
        200,
        False,
    ),
    (
        "DELETE",
        "/{workspace_id}/collections/{collection_id}/documents",
        CollectionDetailResponse,
        200,
        False,
    ),
]

STANDALONE_ROUTES = [
    ("GET", "/conversations/{conversation_id}", ConversationResponse, 200, False),
    ("PATCH", "/conversations/{conversation_id}", ConversationResponse, 200, False),
    ("DELETE", "/conversations/{conversation_id}", None, 204, False),
    ("POST", "/threads", ThreadResponse, 201, False),
    ("GET", "/threads/{thread_id}", ThreadDetailResponse, 200, False),
    ("PATCH", "/threads/{thread_id}", ThreadResponse, 200, False),
    ("DELETE", "/threads/{thread_id}", None, 204, False),
    ("GET", "/threads/{thread_id}/messages", ChatMessageListResponse, 200, False),
    (
        "POST",
        "/messages",
        ChatMessageResponse,
        201,
        False,  # canonical create-message route (audit C4) — must stay un-deprecated
    ),
    ("GET", "/messages/{message_id}", ChatMessageResponse, 200, False),
    ("PATCH", "/messages/{message_id}", ChatMessageResponse, 200, False),
    ("DELETE", "/messages/{message_id}", None, 204, False),
    (
        "GET",
        "/conversations/{conversation_id}/threads",
        ThreadListResponse,
        200,
        False,
    ),
    ("POST", "/collections", CollectionResponse, 201, False),
    ("GET", "/collections/{collection_id}", CollectionDetailResponse, 200, False),
    ("PATCH", "/collections/{collection_id}", CollectionResponse, 200, False),
    ("DELETE", "/collections/{collection_id}", None, 204, False),
    (
        "POST",
        "/collections/{collection_id}/documents",
        CollectionDetailResponse,
        200,
        False,
    ),
    (
        "DELETE",
        "/collections/{collection_id}/documents",
        CollectionDetailResponse,
        200,
        False,
    ),
]


def _actual_table(router: APIRouter) -> List[RouteRow]:
    """Walk the live APIRouter's ``.routes`` (not source regexing) into
    (method, path, response_model, status_code, deprecated) tuples, in
    registration order."""
    table: List[RouteRow] = []
    for route in router.routes:
        methods = sorted(route.methods - {"HEAD"})
        assert len(methods) == 1, (
            f"expected exactly one non-HEAD HTTP method on {route.path}, "
            f"got {methods}"
        )
        table.append(
            (
                methods[0],
                route.path,
                route.response_model,
                route.status_code or 200,  # FastAPI leaves this None => 200 default
                bool(route.deprecated),
            )
        )
    return table


def _expected_table(
    prefix: str, rows: List[Tuple[str, str, Any, int, bool]]
) -> List[RouteRow]:
    return [
        (method, prefix + suffix, response_model, status_code, deprecated)
        for method, suffix, response_model, status_code, deprecated in rows
    ]


def test_nested_router_route_count_unchanged() -> None:
    assert len(workspaces_router.routes) == len(NESTED_ROUTES) == 29


def test_nested_router_contract_unchanged() -> None:
    expected = _expected_table(workspaces_router.prefix, NESTED_ROUTES)
    assert _actual_table(workspaces_router) == expected


def test_nested_router_prefix_and_tags_unchanged() -> None:
    assert workspaces_router.prefix == "/api/v2/workspaces"
    assert workspaces_router.tags == ["workspaces"]


def test_standalone_router_route_count_unchanged() -> None:
    assert len(workspaces_standalone_router.routes) == len(STANDALONE_ROUTES) == 19


def test_standalone_router_contract_unchanged() -> None:
    expected = _expected_table(workspaces_standalone_router.prefix, STANDALONE_ROUTES)
    assert _actual_table(workspaces_standalone_router) == expected


def test_standalone_router_prefix_and_tags_unchanged() -> None:
    assert workspaces_standalone_router.prefix == "/api/v2"
    assert workspaces_standalone_router.tags == ["workspaces-flat"]


def test_total_endpoint_count_is_48() -> None:
    total = len(workspaces_router.routes) + len(workspaces_standalone_router.routes)
    assert total == 48


def test_module_level_constant_unchanged() -> None:
    # recon risk: THREAD_PREVIEW_MAX_CHARS is shared module state; a split
    # must land it somewhere still importable from this path, unchanged.
    assert THREAD_PREVIEW_MAX_CHARS == 240


def test_nested_create_message_is_the_only_deprecated_route() -> None:
    """Audit C4: exactly one route in either router is deprecated — the
    deeply-nested workspace create-message route. Canonical assertion lives in
    ``test_create_message_route_deprecation.py``; repeated here as part of the
    full table, not a second source of truth."""
    deprecated = [
        (route.path, sorted(route.methods - {"HEAD"}))
        for router in (workspaces_router, workspaces_standalone_router)
        for route in router.routes
        if route.deprecated
    ]
    assert deprecated == [
        (
            "/api/v2/workspaces/{workspace_id}/conversations/{conversation_id}"
            "/threads/{thread_id}/messages",
            ["POST"],
        )
    ]


def test_threads_router_registered_before_workspaces_standalone_router() -> None:
    """main.py L628-636: ``threads_router`` (owns ``/api/v2/threads/bulk/*``)
    must be included before ``workspaces_standalone_router`` (owns
    ``/api/v2/threads/{thread_id}``) — reversed order lets the parameterized
    standalone route capture "bulk" as a thread_id before the literal bulk
    routes ever get a chance to match. A split of the standalone routes into
    a new module must preserve this include order in main.py."""
    from src.main import app  # local: only this test needs the full app graph

    def _index_of(target_router: APIRouter) -> int:
        for i, route in enumerate(app.routes):
            if getattr(route, "original_router", None) is target_router:
                return i
        raise AssertionError(f"{target_router!r} not found in app.routes")

    threads_index = _index_of(threads_router)
    standalone_index = _index_of(workspaces_standalone_router)
    assert threads_index < standalone_index, (
        "threads_router must be included before workspaces_standalone_router in "
        "main.py, or DELETE /api/v2/threads/bulk is swallowed by "
        "DELETE /api/v2/threads/{thread_id}"
    )


def test_bulk_delete_route_exists_on_threads_router() -> None:
    # Sanity check for the invariant above: the literal route it protects.
    bulk_delete = [
        route
        for route in threads_router.routes
        if route.path == "/threads/bulk" and "DELETE" in route.methods
    ]
    assert len(bulk_delete) == 1
