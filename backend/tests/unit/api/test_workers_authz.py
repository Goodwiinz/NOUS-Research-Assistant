"""Security regressions for the platform-only ``/workers/*`` surface.

Tenant roles, including ``ADMIN``, must not grant process-wide worker control.
Only explicitly configured platform operators may inspect or mutate this
control plane.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

pytestmark = pytest.mark.unit


def _routes() -> dict[str, APIRoute]:
    from src.api.infrastructure.workers import router

    return {r.path: r for r in router.routes if isinstance(r, APIRoute)}


def test_all_worker_routes_require_platform_operator() -> None:
    """Every worker route must use the platform operator boundary."""
    from src.core import dependencies as dependency_module

    require_platform_operator = getattr(
        dependency_module, "require_platform_operator", None
    )
    assert require_platform_operator is not None, (
        "the worker control plane must expose a dedicated platform-operator "
        "dependency"
    )

    routes = _routes()
    assert routes, "workers router has no routes — path likely changed"

    ungated = []
    for path, route in routes.items():
        calls = [d.call for d in route.dependant.dependencies]
        if require_platform_operator not in calls:
            ungated.append(path)

    assert ungated == [], f"routes missing require_platform_operator: {ungated}"


def test_worker_routes_no_longer_use_tenant_admin() -> None:
    """A tenant administrator is not a process/platform operator."""
    from src.core import dependencies as dependency_module

    require_admin = dependency_module.require_admin

    routes = _routes()
    for path, route in routes.items():
        calls = [d.call for d in routes[path].dependant.dependencies]
        assert require_admin not in calls, f"{path} still trusts tenant ADMIN"


def test_require_admin_rejects_non_admin_caller() -> None:
    """Sanity: the require_admin dependency itself 403s a non-admin — the
    piece the router-introspection tests above assume is wired correctly."""
    from src.core.dependencies import require_role
    from src.models.user import UserRole

    checker = require_role(UserRole.ADMIN)
    non_admin = Mock()
    non_admin.has_permission = Mock(return_value=False)

    with pytest.raises(HTTPException) as exc:
        checker(current_user=non_admin)

    assert exc.value.status_code == 403


def test_platform_operator_rejects_tenant_admin_even_when_role_is_admin(
    monkeypatch,
) -> None:
    """An ADMIN from a tenant without an allowlisted UUID must be denied."""
    from src.core import dependencies as dependency_module

    checker = getattr(dependency_module, "require_platform_operator", None)
    assert checker is not None, "platform operator dependency is not implemented"
    tenant_admin = SimpleNamespace(id=uuid4(), role="admin")
    monkeypatch.setattr(dependency_module.settings, "PLATFORM_OPERATOR_USER_IDS", "")

    with pytest.raises(HTTPException) as exc:
        checker(current_user=tenant_admin)

    assert exc.value.status_code == 403


def test_platform_operator_allows_configured_uuid_regardless_of_tenant_role(
    monkeypatch,
) -> None:
    """An explicitly configured UUID is the only platform authority."""
    from src.core import dependencies as dependency_module

    checker = getattr(dependency_module, "require_platform_operator", None)
    assert checker is not None, "platform operator dependency is not implemented"
    operator = SimpleNamespace(id=uuid4(), role="user")
    monkeypatch.setattr(
        dependency_module.settings,
        "PLATFORM_OPERATOR_USER_IDS",
        str(operator.id),
    )

    assert checker(current_user=operator) is operator


def test_platform_operator_malformed_allowlist_fails_closed(monkeypatch) -> None:
    """A malformed allowlist must never broaden platform access."""
    from src.core import dependencies as dependency_module

    checker = getattr(dependency_module, "require_platform_operator", None)
    assert checker is not None, "platform operator dependency is not implemented"
    operator = SimpleNamespace(id=uuid4(), role="user")
    monkeypatch.setattr(
        dependency_module.settings,
        "PLATFORM_OPERATOR_USER_IDS",
        f"{operator.id},definitely-not-a-uuid",
    )

    with pytest.raises(HTTPException) as exc:
        checker(current_user=operator)

    assert exc.value.status_code == 403
