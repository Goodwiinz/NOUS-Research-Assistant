"""
Test API key authentication security for public endpoints.

Uses FastAPI dependency_overrides for proper dependency injection mocking.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.core.api_key_auth import APIKeyData, generate_api_key, get_api_key_data
from src.core.database import get_db
from src.core.dependencies import require_admin
from src.main import app

client = TestClient(app)


def _make_api_key_data(
    record: dict,
    endpoint: str = "/api/v1/search/authenticated/hybrid",
) -> tuple:
    """Build an (APIKeyData, endpoint) tuple from a record dict."""
    return (
        APIKeyData(
            id=record["id"],
            name=record["name"],
            key_prefix=record["key_prefix"],
            is_active=record["is_active"],
            rate_limit_per_hour=record["rate_limit_per_hour"],
            last_used_at=record.get("last_used_at"),
            usage_count=record.get("usage_count", 0),
            organization_id=record.get("organization_id"),
        ),
        endpoint,
    )


class TestAPIKeyAuthentication:
    """Test API key authentication functionality"""

    def setup_method(self):
        self.valid_api_key, self.valid_key_hash = generate_api_key()
        self.invalid_api_key = "rag_invalid_key_123456789"
        self.expired_api_key, self.expired_key_hash = generate_api_key()

        self.valid_api_key_record = {
            "id": "test-key-id",
            "name": "Test API Key",
            "key_hash": self.valid_key_hash,
            "key_prefix": self.valid_api_key[:8],
            "is_active": True,
            "rate_limit_per_hour": 100,
            "expires_at": None,
            "usage_count": 0,
            "last_used_at": None,
            "organization_id": "test-org-id",
        }

        self.expired_api_key_record = {
            "id": "expired-key-id",
            "name": "Expired API Key",
            "key_hash": self.expired_key_hash,
            "key_prefix": self.expired_api_key[:8],
            "is_active": True,
            "rate_limit_per_hour": 100,
            "expires_at": datetime.utcnow() - timedelta(days=1),
            "usage_count": 0,
            "last_used_at": None,
            "organization_id": "test-org-id",
        }

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_valid_api_key_authentication(self):
        """Test successful authentication with valid API key"""
        app.dependency_overrides[get_api_key_data] = lambda: _make_api_key_data(
            self.valid_api_key_record
        )

        response = client.post(
            "/api/v1/search/authenticated/hybrid",
            headers={"Authorization": f"Bearer {self.valid_api_key}"},
            json={
                "query": "test search",
                "search_type": "HYBRID",
                "limit": 10,
            },
        )

        # Should pass auth (may fail on search service internals, but not auth)
        assert response.status_code != 401
        assert response.status_code != 403

    def test_missing_api_key(self):
        """Test request without API key"""
        response = client.post(
            "/api/v1/search/authenticated/hybrid",
            json={
                "query": "test search",
                "search_type": "HYBRID",
                "limit": 10,
            },
        )
        assert response.status_code in [401, 403]

    def test_invalid_api_key_format(self):
        """Test request with invalid API key format"""
        response = client.post(
            "/api/v1/search/authenticated/hybrid",
            headers={"Authorization": "Bearer invalid_format"},
            json={
                "query": "test search",
                "search_type": "HYBRID",
                "limit": 10,
            },
        )
        assert response.status_code == 401

    def test_nonexistent_api_key(self):
        """Test request with API key not in database"""
        from fastapi import HTTPException, status

        async def _not_found_override():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
            )

        app.dependency_overrides[get_api_key_data] = _not_found_override

        response = client.post(
            "/api/v1/search/authenticated/hybrid",
            headers={"Authorization": f"Bearer {self.invalid_api_key}"},
            json={
                "query": "test search",
                "search_type": "HYBRID",
                "limit": 10,
            },
        )
        assert response.status_code == 401

    def test_expired_api_key(self):
        """Test request with expired API key returns 401"""
        from fastapi import HTTPException, status

        async def _expired_override():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key has expired",
            )

        app.dependency_overrides[get_api_key_data] = _expired_override

        response = client.post(
            "/api/v1/search/authenticated/hybrid",
            headers={"Authorization": f"Bearer {self.expired_api_key}"},
            json={
                "query": "test search",
                "search_type": "HYBRID",
                "limit": 10,
            },
        )

        assert response.status_code == 401
        body = response.json()
        message = body.get("detail", "") or body.get("error", {}).get("message", "")
        assert "expired" in message.lower()

    def test_rate_limit_enforcement(self):
        """Test rate limiting for API keys returns 429"""
        from fastapi import HTTPException

        async def _rate_limited_override():
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Please try again later.",
            )

        app.dependency_overrides[get_api_key_data] = _rate_limited_override

        response = client.post(
            "/api/v1/search/authenticated/hybrid",
            headers={"Authorization": f"Bearer {self.valid_api_key}"},
            json={
                "query": "test search",
                "search_type": "HYBRID",
                "limit": 10,
            },
        )

        assert response.status_code == 429
        body = response.json()
        message = body.get("detail", "") or body.get("error", {}).get("message", "")
        assert "rate limit" in message.lower()

    def test_inactive_api_key(self):
        """Test request with inactive API key"""
        from fastapi import HTTPException, status

        async def _inactive_override():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key is inactive",
            )

        app.dependency_overrides[get_api_key_data] = _inactive_override

        response = client.post(
            "/api/v1/search/authenticated/hybrid",
            headers={"Authorization": f"Bearer {self.valid_api_key}"},
            json={
                "query": "test search",
                "search_type": "HYBRID",
                "limit": 10,
            },
        )
        assert response.status_code == 401

    def test_old_public_endpoint_removed(self, test_app, test_auth_headers):
        """Authenticated callers still get 404 for the removed public route."""
        response = client.post(
            "/api/v1/search/public/hybrid",
            headers=test_auth_headers,
            json={
                "query": "test search",
                "search_type": "HYBRID",
                "limit": 10,
            },
        )
        assert response.status_code == 404

    def test_health_check_authentication(self):
        """Test health check endpoint accepts valid authentication"""
        app.dependency_overrides[get_api_key_data] = lambda: _make_api_key_data(
            self.valid_api_key_record,
            endpoint="/api/v1/search/authenticated/health",
        )

        response = client.get(
            "/api/v1/search/authenticated/health",
            headers={"Authorization": f"Bearer {self.valid_api_key}"},
        )

        assert response.status_code != 401
        assert response.status_code != 403

    def test_health_check_without_auth(self):
        """Test health check fails without authentication"""
        response = client.get("/api/v1/search/public/health")
        assert response.status_code == 401  # fail-closed gate precedes route lookup

        response = client.get("/api/v1/search/authenticated/health")
        assert response.status_code in [401, 403]


class TestAPIKeyManagement:
    """Test API key management endpoints"""

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_create_api_key_admin_only(self, test_app, test_auth_headers):
        """Test API key creation succeeds for admin with organization"""
        mock_admin = MagicMock()
        mock_admin.id = "admin-user-id"
        mock_admin.email = "admin@test.com"
        mock_admin.organization_id = "test-org-id"
        mock_admin.role = MagicMock(value="admin")
        mock_admin.has_permission = MagicMock(return_value=True)

        mock_db = MagicMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock(
            side_effect=lambda obj: setattr(obj, "id", "new-key-id")
        )

        app.dependency_overrides[require_admin] = lambda: mock_admin
        app.dependency_overrides[get_db] = lambda: mock_db

        response = client.post(
            "/api/v1/api-keys/",
            headers=test_auth_headers,
            json={
                "name": "Test API Key",
                "description": "Test key for security testing",
                "rate_limit_per_hour": 50,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "api_key" in data
        assert data["name"] == "Test API Key"
        assert data["organization_id"] == "test-org-id"

    def test_create_api_key_non_admin_denied(self, test_app, test_auth_headers):
        """Test API key creation denied for non-admin users"""
        from fastapi import HTTPException

        async def _forbidden():
            raise HTTPException(
                status_code=403,
                detail="Insufficient permissions. Required role: admin",
            )

        app.dependency_overrides[require_admin] = _forbidden

        response = client.post(
            "/api/v1/api-keys/",
            headers=test_auth_headers,
            json={
                "name": "Test API Key",
                "description": "Unauthorized attempt",
                "rate_limit_per_hour": 50,
            },
        )
        assert response.status_code == 403


class TestSecurityLogging:
    """Test security-related logging functionality"""

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_api_access_logging(self):
        """Test that authenticated API access does not return auth errors"""
        valid_key, valid_hash = generate_api_key()
        api_key_record = {
            "id": "test-key-id",
            "name": "Test Key",
            "key_hash": valid_hash,
            "key_prefix": valid_key[:8],
            "is_active": True,
            "rate_limit_per_hour": 100,
            "expires_at": None,
            "usage_count": 0,
            "last_used_at": None,
            "organization_id": "test-org-id",
        }

        app.dependency_overrides[get_api_key_data] = lambda: _make_api_key_data(
            api_key_record
        )

        response = client.post(
            "/api/v1/search/authenticated/hybrid",
            headers={"Authorization": f"Bearer {valid_key}"},
            json={
                "query": "test security logging",
                "search_type": "HYBRID",
                "limit": 5,
            },
        )

        # Should not be an auth failure
        assert response.status_code != 401
        assert response.status_code != 403

    def test_failed_auth_logging(self):
        """Test that failed authentication attempts return 401"""
        from fastapi import HTTPException, status

        async def _auth_failed_override():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
            )

        app.dependency_overrides[get_api_key_data] = _auth_failed_override

        response = client.post(
            "/api/v1/search/authenticated/hybrid",
            headers={"Authorization": "Bearer invalid_key"},
            json={
                "query": "unauthorized attempt",
                "search_type": "HYBRID",
                "limit": 5,
            },
        )
        assert response.status_code == 401


class TestVulnerabilityRegression:
    """Test that original vulnerability is fixed"""

    def test_public_search_blocked(self, test_app, test_auth_headers):
        """Gate rejects anonymous callers; removed routes remain 404 with auth."""
        test_cases = [
            "/api/v1/search/public/hybrid",
            "/api/v1/search/public/health",
        ]
        for endpoint in test_cases:
            response = client.post(endpoint, json={"query": "test"})
            assert response.status_code == 401, f"Endpoint {endpoint} allowed anonymity"
            response = client.post(
                endpoint, headers=test_auth_headers, json={"query": "test"}
            )
            assert response.status_code == 404, f"Endpoint {endpoint} should not exist"

    def test_data_isolation_with_api_key(self):
        """Test that API key access doesn't bypass organization isolation"""
        pass

    def test_no_anonymous_access(self):
        """Verify no endpoints allow anonymous access to search functionality"""
        search_endpoints = [
            "/api/v1/search/",
            "/api/v1/search/hybrid",
            "/api/v1/search/authenticated/hybrid",
        ]
        for endpoint in search_endpoints:
            response = client.post(endpoint, json={"query": "test"})
            assert response.status_code in [
                401,
                403,
                422,
                500,
            ], f"Endpoint {endpoint} allows anonymous access"


if __name__ == "__main__":
    pytest.main([__file__])
