"""
Integration tests for Bulk Thread API Endpoints (GOO-52)

Tests the bulk thread operation endpoints:
- POST /threads/bulk/resolve
- POST /threads/bulk/archive
- DELETE /threads/bulk

These tests verify API behavior, authorization, and WebSocket broadcasting.
"""

# CRITICAL: Set environment variables BEFORE any imports from src
# This ensures database.py uses SQLite instead of PostgreSQL
import os
import sys
from pathlib import Path

backend_dir = Path(__file__).parent.parent.parent
sys.path.insert(0, str(backend_dir))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ASYNC_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ENVIRONMENT", "testing")
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-integration-tests")

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from src.api.threads import (
    check_bulk_archive_rate_limit,
    check_bulk_delete_rate_limit,
    check_bulk_resolve_rate_limit,
)
from src.core.database import get_db
from src.core.dependencies import get_current_user


def _make_thread(thread_id: UUID, created_by_id: UUID | None = None) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=thread_id,
        conversation_id=uuid4(),
        title="Test Thread",
        summary=None,
        status="resolved",
        last_message_at=now,
        message_count=1,
        token_count=10,
        created_by_id=created_by_id or uuid4(),
        created_at=now,
        updated_at=now,
    )


class TestBulkResolveEndpoint:
    """Test POST /threads/bulk/resolve endpoint"""

    @pytest.fixture
    def mock_app(self, test_app):
        """Create FastAPI app with mocked dependencies (src.main app with the tenancy-gate test seam)"""
        return test_app

    @pytest.fixture
    def test_user(self):
        """Create a test user"""
        user = Mock()
        user.id = uuid4()
        user.email = "test@example.com"
        user.is_active = True
        return user

    @pytest.fixture
    def auth_headers(self, test_auth_headers):
        """Bearer token the tenancy-gate test seam (tests/conftest.py) accepts."""
        return test_auth_headers

    @pytest.fixture
    def mock_chat_service(self):
        """Create mock ChatService"""
        service = Mock()
        return service

    @pytest.fixture
    def mock_thread_event_service(self):
        """Create mock ThreadEventService"""
        service = Mock()
        service.broadcast_threads_bulk_updated = AsyncMock()
        return service

    @pytest.fixture
    def mock_db(self):
        """Create mock database session"""
        return Mock()

    def test_bulk_resolve_success(
        self, mock_app, test_user, auth_headers, mock_chat_service, mock_db
    ):
        """Test successful bulk resolve with valid thread IDs"""
        thread_ids = [uuid4() for _ in range(3)]

        # Mock successful updates
        mock_results = [
            (tid, True, None, _make_thread(tid, test_user.id)) for tid in thread_ids
        ]
        mock_chat_service.bulk_update_threads = AsyncMock(return_value=mock_results)

        # Override dependencies
        mock_app.dependency_overrides[get_current_user] = lambda: test_user
        mock_app.dependency_overrides[get_db] = lambda: mock_db
        # Mock rate limiter to pass
        mock_app.dependency_overrides[check_bulk_resolve_rate_limit] = lambda: True

        with patch(
            "src.api.threads.threads.get_chat_service", return_value=mock_chat_service
        ):
            with patch("src.api.threads.threads.thread_event_service") as mock_event:
                mock_event.broadcast_threads_bulk_updated = AsyncMock()

                client = TestClient(mock_app)
                response = client.post(
                    "/api/v2/threads/bulk/resolve",
                    json={"thread_ids": [str(tid) for tid in thread_ids]},
                    headers=auth_headers,
                )

        # Clear overrides
        mock_app.dependency_overrides = {}

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == 3
        assert data["succeeded"] == 3
        assert data["failed"] == 0

    def test_bulk_resolve_partial_success(
        self, mock_app, test_user, auth_headers, mock_chat_service, mock_db
    ):
        """Test bulk resolve with partial success (some threads not found)"""
        thread_ids = [uuid4() for _ in range(4)]

        # Mock partial success - first 2 succeed, last 2 fail
        mock_results = [
            (thread_ids[0], True, None, _make_thread(thread_ids[0], test_user.id)),
            (thread_ids[1], True, None, _make_thread(thread_ids[1], test_user.id)),
            (thread_ids[2], False, "Not found", None),
            (thread_ids[3], False, "Insufficient permissions", None),
        ]
        mock_chat_service.bulk_update_threads = AsyncMock(return_value=mock_results)

        # Override dependencies
        mock_app.dependency_overrides[get_current_user] = lambda: test_user
        mock_app.dependency_overrides[get_db] = lambda: mock_db
        mock_app.dependency_overrides[check_bulk_resolve_rate_limit] = lambda: True

        with patch(
            "src.api.threads.threads.get_chat_service", return_value=mock_chat_service
        ):
            with patch("src.api.threads.threads.thread_event_service") as mock_event:
                mock_event.broadcast_threads_bulk_updated = AsyncMock()

                client = TestClient(mock_app)
                response = client.post(
                    "/api/v2/threads/bulk/resolve",
                    json={"thread_ids": [str(tid) for tid in thread_ids]},
                    headers=auth_headers,
                )

        # Clear overrides
        mock_app.dependency_overrides = {}

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == 4
        assert data["succeeded"] == 2
        assert data["failed"] == 2

    def test_bulk_resolve_broadcasts_websocket_event(
        self, mock_app, test_user, auth_headers, mock_chat_service, mock_db
    ):
        """Test that bulk resolve broadcasts WebSocket event"""
        thread_ids = [uuid4() for _ in range(2)]

        mock_results = [
            (tid, True, None, _make_thread(tid, test_user.id)) for tid in thread_ids
        ]
        mock_chat_service.bulk_update_threads = AsyncMock(return_value=mock_results)

        # Override dependencies
        mock_app.dependency_overrides[get_current_user] = lambda: test_user
        mock_app.dependency_overrides[get_db] = lambda: mock_db
        mock_app.dependency_overrides[check_bulk_resolve_rate_limit] = lambda: True

        with patch(
            "src.api.threads.threads.get_chat_service", return_value=mock_chat_service
        ):
            with patch("src.api.threads.threads.thread_event_service") as mock_event:
                mock_event.broadcast_threads_bulk_updated = AsyncMock()

                client = TestClient(mock_app)
                response = client.post(
                    "/api/v2/threads/bulk/resolve",
                    json={"thread_ids": [str(tid) for tid in thread_ids]},
                    headers=auth_headers,
                )

                # Verify WebSocket broadcast was called
                mock_event.broadcast_threads_bulk_updated.assert_called_once()
                call_args = mock_event.broadcast_threads_bulk_updated.call_args
                assert call_args.kwargs["action"] == "resolved"

        # Clear overrides
        mock_app.dependency_overrides = {}

    def test_bulk_resolve_invalid_thread_ids_format(
        self, mock_app, test_user, auth_headers
    ):
        """Test bulk resolve with invalid thread_ids format"""
        mock_app.dependency_overrides[get_current_user] = lambda: test_user
        mock_app.dependency_overrides[get_db] = lambda: Mock()
        mock_app.dependency_overrides[check_bulk_resolve_rate_limit] = lambda: True

        client = TestClient(mock_app)
        response = client.post(
            "/api/v2/threads/bulk/resolve",
            json={"thread_ids": ["not-a-valid-uuid"]},
            headers=auth_headers,
        )
        mock_app.dependency_overrides = {}

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_bulk_resolve_empty_thread_ids(self, mock_app, test_user, auth_headers):
        """Test bulk resolve with empty thread_ids list"""
        mock_app.dependency_overrides[get_current_user] = lambda: test_user
        mock_app.dependency_overrides[get_db] = lambda: Mock()
        mock_app.dependency_overrides[check_bulk_resolve_rate_limit] = lambda: True

        client = TestClient(mock_app)
        response = client.post(
            "/api/v2/threads/bulk/resolve",
            json={"thread_ids": []},
            headers=auth_headers,
        )
        mock_app.dependency_overrides = {}

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_bulk_resolve_unauthenticated(self, mock_app):
        """Test bulk resolve without authentication"""
        client = TestClient(mock_app)
        response = client.post(
            "/api/v2/threads/bulk/resolve", json={"thread_ids": [str(uuid4())]}
        )

        assert response.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )

    def test_bulk_resolve_exceeds_max_limit(self, mock_app, test_user, auth_headers):
        """Test bulk resolve with more than 100 thread IDs"""
        thread_ids = [str(uuid4()) for _ in range(101)]

        mock_app.dependency_overrides[get_current_user] = lambda: test_user
        mock_app.dependency_overrides[get_db] = lambda: Mock()
        mock_app.dependency_overrides[check_bulk_resolve_rate_limit] = lambda: True

        client = TestClient(mock_app)
        response = client.post(
            "/api/v2/threads/bulk/resolve",
            json={"thread_ids": thread_ids},
            headers=auth_headers,
        )

        mock_app.dependency_overrides = {}

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestBulkArchiveEndpoint:
    """Test POST /threads/bulk/archive endpoint"""

    @pytest.fixture
    def mock_app(self, test_app):
        """Create FastAPI app (src.main app with the tenancy-gate test seam)"""
        return test_app

    @pytest.fixture
    def test_user(self):
        """Create a test user"""
        user = Mock()
        user.id = uuid4()
        user.email = "test@example.com"
        user.is_active = True
        return user

    @pytest.fixture
    def auth_headers(self, test_auth_headers):
        """Bearer token the tenancy-gate test seam (tests/conftest.py) accepts."""
        return test_auth_headers

    @pytest.fixture
    def mock_chat_service(self):
        """Create mock ChatService"""
        return Mock()

    @pytest.fixture
    def mock_db(self):
        """Create mock database session"""
        return Mock()

    def test_bulk_archive_success(
        self, mock_app, test_user, auth_headers, mock_chat_service, mock_db
    ):
        """Test successful bulk archive"""
        thread_ids = [uuid4() for _ in range(3)]

        mock_results = [
            (tid, True, None, _make_thread(tid, test_user.id)) for tid in thread_ids
        ]
        mock_chat_service.bulk_update_threads = AsyncMock(return_value=mock_results)

        # Override dependencies
        mock_app.dependency_overrides[get_current_user] = lambda: test_user
        mock_app.dependency_overrides[get_db] = lambda: mock_db
        mock_app.dependency_overrides[check_bulk_archive_rate_limit] = lambda: True

        with patch(
            "src.api.threads.threads.get_chat_service", return_value=mock_chat_service
        ):
            with patch("src.api.threads.threads.thread_event_service") as mock_event:
                mock_event.broadcast_threads_bulk_updated = AsyncMock()

                client = TestClient(mock_app)
                response = client.post(
                    "/api/v2/threads/bulk/archive",
                    json={"thread_ids": [str(tid) for tid in thread_ids]},
                    headers=auth_headers,
                )

        mock_app.dependency_overrides = {}

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["succeeded"] == 3

    def test_bulk_archive_broadcasts_archive_action(
        self, mock_app, test_user, auth_headers, mock_chat_service, mock_db
    ):
        """Test that bulk archive broadcasts 'archived' action"""
        thread_ids = [uuid4()]

        mock_results = [
            (thread_ids[0], True, None, _make_thread(thread_ids[0], test_user.id))
        ]
        mock_chat_service.bulk_update_threads = AsyncMock(return_value=mock_results)

        # Override dependencies
        mock_app.dependency_overrides[get_current_user] = lambda: test_user
        mock_app.dependency_overrides[get_db] = lambda: mock_db
        mock_app.dependency_overrides[check_bulk_archive_rate_limit] = lambda: True

        with patch(
            "src.api.threads.threads.get_chat_service", return_value=mock_chat_service
        ):
            with patch("src.api.threads.threads.thread_event_service") as mock_event:
                mock_event.broadcast_threads_bulk_updated = AsyncMock()

                client = TestClient(mock_app)
                response = client.post(
                    "/api/v2/threads/bulk/archive",
                    json={"thread_ids": [str(thread_ids[0])]},
                    headers=auth_headers,
                )

                call_args = mock_event.broadcast_threads_bulk_updated.call_args
                assert call_args.kwargs["action"] == "archived"

        mock_app.dependency_overrides = {}


