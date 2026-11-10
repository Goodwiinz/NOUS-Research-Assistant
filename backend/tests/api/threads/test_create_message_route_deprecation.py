"""Audit C4: the two deprecated nested create-message routes are removed.

``docs/decisions/api-deprecation-window.md`` made the nested
``POST /api/v2/threads/{thread_id}/messages`` and
``POST /api/v2/workspaces/{ws}/conversations/{c}/threads/{t}/messages``
deletion-eligible on 2026-07-26 once no non-test caller remained (re-verified
2026-11-10). Only the flat ``POST /api/v2/messages`` creates messages over
REST; the nested paths keep their GET list routes.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.threads.threads import router as threads_router
from src.api.threads.workspaces import router as workspaces_router
from src.api.threads.workspaces import standalone_router as workspaces_standalone_router

pytestmark = pytest.mark.unit

_NESTED_THREAD = "/{thread_id}/messages"
_NESTED_WORKSPACE = (
    "/{workspace_id}/conversations/{conversation_id}/threads/{thread_id}/messages"
)
_IDS = "00000000-0000-4000-8000-000000000001"


def _methods(router: Any, path_suffix: str) -> set[str]:
    methods: set[str] = set()
    for route in router.routes:
        if getattr(route, "path", "").endswith(path_suffix):
            methods |= set(getattr(route, "methods", set()) or set())
    return methods


def test_nested_create_routes_are_gone_but_list_routes_remain() -> None:
    assert "POST" not in _methods(threads_router, _NESTED_THREAD)
    assert "GET" in _methods(threads_router, _NESTED_THREAD)
    assert "POST" not in _methods(workspaces_router, _NESTED_WORKSPACE)
    assert "GET" in _methods(workspaces_router, _NESTED_WORKSPACE)


def test_flat_create_route_is_the_canonical_rest_path() -> None:
    posts = [
        route
        for route in workspaces_standalone_router.routes
        if getattr(route, "path", "").endswith("/messages")
        and "POST" in getattr(route, "methods", set())
    ]
    assert len(posts) == 1
    assert not posts[0].deprecated


@pytest.mark.parametrize(
    ("router", "mount", "path"),
    [
        # main.py mounts the threads router under /api/v2.
        (threads_router, "/api/v2", f"/api/v2/threads/{_IDS}/messages"),
        (
            workspaces_router,
            "",
            f"/api/v2/workspaces/{_IDS}/conversations/{_IDS}/threads/{_IDS}/messages",
        ),
    ],
)
def test_posting_to_a_removed_route_is_method_not_allowed(
    router: Any, mount: str, path: str
) -> None:
    app = FastAPI()
    app.include_router(router, prefix=mount)
    response = TestClient(app).post(
        path, json={"thread_id": _IDS, "content": "x", "role": "user"}
    )
    # The GET list route still owns the path, so POST is 405, not a handler.
    assert response.status_code == 405
