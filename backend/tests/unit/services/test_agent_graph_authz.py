"""Endpoint authorization tests for agent graph endpoints.

Covers the headline auth fixes from the security audit:
- /graph/trace/{thread_id}: IDOR guard (ownership check)
- /graph/mermaid: admin-only gate
- KG find_paths intermediate node org scoping
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.agent.execute import router
from src.core.database import get_db
from src.core.dependencies import get_current_user, require_admin
from src.models.user import UserRole

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_user(user_id: str = "user-111", role: str = "user"):
    user = Mock()
    user.id = user_id
    user.email = "test@example.com"
    user.first_name = "Test"
    user.last_name = "User"
    # Use a real UserRole enum so has_permission works correctly.
    user.role = UserRole(role)
    user.organization_id = "test-org"
    user.is_active = True

    # Mock.has_permission() would return a truthy Mock, bypassing the check.
    # Wire in the real implementation so the admin gate actually enforces roles.
    _role_hierarchy = {
        UserRole.USER: 0,
        UserRole.ANALYST: 1,
        UserRole.CONTENT_MANAGER: 2,
        UserRole.ADMIN: 3,
    }

    def _has_permission(required_role: UserRole) -> bool:
        if isinstance(required_role, str):
            try:
                required_role = UserRole(required_role)
            except ValueError:
                return False
        return _role_hierarchy.get(user.role, 0) >= _role_hierarchy.get(
            required_role, 100
        )

    user.has_permission = _has_permission
    return user


def _make_mock_db(thread_result=None):
    """Return a mock async db session.

    thread_result controls what scalar_one_or_none() returns for the
    ownership query (None → 404, a Mock → owned thread).
    """
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=Mock(return_value=thread_result))
    )
    db.add = Mock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.begin = AsyncMock()
    db.get = AsyncMock(return_value=None)
    return db


def _build_app(user, db):
    """Build a minimal FastAPI app with the agent router and given overrides."""
    app = FastAPI()
    app.include_router(router)

    async def override_get_current_user():
        return user

    async def override_get_db():
        return db

    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_db] = override_get_db
    return app


def _build_app_with_require_admin(admin_user, db):
    """Build app that also overrides require_admin directly."""
    app = FastAPI()
    app.include_router(router)

    async def override_require_admin():
        return admin_user

    async def override_get_db():
        return db

    app.dependency_overrides[require_admin] = override_require_admin
    app.dependency_overrides[get_db] = override_get_db
    return app


@asynccontextmanager
async def _no_lifespan(_app):
    yield


def _client(app) -> TestClient:
    app.router.lifespan_context = _no_lifespan
    return TestClient(app)


# ---------------------------------------------------------------------------
# /graph/trace/{thread_id} — ownership tests
# ---------------------------------------------------------------------------


class TestGraphTrace:
    def test_graph_trace_returns_404_for_non_owner(self):
        """No access through workspace_access.get_thread → 404."""
        user = _make_mock_user("user-aaa")
        db = _make_mock_db()
        app = _build_app(user, db)

        valid_thread_id = str(uuid4())
        with patch(
            "src.api.agent.execute.workspace_access.get_thread",
            new=AsyncMock(return_value=None),
        ) as get_thread:
            with _client(app) as c:
                resp = c.get(f"/api/v1/agent/graph/trace/{valid_thread_id}")

        assert resp.status_code == 404
        assert get_thread.await_args.args[2] == user.id

    def test_graph_trace_returns_200_for_owner(self):
        """Owner gets 200 and a mermaid diagram."""
        user = _make_mock_user("user-aaa")
        db = _make_mock_db()
        app = _build_app(user, db)

        valid_thread_id = str(uuid4())

        with (
            patch(
                "src.services.agent.visualization.get_execution_trace_mermaid",
                new=AsyncMock(return_value="sequenceDiagram\nAlice->>Bob: hello"),
            ),
            patch(
                "src.api.agent.execute.workspace_access.get_thread",
                new=AsyncMock(return_value=Mock()),
            ),
        ):
            with _client(app) as c:
                resp = c.get(f"/api/v1/agent/graph/trace/{valid_thread_id}")

        assert resp.status_code == 200
        assert "mermaid" in resp.json()

    def test_graph_trace_rejects_malformed_thread_id(self):
        """A non-UUID path segment is rejected by the Path pattern → 422."""
        user = _make_mock_user("user-aaa")
        db = _make_mock_db()
        app = _build_app(user, db)

        with _client(app) as c:
            resp = c.get("/api/v1/agent/graph/trace/not-a-uuid")

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /graph/mermaid — admin gate tests
# ---------------------------------------------------------------------------


class TestGraphMermaid:
    def test_graph_mermaid_forbidden_for_non_admin(self):
        """A regular user (role=USER) is rejected with 403."""
        user = _make_mock_user("user-aaa", role="user")
        db = _make_mock_db()
        app = _build_app(user, db)

        with _client(app) as c:
            resp = c.get("/api/v1/agent/graph/mermaid")

        assert resp.status_code == 403

    def test_graph_mermaid_allows_admin(self):
        """Admin (via require_admin override) gets 200 with a mermaid diagram."""
        admin_user = _make_mock_user("user-admin", role="admin")
        db = _make_mock_db()
        app = _build_app_with_require_admin(admin_user, db)

        with patch(
            "src.services.agent.visualization.get_graph_mermaid",
            return_value="graph TD\nA-->B",
        ):
            with _client(app) as c:
                resp = c.get("/api/v1/agent/graph/mermaid")

        assert resp.status_code == 200
        assert "mermaid" in resp.json()


# ---------------------------------------------------------------------------
# PART B2 — KG find_paths intermediate node org scoping
# ---------------------------------------------------------------------------


class TestFindPathsNodeScoping:
    """Unit tests for intermediate-node org scoping in find_paths."""

    def _run_find_paths(self, organization_id):
        """Run find_paths with a fake session; return the captured query."""
        from src.services.knowledge_graph.knowledge_graph_service import (
            knowledge_graph_service,
        )

        captured = {}

        class FakeResult:
            def __iter__(self):
                return iter([])

        class FakeSession:
            def run(self, query, params):
                captured["query"] = query
                captured["params"] = params
                return FakeResult()

        @asynccontextmanager
        async def _fake_ctx():
            yield FakeSession()

        fake_session_cm = MagicMock()
        fake_session_cm.__enter__ = Mock(return_value=FakeSession())
        fake_session_cm.__exit__ = Mock(return_value=False)

        with patch.object(
            knowledge_graph_service, "get_session", return_value=fake_session_cm
        ):
            knowledge_graph_service.find_paths(
                "a", "b", organization_id=organization_id
            )

        return captured.get("query", "")

    def test_find_paths_scopes_intermediate_nodes_to_org(self):
        """When organization_id is set, query must filter intermediate nodes."""
        query = self._run_find_paths(organization_id="org-1")
        assert "nodes(path)" in query
        assert "organization_id" in query

    def test_find_paths_no_intermediate_scope_without_org(self):
        """When organization_id is None, intermediate node filter is absent."""
        query = self._run_find_paths(organization_id=None)
        assert "nodes(path)" not in query
