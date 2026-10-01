"""Lock in the retirement of POST /auth/change-password.

The endpoint was dead by construction. It called
``AuthService.change_password``, which gated on
``verify_password(current_password, user.password_hash)`` — but under hosted
GoTrue there is no backend register/login, so ``User.password_hash`` only ever
holds the random secret written by JIT provisioning
(``src/core/user_provisioning.py``: ``get_password_hash(secrets.token_urlsafe(32))``).
No caller could ever supply that secret, so the check could never pass, and
every attempt still burned the caller's own ``chpw_ip`` / ``chpw_email``
rate-limit budget. Password changes belong to Supabase's reset-password email
flow, which already works.

This test fails if the route, the service method, or the unreachable
``EnhancedAuthService`` copy is reintroduced. If a password-change feature is
ever wanted again, it must not be rebuilt on the local ``password_hash``.
"""

from __future__ import annotations

import pytest

from src.api.auth.auth import router as auth_router
from src.security.enhanced_auth import EnhancedAuthService
from src.services.security.auth_service import AuthService


def test_auth_router_exposes_no_change_password_route() -> None:
    paths = {getattr(route, "path", None) for route in auth_router.routes}
    assert "/auth/change-password" not in paths


def test_openapi_schema_has_no_change_password_path() -> None:
    """The published REST contract must not advertise the retired endpoint.

    NOTE: walking ``app.routes`` does NOT work here — this FastAPI version keeps
    ``include_router`` results as lazy ``_IncludedRouter`` entries rather than
    flattening them, so a top-level scan silently finds nothing and would pass
    even with the route present. The generated schema is the honest view.
    """
    from src.main import app

    assert "/api/v1/auth/change-password" not in app.openapi()["paths"]
    assert "PasswordChange" not in app.openapi()["components"]["schemas"]


def test_posting_to_change_password_returns_404(test_app, test_auth_headers) -> None:
    """End-to-end proof: the path is unrouted, not merely unlisted."""
    from contextlib import asynccontextmanager
    from typing import Any, AsyncIterator

    from fastapi.testclient import TestClient

    @asynccontextmanager
    async def _no_lifespan(_app: Any) -> AsyncIterator[None]:
        # pragma: no cover - trivial shim
        yield

    original_lifespan = test_app.router.lifespan_context
    test_app.router.lifespan_context = _no_lifespan
    try:
        with TestClient(test_app, headers=test_auth_headers) as client:
            response = client.post(
                "/api/v1/auth/change-password",
                json={"current_password": "a", "new_password": "b"},
            )
    finally:
        test_app.router.lifespan_context = original_lifespan

    assert response.status_code == 404


@pytest.mark.parametrize("service", [AuthService, EnhancedAuthService])
def test_change_password_service_methods_are_gone(service: type) -> None:
    assert not hasattr(service, "change_password")


def test_password_change_schema_is_gone() -> None:
    import src.api.auth.auth as auth_module

    assert not hasattr(auth_module, "PasswordChange")
