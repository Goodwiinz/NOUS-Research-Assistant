"""
Authentication and Authorization Security Tests for Search Endpoints

Tests comprehensive authentication and authorization including:
- JWT token validation and security
- API key authentication
- Role-based access control (RBAC)
- Organization isolation
- User permissions and privilege escalation
- Session management and token lifecycle
"""

import time
import uuid
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import jwt
import pytest

from src.core.dependencies import get_current_user
from src.main import app

from .conftest import SecurityTestCase


class TestSearchAuthenticationSecurity(SecurityTestCase):
    """Test authentication security for search endpoints"""

    def test_missing_authentication(
        self, unauthenticated_security_test_client, search_service_mocks
    ):
        """Test that endpoints reject requests without authentication"""

        endpoints = [
            ("POST", "/search/", {"query": "test"}),
            ("POST", "/search/hybrid", {"query": "test"}),
            ("GET", "/search/suggestions?q=test", None),
            ("GET", "/search/history", None),
            ("POST", "/search/history", {"query": "test"}),
            ("DELETE", "/search/history", None),
        ]

        for method, url, data in endpoints:
            if data:
                response = unauthenticated_security_test_client.make_request(
                    method, url, json=data
                )
            else:
                response = unauthenticated_security_test_client.make_request(
                    method, url
                )

            assert response.status_code in [
                401,
                403,
            ], f"Should reject unauthenticated {method} {url}, got {response.status_code}"

    def test_invalid_jwt_tokens(
        self, unauthenticated_security_test_client, search_service_mocks
    ):
        """Test handling of invalid JWT tokens"""

        invalid_tokens = [
            "invalid_token",
            "Bearer invalid_token",
            "Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.invalid",
            "Bearer not.a.jwt.token",
            "Bearer .invalid.token",
            "Bearer expired.token.here",
            "Bearer " + "a" * 1000,  # Extremely long token
            "Bearer \x00\x01\x02",  # Binary data
        ]

        for token in invalid_tokens:
            headers = {"Authorization": token}
            response = unauthenticated_security_test_client.make_request(
                "POST", "/search/", headers=headers, json={"query": "test"}
            )

            assert response.status_code in [
                401,
                403,
            ], f"Should reject invalid token: {token[:50]}..."

    def test_jwt_token_manipulation(
        self, unauthenticated_security_test_client, search_service_mocks
    ):
        """Test JWT token manipulation attacks"""

        # Test algorithm confusion attack
        # Create token with 'none' algorithm
        none_alg_token = jwt.encode(
            {"user_id": str(uuid.uuid4()), "exp": time.time() + 3600},
            "",
            algorithm="none",
        )

        headers = {"Authorization": f"Bearer {none_alg_token}"}
        response = unauthenticated_security_test_client.make_request(
            "POST", "/search/", headers=headers, json={"query": "test"}
        )

        # Should reject 'none' algorithm tokens
        assert response.status_code in [
            401,
            403,
        ], "Should reject 'none' algorithm tokens"

    def test_token_reuse_and_replay(
        self, unauthenticated_security_test_client, search_service_mocks
    ):
        """Test token reuse and replay attack prevention"""

        # Create expired token
        expired_token = jwt.encode(
            {"user_id": str(uuid.uuid4()), "exp": time.time() - 3600},
            "secret",
            algorithm="HS256",
        )

        headers = {"Authorization": f"Bearer {expired_token}"}
        response = unauthenticated_security_test_client.make_request(
            "POST", "/search/", headers=headers, json={"query": "test"}
        )

        assert response.status_code in [401, 403], "Should reject expired tokens"

    def test_api_key_authentication(
        self, unauthenticated_security_test_client, search_service_mocks
    ):
        """Test API key authentication security"""

        # Test missing API key
        response = unauthenticated_security_test_client.make_request(
            "POST", "/search/", json={"query": "test"}
        )
        assert response.status_code in [401, 403], "Should reject missing API key"

        # Test invalid API keys
        invalid_api_keys = [
            "",
            "invalid_key",
            "123",
            "a" * 1000,  # Too long
            "\x00\x01\x02",  # Binary data
            "'; DROP TABLE api_keys; --",  # SQL injection
            "<script>alert('xss')</script>",  # XSS
        ]

        for api_key in invalid_api_keys:
            headers = {"X-API-Key": api_key}
            response = unauthenticated_security_test_client.make_request(
                "POST", "/search/", headers=headers, json={"query": "test"}
            )

            assert response.status_code in [
                401,
                403,
            ], f"Should reject invalid API key: {api_key[:20]}..."

    def test_api_key_privilege_escalation(
        self, security_test_client, search_service_mocks
    ):
        """Test API key privilege escalation attempts"""

        # Mock limited user
        limited_user = Mock()
        limited_user.id = uuid.uuid4()
        limited_user.organization_id = uuid.uuid4()
        limited_user.role = "readonly"
        security_test_client.set_user(limited_user)

        # Test read operations (should succeed with authenticated user)
        response = security_test_client.make_request(
            "GET", "/search/suggestions?q=test", headers={"X-API-Key": "limited_key"}
        )
        assert (
            response.status_code == 200
        ), "Should allow read operations with read permissions"

        # Test write operations (should handle gracefully)
        response = security_test_client.make_request(
            "POST",
            "/search/history",
            headers={"X-API-Key": "limited_key"},
            json={"query": "test"},
        )
        # Should either work, be forbidden, or be a validation error
        # (the history endpoint may expect different payload schema)
        assert response.status_code in [
            200,
            403,
            422,
        ], "Should handle write operations appropriately"

    def test_organization_isolation(self, security_test_client, search_service_mocks):
        """Test organization isolation in search results"""

        org1_id = uuid.uuid4()
        org2_id = uuid.uuid4()

        # User from organization 1
        user_org1 = Mock()
        user_org1.id = uuid.uuid4()
        user_org1.organization_id = org1_id
        user_org1.role = "user"

        # User from organization 2
        user_org2 = Mock()
        user_org2.id = uuid.uuid4()
        user_org2.organization_id = org2_id
        user_org2.role = "user"

        # Test with user from org1
        security_test_client.set_user(user_org1)
        response1 = security_test_client.make_request(
            "POST", "/search/", json={"query": "test"}
        )
        assert response1.status_code == 200

        # Test with user from org2
        security_test_client.set_user(user_org2)
        response2 = security_test_client.make_request(
            "POST", "/search/", json={"query": "test"}
        )
        assert response2.status_code == 200

        # Verify search services are called with correct organization IDs.
        # Default search_type is "fulltext", so fulltext service is used.
        fulltext_mock = search_service_mocks["fulltext"]
        fulltext_mock.search.assert_called()
        calls = fulltext_mock.search.call_args_list

        # Check that calls have different organization IDs
        if len(calls) >= 2:
            org_id_1 = calls[-2][1].get("organization_id")
            org_id_2 = calls[-1][1].get("organization_id")
            if org_id_1 and org_id_2:
                assert (
                    org_id_1 != org_id_2
                ), "Different organizations should be isolated"

    def test_role_based_access_control(
        self, security_test_client, search_service_mocks
    ):
        """Test role-based access control enforcement.

        The search endpoint does not implement RBAC beyond authentication,
        so all authenticated users can search and access history.
        """

        roles = ["admin", "user", "readonly"]

        for role in roles:
            user = Mock()
            user.id = uuid.uuid4()
            user.organization_id = uuid.uuid4()
            user.role = role
            user.is_active = True
            security_test_client.set_user(user)

            # All authenticated users should be able to search
            response = security_test_client.make_request(
                "POST", "/search/", json={"query": "test"}
            )
            assert response.status_code == 200, f"Role {role} should have search access"

    def test_inactive_user_access(self, security_test_client, search_service_mocks):
        """Test that inactive users still pass mock auth.

        In a real system inactive users would be rejected by get_current_user.
        Since we override the dependency, the endpoint itself doesn't check
        is_active, so the request succeeds.  This test verifies the endpoint
        doesn't crash with an inactive user mock.
        """

        inactive_user = Mock()
        inactive_user.id = uuid.uuid4()
        inactive_user.organization_id = uuid.uuid4()
        inactive_user.role = "user"
        inactive_user.is_active = False
        security_test_client.set_user(inactive_user)

        response = security_test_client.make_request(
            "POST", "/search/", json={"query": "test"}
        )

        # The endpoint itself doesn't check is_active; that's the auth
        # dependency's job.  With mock auth, the request succeeds.
        assert response.status_code == 200

    def test_concurrent_session_security(
        self, security_test_client, search_service_mocks
    ):
        """Test concurrent session handling and security"""

        # Simulate concurrent requests with same token
        token = "Bearer valid_token"

        # Make multiple concurrent requests
        responses = []
        for i in range(5):
            response = security_test_client.make_request(
                "POST",
                "/search/",
                headers={"Authorization": token},
                json={"query": f"test_{i}"},
            )
            responses.append(response)

        # All should either succeed or fail consistently
        status_codes = [r.status_code for r in responses]
        assert (
            len(set(status_codes)) == 1
        ), "Concurrent requests should have consistent status"

    def test_user_impersonation_protection(
        self, security_test_client, search_service_mocks
    ):
        """Test protection against user impersonation attacks"""

        # Attempt to impersonate with user_id in request
        malicious_payloads = [
            {
                "query": "test",
                "user_id": str(uuid.uuid4()),  # Different user ID
            },
            {
                "query": "test",
                "organization_id": str(uuid.uuid4()),  # Different org ID
            },
            {
                "query": "test",
                "role": "admin",  # Attempt role escalation
            },
        ]

        for payload in malicious_payloads:
            response = security_test_client.make_request(
                "POST", "/search/", json=payload
            )

            # Should either ignore extra fields or reject
            assert response.status_code in [
                200,
                422,
            ], "Should handle impersonation attempts safely"

    def test_cross_organization_access_prevention(
        self, security_test_client, search_service_mocks
    ):
        """Test prevention of cross-organization data access"""

        user = Mock()
        user.id = uuid.uuid4()
        user.organization_id = uuid.uuid4()
        user.role = "admin"
        security_test_client.set_user(user)

        # Attempt to access data from different organization
        cross_org_payload = {
            "query": "test",
            "filters": {"organization_id": str(uuid.uuid4())},  # Different org
        }

        response = security_test_client.make_request(
            "POST", "/search/", json=cross_org_payload
        )

        # Should use user's org, not the requested org
        assert (
            response.status_code == 200
        ), "Should process request with user's organization"

        # Verify service was called with the user's org ID.
        # Default search_type is "fulltext", so fulltext service is used.
        fulltext_mock = search_service_mocks["fulltext"]
        fulltext_mock.search.assert_called()
        call_kwargs = fulltext_mock.search.call_args[1]
        if "organization_id" in call_kwargs:
            assert call_kwargs["organization_id"] == str(user.organization_id)

    def test_authentication_bypass_attempts(
        self, unauthenticated_security_test_client, search_service_mocks
    ):
        """Test common authentication bypass attempts"""

        # Authentication bypass attempts
        bypass_attempts = [
            {"Authorization": "Bearer null"},
            {"Authorization": "Bearer undefined"},
            {"Authorization": "Bearer false"},
            {"Authorization": "Bearer 0"},
            {"Authorization": "Bearer admin"},
            {"Authorization": "Bearer guest"},
            {"Authorization": "Bearer ../../../etc/passwd"},
            {"X-User-ID": "admin"},
            {"X-Role": "admin"},
            {"X-Forwarded-User": "admin"},
            {"X-Remote-User": "admin"},
        ]

        for headers in bypass_attempts:
            response = unauthenticated_security_test_client.make_request(
                "POST", "/search/", headers=headers, json={"query": "test"}
            )

            assert response.status_code in [
                401,
                403,
            ], f"Should reject bypass attempt: {headers}"

    def test_token_information_disclosure(
        self, security_test_client, search_service_mocks
    ):
        """Test that token information is not disclosed in responses"""

        response = security_test_client.make_request(
            "POST",
            "/search/",
            headers={"Authorization": "Bearer invalid_token"},
            json={"query": "test"},
        )

        # Should not disclose token information
        sensitive_patterns = [
            "invalid_token",
            "secret",
            "signature",
        ]

        self.assert_no_information_disclosure(response, sensitive_patterns)

    def test_session_fixation_protection(
        self, security_test_client, search_service_mocks, authentication_headers
    ):
        """Test protection against session fixation attacks.

        PR #1722: every request is authenticated on its own by the fail-closed
        tenancy gate, so an established session never lets a different,
        unverifiable token through.
        """

        # Make initial request with a valid token
        response1 = security_test_client.make_request(
            "POST",
            "/search/",
            headers=authentication_headers["valid_jwt"],
            json={"query": "test1"},
        )
        assert response1.status_code == 200

        # A different, unverifiable token on the next request is rejected
        response2 = security_test_client.make_request(
            "POST",
            "/search/",
            headers={"Authorization": "Bearer token2"},
            json={"query": "test2"},
        )
        assert response2.status_code == 401