class TestBulkDeleteEndpoint:
    """Test DELETE /threads/bulk endpoint"""

    @pytest.fixture
    def mock_app(self, test_app):
        """Create FastAPI app (src.main app with the tenancy-gate test seam)"""
        return test_app

    @pytest.fixture
    def test_user(self):
        """Create a test user"""
        user = Mock()
        user.id = uuid4()
        user.email = "test@example.com"
        user.is_active = True
        return user

    @pytest.fixture
    def auth_headers(self, test_auth_headers):
        """Bearer token the tenancy-gate test seam (tests/conftest.py) accepts."""
        return test_auth_headers

    @pytest.fixture
    def mock_chat_service(self):
        """Create mock ChatService"""
        return Mock()

    @pytest.fixture
    def mock_db(self):
        """Create mock database session"""
        return Mock()

    def test_bulk_delete_success(
        self, mock_app, test_user, auth_headers, mock_chat_service, mock_db
    ):
        """Test successful bulk delete"""
        thread_ids = [uuid4() for _ in range(3)]

        mock_results = [(tid, True, None) for tid in thread_ids]
        mock_chat_service.bulk_delete_threads = AsyncMock(return_value=mock_results)

        # Override dependencies
        mock_app.dependency_overrides[get_current_user] = lambda: test_user
        mock_app.dependency_overrides[get_db] = lambda: mock_db
        mock_app.dependency_overrides[check_bulk_delete_rate_limit] = lambda: True

        with patch(
            "src.api.threads.threads.get_chat_service", return_value=mock_chat_service
        ):
            with patch("src.api.threads.threads.thread_event_service") as mock_event:
                mock_event.broadcast_threads_bulk_updated = AsyncMock()

                client = TestClient(mock_app)
                response = client.request(
                    method="DELETE",
                    url="/api/v2/threads/bulk",
                    json={"thread_ids": [str(tid) for tid in thread_ids]},
                    headers=auth_headers,
                )

        mock_app.dependency_overrides = {}

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == 3
        assert data["succeeded"] == 3

    def test_bulk_delete_partial_success(
        self, mock_app, test_user, auth_headers, mock_chat_service, mock_db
    ):
        """Test bulk delete with partial success"""
        thread_ids = [uuid4() for _ in range(3)]

        mock_results = [
            (thread_ids[0], True, None),
            (thread_ids[1], False, "Not found"),
            (thread_ids[2], True, None),
        ]
        mock_chat_service.bulk_delete_threads = AsyncMock(return_value=mock_results)

        # Override dependencies
        mock_app.dependency_overrides[get_current_user] = lambda: test_user
        mock_app.dependency_overrides[get_db] = lambda: mock_db
        mock_app.dependency_overrides[check_bulk_delete_rate_limit] = lambda: True

        with patch(
            "src.api.threads.threads.get_chat_service", return_value=mock_chat_service
        ):
            with patch("src.api.threads.threads.thread_event_service") as mock_event:
                mock_event.broadcast_threads_bulk_updated = AsyncMock()

                client = TestClient(mock_app)
                response = client.request(
                    method="DELETE",
                    url="/api/v2/threads/bulk",
                    json={"thread_ids": [str(tid) for tid in thread_ids]},
                    headers=auth_headers,
                )

        mock_app.dependency_overrides = {}

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["succeeded"] == 2
        assert data["failed"] == 1

    def test_bulk_delete_broadcasts_deleted_action(
        self, mock_app, test_user, auth_headers, mock_chat_service, mock_db
    ):
        """Test that bulk delete broadcasts 'deleted' action"""
        thread_ids = [uuid4()]

        mock_results = [(thread_ids[0], True, None)]
        mock_chat_service.bulk_delete_threads = AsyncMock(return_value=mock_results)

        # Override dependencies
        mock_app.dependency_overrides[get_current_user] = lambda: test_user
        mock_app.dependency_overrides[get_db] = lambda: mock_db
        mock_app.dependency_overrides[check_bulk_delete_rate_limit] = lambda: True

        with patch(
            "src.api.threads.threads.get_chat_service", return_value=mock_chat_service
        ):
            with patch("src.api.threads.threads.thread_event_service") as mock_event:
                mock_event.broadcast_threads_bulk_updated = AsyncMock()

                client = TestClient(mock_app)
                response = client.request(
                    method="DELETE",
                    url="/api/v2/threads/bulk",
                    json={"thread_ids": [str(thread_ids[0])]},
                    headers=auth_headers,
                )

                call_args = mock_event.broadcast_threads_bulk_updated.call_args
                assert call_args.kwargs["action"] == "deleted"

        mock_app.dependency_overrides = {}

    def test_bulk_delete_requires_authentication(self, mock_app):
        """Test bulk delete requires authentication"""
        client = TestClient(mock_app)
        response = client.request(
            method="DELETE",
            url="/api/v2/threads/bulk",
            json={"thread_ids": [str(uuid4())]},
        )

        assert response.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )


