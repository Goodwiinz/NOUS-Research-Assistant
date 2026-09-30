"""I12 regression: encryption endpoints must be guarded by the canonical RBAC
dependency, not the analytics decorator.

The analytics ``require_permission`` decorator (src/auth/rbac_decorator.py)
expects an ``AnalyticsPermission`` enum; passing ``["encryption:manage"]``
made ``permission in role_permissions.permissions`` (analytics_permissions.py)
raise ``TypeError: unhashable type: 'list'`` → 500 for EVERY authenticated
caller. The endpoints now use ``require_permission_dep("system_admin")`` from
src/middleware/rbac.py — the same dependency the compliance and RBAC-management
routers already use. ``system_admin`` is substituted because no ``encryption:*``
permission exists in SYSTEM_PERMISSIONS (src/models/permission.py).
"""

import re
import uuid
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.security.encryption import router as encryption_router
from src.core.database import get_db, get_db_sync

API_PREFIX = "/api/v1/security/encryption"

_USER_ID = uuid.uuid4()
_ORG_ID = uuid.uuid4()

POST_BODY = {
    "user_id": str(_USER_ID),
    "profile_data": {"job_title": "Engineer"},
}

STATUS_PAYLOAD: dict[str, Any] = {
    "key_management": {},
    "encrypted_resources": {},
    "recent_operations": [],
}


def _fake_user(role: str = "admin") -> MagicMock:
    user = MagicMock()
    user.id = _USER_ID
    user.organization_id = _ORG_ID
    user.role.value = role
    return user


def _build_app(authed_user: MagicMock | None = None) -> FastAPI:
    # Reuse the repo's HTTPException handler so status-code/envelope assertions
    # match production behavior ({"error": {...}}), not FastAPI's default.
    from src.core.dependencies import is_active_user
    from src.main import http_exception_handler

    app = FastAPI()
    app.include_router(encryption_router, prefix="/api/v1/security")
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]

    def _fake_db() -> Iterator[MagicMock]:
        yield MagicMock()

    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_db_sync] = _fake_db
    if authed_user is not None:
        app.dependency_overrides[is_active_user] = lambda: authed_user
    return app


def _tenant_ctx() -> tuple[Any, Any]:
    """The guard reads tenant ContextVars set by MultiTenancyMiddleware in the
    real app; the mini test app has no middleware, so patch the accessors."""
    import src.middleware.rbac as rbac_mod

    return (
        patch.object(rbac_mod, "get_current_user_id", return_value=str(_USER_ID)),
        patch.object(rbac_mod, "get_current_tenant_id", return_value=str(_ORG_ID)),
    )