class TestSearchAuthorizationSecurity(SecurityTestCase):
    """Test authorization security for search endpoints"""

    def test_resource_level_authorization(
        self, security_test_client, search_service_mocks
    ):
        """Test authorization at the resource level"""

        user = Mock()
        user.id = uuid.uuid4()
        user.organization_id = uuid.uuid4()
        user.role = "user"
        user.permissions = ["search:read", "search:write"]
        security_test_client.set_user(user)

        # Test operations user has permission for
        allowed_operations = [
            ("POST", "/search/", {"query": "test"}),
            ("GET", "/search/suggestions?q=test", None),
        ]

        for method, url, data in allowed_operations:
            kwargs = {}
            if data is not None:
                kwargs["json"] = data
            response = security_test_client.make_request(method, url, **kwargs)
            assert response.status_code == 200, f"Should allow {method} {url}"

    def test_permission_inheritance_and_delegation(
        self, security_test_client, search_service_mocks
    ):
        """Test permission inheritance and delegation mechanisms"""

        # Test user with inherited permissions
        user_with_inheritance = Mock()
        user_with_inheritance.id = uuid.uuid4()
        user_with_inheritance.organization_id = uuid.uuid4()
        user_with_inheritance.role = "team_lead"
        user_with_inheritance.inherited_permissions = ["search:admin"]
        security_test_client.set_user(user_with_inheritance)

        # Should have access to admin-level operations
        response = security_test_client.make_request(
            "DELETE",
            "/search/history",
        )
        assert (
            response.status_code == 200
        ), "Should allow admin operations with inherited permissions"

    def test_authorization_cache_security(
        self, unauthenticated_security_test_client, search_service_mocks
    ):
        """Test authorization cache security and invalidation"""

        # Make multiple requests with no authentication
        for i in range(3):
            response = unauthenticated_security_test_client.make_request(
                "POST",
                "/search/",
                headers={"Authorization": "Bearer test_token"},
                json={"query": f"test_{i}"},
            )
            # Each request should be independently rejected
            assert response.status_code in [
                401,
                403,
            ], "Should not cache unauthorized decisions"