class TestBulkOperationsResponseSchema:
    """Test that response schema matches BulkThreadResponse"""

    @pytest.fixture
    def mock_app(self, test_app):
        """Create FastAPI app (src.main app with the tenancy-gate test seam)"""
        return test_app

    @pytest.fixture
    def test_user(self):
        """Create a test user"""
        user = Mock()
        user.id = uuid4()
        user.email = "test@example.com"
        user.is_active = True
        return user

    @pytest.fixture
    def auth_headers(self, test_auth_headers):
        """Bearer token the tenancy-gate test seam (tests/conftest.py) accepts."""
        return test_auth_headers

    def test_response_has_required_fields(self, mock_app, test_user, auth_headers):
        """Test response includes all required fields"""
        thread_ids = [uuid4()]

        mock_service = Mock()
        mock_results = [
            (thread_ids[0], True, None, _make_thread(thread_ids[0], test_user.id))
        ]
        mock_service.bulk_update_threads = AsyncMock(return_value=mock_results)

        mock_app.dependency_overrides[get_current_user] = lambda: test_user
        mock_app.dependency_overrides[get_db] = lambda: Mock()
        mock_app.dependency_overrides[check_bulk_resolve_rate_limit] = lambda: True

        with patch(
            "src.api.threads.threads.get_chat_service", return_value=mock_service
        ):
            with patch("src.api.threads.threads.thread_event_service") as mock_event:
                mock_event.broadcast_threads_bulk_updated = AsyncMock()

                client = TestClient(mock_app)
                response = client.post(
                    "/api/v2/threads/bulk/resolve",
                    json={"thread_ids": [str(thread_ids[0])]},
                    headers=auth_headers,
                )
        mock_app.dependency_overrides = {}

        data = response.json()
        assert "total" in data
        assert "succeeded" in data
        assert "failed" in data
        assert "results" in data
        assert isinstance(data["results"], list)

    def test_results_have_correct_structure(self, mock_app, test_user, auth_headers):
        """Test each result has correct structure"""
        thread_id = uuid4()

        mock_service = Mock()
        mock_results = [(thread_id, True, None, _make_thread(thread_id, test_user.id))]
        mock_service.bulk_update_threads = AsyncMock(return_value=mock_results)

        mock_app.dependency_overrides[get_current_user] = lambda: test_user
        mock_app.dependency_overrides[get_db] = lambda: Mock()
        mock_app.dependency_overrides[check_bulk_resolve_rate_limit] = lambda: True

        with patch(
            "src.api.threads.threads.get_chat_service", return_value=mock_service
        ):
            with patch("src.api.threads.threads.thread_event_service") as mock_event:
                mock_event.broadcast_threads_bulk_updated = AsyncMock()

                client = TestClient(mock_app)
                response = client.post(
                    "/api/v2/threads/bulk/resolve",
                    json={"thread_ids": [str(thread_id)]},
                    headers=auth_headers,
                )
        mock_app.dependency_overrides = {}

        data = response.json()
        result = data["results"][0]
        assert "thread_id" in result
        assert "success" in result
        # error and thread are optional
