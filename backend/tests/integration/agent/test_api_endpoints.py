"""Integration tests for agent API endpoints."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.agent.execute import _get_job, _set_job, _set_job_async, router
from tests.utils.agent_job_status import stub_durable_status_projection

pytestmark = pytest.mark.integration  # NOT asyncio -- TestClient is sync

USER_ID = "33333333-3333-3333-3333-333333333333"
ORG_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _stub_durable_status_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_durable_status_projection(monkeypatch)


# ---------------------------------------------------------------------------
# Fixtures (mirrors pattern from tests/unit/services/test_agent_integration.py)
# ---------------------------------------------------------------------------


def _make_mock_user(user_id: str = USER_ID):
    user = Mock()
    user.id = user_id
    user.email = "integration@example.com"
    user.first_name = "Integ"
    user.last_name = "Test"
    user.role = Mock(value="user")
    user.organization_id = ORG_ID
    user.is_active = True
    return user


def _make_mock_db():
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=Mock(return_value=None))
    )
    db.add = Mock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.begin = AsyncMock()
    db.get = AsyncMock(return_value=None)
    return db


@pytest.fixture
def mock_user():
    return _make_mock_user()


@pytest.fixture
def app_with_overrides(mock_user):
    """Create a FastAPI app with the agent router and dependency overrides."""
    from src.core.database import get_db
    from src.core.dependencies import get_current_user

    app = FastAPI()
    app.include_router(router)

    async def override_get_current_user():
        return mock_user

    async def override_get_db():
        return _make_mock_db()

    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_db] = override_get_db

    return app


@pytest.fixture
def client(app_with_overrides):
    @asynccontextmanager
    async def _no_lifespan(_app):
        yield

    app_with_overrides.router.lifespan_context = _no_lifespan
    with TestClient(app_with_overrides) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExecuteCreatesJob:
    """POST /execute should create a job and return a job_id."""

    def test_execute_creates_job(self, client):
        """POST /execute returns 200 with a job_id (patch _run_agent_graph as no-op)."""
        with patch(
            "src.api.agent.execute._run_agent_graph",
            new_callable=AsyncMock,
        ):
            response = client.post(
                "/api/v1/agent/execute",
                json={
                    "messages": [{"role": "user", "content": "hello"}],
                    "page_context": {"type": "unknown"},
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data
        assert len(data["job_id"]) > 0


class TestExecuteRequiresMessage:
    """POST /execute with invalid body should return 422."""

    def test_execute_requires_message(self, client):
        """POST /execute with missing 'messages' field returns 422."""
        response = client.post(
            "/api/v1/agent/execute",
            json={},
        )

        assert response.status_code == 422


class TestGetJobStatus:
    """GET /jobs/{id} should return the pre-set job."""

    @pytest.mark.asyncio
    async def test_get_job_status(self, client, mock_user):
        """Pre-set a job via _set_job, GET /jobs/{id} returns 200."""
        job_id = str(uuid4())
        await _set_job_async(
            job_id,
            {
                "status": "completed",
                "result": {
                    "message": {"role": "assistant", "content": "done"},
                },
                "tool_executions": [],
                "user_id": str(mock_user.id),
            },
        )

        response = client.get(f"/api/v1/agent/jobs/{job_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"


class TestGetJobNotFound:
    """GET /jobs/{random} should return 404."""

    def test_get_job_not_found(self, client):
        """GET /jobs/{random_id} returns 404 for a nonexistent job."""
        random_id = str(uuid4())
        response = client.get(f"/api/v1/agent/jobs/{random_id}")
        assert response.status_code == 404


class TestGetJobOwnership:
    """GET /jobs/{id} must fail closed on ownership."""

    @pytest.mark.asyncio
    async def test_other_users_job_returns_404(self, client):
        """A job owned by another user is invisible to the caller."""
        job_id = str(uuid4())
        await _set_job_async(
            job_id,
            {
                "status": "completed",
                "result": {"message": {"role": "assistant", "content": "secret"}},
                "tool_executions": [],
                "user_id": "someone-else",
            },
        )

        response = client.get(f"/api/v1/agent/jobs/{job_id}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_job_without_owner_returns_404(self, client):
        """Fail closed: a record missing user_id must not be readable.

        Regression test — terminal status writes used to drop user_id from
        the job record, and the old ``if job.get("user_id") and ...`` guard
        then let ANY authenticated user read the result.
        """
        job_id = str(uuid4())
        await _set_job_async(
            job_id,
            {
                "status": "completed",
                "result": {"message": {"role": "assistant", "content": "secret"}},
                "tool_executions": [],
            },
        )

        response = client.get(f"/api/v1/agent/jobs/{job_id}")
        assert response.status_code == 404


class TestThreadsList:
    """GET /threads should return 200 with an empty list when DB is mocked."""

    def test_threads_list(self, client):
        """GET /threads returns 200 (mock DB returns empty result set)."""
        mock_db = _make_mock_db()

        # The endpoint runs a SINGLE windowed query — select(Thread, count().over())
        # — and reads rows via .all(). An empty result => empty list, total 0.
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        from src.core.database import get_db

        async def override_get_db_with_threads():
            return mock_db

        client.app.dependency_overrides[get_db] = override_get_db_with_threads

        response = client.get("/api/v1/agent/threads")
        assert response.status_code == 200
        data = response.json()
        assert "threads" in data
        assert isinstance(data["threads"], list)
        assert data["total"] == 0