@pytest.mark.unit
class TestEncryptionEndpointGuards:
    def test_unauthenticated_get_status_returns_401(self) -> None:
        resp = TestClient(_build_app()).get(f"{API_PREFIX}/status")
        assert resp.status_code == 401

    def test_unauthenticated_post_user_profile_returns_401(self) -> None:
        resp = TestClient(_build_app()).post(
            f"{API_PREFIX}/profiles/user", json=POST_BODY
        )
        assert resp.status_code == 401

    def test_get_status_without_permission_is_403_envelope_not_500(self) -> None:
        """The old analytics decorator turned a missing permission into
        TypeError → 500. It must be a repo-envelope 403 instead."""
        svc = MagicMock()
        svc.user_has_permission.return_value = False
        app = _build_app(authed_user=_fake_user())
        ctx = _tenant_ctx()
        with ctx[0], ctx[1], patch("src.middleware.rbac.RBACService", return_value=svc):
            resp = TestClient(app).get(f"{API_PREFIX}/status")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["status_code"] == 403
        assert body["error"]["type"] == "http_error"
        assert "system_admin" in body["error"]["message"]
        svc.user_has_permission.assert_called_once_with(
            str(_USER_ID), "system_admin", str(_ORG_ID)
        )

    def test_post_user_profile_without_permission_is_403_envelope_not_500(self) -> None:
        svc = MagicMock()
        svc.user_has_permission.return_value = False
        app = _build_app(authed_user=_fake_user())
        ctx = _tenant_ctx()
        with ctx[0], ctx[1], patch("src.middleware.rbac.RBACService", return_value=svc):
            resp = TestClient(app).post(f"{API_PREFIX}/profiles/user", json=POST_BODY)
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["status_code"] == 403
        assert body["error"]["type"] == "http_error"
        assert "system_admin" in body["error"]["message"]

    def test_get_status_with_permission_returns_200(self) -> None:
        svc = MagicMock()
        svc.user_has_permission.return_value = True
        app = _build_app(authed_user=_fake_user())
        ctx = _tenant_ctx()
        with (
            ctx[0],
            ctx[1],
            patch("src.middleware.rbac.RBACService", return_value=svc),
            patch("src.api.security.encryption.EncryptionService") as enc_svc_cls,
        ):
            enc_svc_cls.return_value.get_encryption_status.return_value = STATUS_PAYLOAD
            resp = TestClient(app).get(f"{API_PREFIX}/status")
        assert resp.status_code == 200
        assert resp.json()["key_management"] == {}

    def test_post_user_profile_with_permission_returns_200(self) -> None:
        svc = MagicMock()
        svc.user_has_permission.return_value = True
        profile = MagicMock()
        profile.id = "profile-1"
        profile.user_id = _USER_ID
        app = _build_app(authed_user=_fake_user())
        ctx = _tenant_ctx()
        with (
            ctx[0],
            ctx[1],
            patch("src.middleware.rbac.RBACService", return_value=svc),
            patch("src.api.security.encryption.EncryptionService") as enc_svc_cls,
        ):
            enc_svc_cls.return_value.encrypt_user_profile.return_value = profile
            resp = TestClient(app).post(f"{API_PREFIX}/profiles/user", json=POST_BODY)
        assert resp.status_code == 200
        assert resp.json()["profile_id"] == "profile-1"


@pytest.mark.unit
class TestEncryptionRouteWiring:
    def test_routes_inject_sync_session_not_async_get_db(self) -> None:
        """EncryptionService and the route bodies use the sync ORM API
        (.query/.commit/.rollback). Async ``get_db`` yields an AsyncSession
        with no ``.query`` → 500 for every caller the guard lets through."""
        from fastapi.routing import APIRoute

        db_deps = {
            route.path: sub.call
            for route in encryption_router.routes
            if isinstance(route, APIRoute)
            for sub in route.dependant.dependencies
            if sub.name == "db"
        }
        assert len(db_deps) == 7
        assert {p: c for p, c in db_deps.items() if c is not get_db_sync} == {}


@pytest.mark.unit
class TestDeletedGuardSources:
    """Tripwire: the analytics RBAC decorator module and the dead symbols of
    middleware/rbac.py were removed in I12 — nothing may import them again."""

    def test_no_module_imports_rbac_decorator(self) -> None:
        src_root = Path(__file__).resolve().parents[2] / "src"
        import_line = re.compile(r"^\s*(?:from|import)\s+\S*rbac_decorator\b", re.M)
        offenders = []
        for p in src_root.rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            if import_line.search(text) or "src.auth.rbac_decorator" in text:
                offenders.append(str(p.relative_to(src_root)))
        assert offenders == []

    def test_middleware_rbac_imports_request_only_the_live_dependency(self) -> None:
        src_root = Path(__file__).resolve().parents[2] / "src"
        offenders = []
        for p in src_root.rglob("*.py"):
            for line in p.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped.startswith("from src.middleware.rbac import"):
                    continue
                names = {n.strip() for n in stripped.split("import", 1)[1].split(",")}
                if names != {"require_permission_dep"}:
                    offenders.append(f"{p.relative_to(src_root)}: {stripped}")
        assert offenders == []

    def test_dead_rbac_symbols_are_gone(self) -> None:
        import src.middleware.rbac as rbac_mod

        for dead in (
            "RBACMiddleware",
            "require_permission",
            "require_any_permission",
            "require_all_permissions",
            "require_role",
            "require_any_role",
            "has_permission",
            "has_any_permission",
            "has_role",
            "get_current_user_permissions",
            "get_current_user_roles",
            "clear_permission_cache",
            "clear_user_permission_cache",
        ):
            assert not hasattr(rbac_mod, dead), f"dead symbol resurrected: {dead}"
