"""GOO-406 (A4): role delegation is bounded by the caller's own authority.

``user_manage_roles`` alone let an RBAC ``admin`` (priority 800) assign itself
``super_admin`` (priority 1000, every permission) or revoke ``super_admin``
from the real super admin. ``RBACService.assert_caller_can_delegate`` now
requires role.permissions to be a subset of the caller's effective
permissions AND role.priority <= the caller's highest role priority, on
assign, revoke, create and update.
"""

from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable, Generator, Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.core.database import get_db_sync
from src.models.permission import Role, UserRoleAssignment
from src.models.user import User
from src.services.security.rbac_service import RBACService, get_rbac_service

API = "/api/v1/rbac/rbac"  # live double prefix (main.py + router prefix)
ORG = "org-1"
ADMIN_ID = "admin-user"
SUPER_ID = "super-user"
TARGET_ID = "target-user"

ADMIN_PERMS = {"user_read", "user_manage_roles", "document_read", "document_create"}
SUPER_PERMS = ADMIN_PERMS | {"system_admin", "organization_delete", "audit_manage"}


def _role(name: str, priority: int, perms: set[str]) -> SimpleNamespace:
    """Role stand-in with the attributes the delegation check reads."""
    return SimpleNamespace(
        id=f"{name}-id",
        name=name,
        priority=priority,
        is_active=True,
        permissions=[SimpleNamespace(name=p, is_active=True) for p in sorted(perms)],
    )


SUPER_ROLE = _role("super_admin", 1000, SUPER_PERMS)
ADMIN_ROLE = _role("admin", 800, ADMIN_PERMS)
EDITOR_ROLE = _role("editor", 600, {"document_read", "document_create"})

ClientFor = Callable[[str, MagicMock], TestClient]

CALLERS = {
    ADMIN_ID: (ADMIN_PERMS, [ADMIN_ROLE]),
    SUPER_ID: (SUPER_PERMS, [SUPER_ROLE]),
}


class _FakeQuery:
    """Query stand-in: ignores filters, returns a preset ``first()``."""

    def __init__(self, result: Any) -> None:
        self._result = result

    def filter(self, *args: Any, **kwargs: Any) -> "_FakeQuery":
        return self

    def first(self) -> Any:
        return self._result


def _fake_db(role: Any, existing_assignment: Any = None) -> MagicMock:
    """Route db.query(entity) to the preset role / member / assignment."""

    def query(entity: Any) -> _FakeQuery:
        target = getattr(entity, "class_", entity)
        if target is Role:
            return _FakeQuery(role)
        if target is User:
            return _FakeQuery((1,))
        if target is UserRoleAssignment:
            return _FakeQuery(existing_assignment)
        raise AssertionError(f"unexpected query entity: {entity!r}")

    db = MagicMock()
    db.query.side_effect = query
    db.refresh.side_effect = lambda obj: setattr(obj, "assigned_at", datetime.utcnow())
    return db


def _client(caller_id: str, db: MagicMock) -> Generator[TestClient, None, None]:
    from src.api.security.rbac_management import router as rbac_router
    from src.main import http_exception_handler

    app = FastAPI()
    app.include_router(rbac_router, prefix="/api/v1/rbac")
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.dependency_overrides[get_db_sync] = lambda: MagicMock()
    app.dependency_overrides[get_rbac_service] = lambda: RBACService(db)

    perms, roles = CALLERS[caller_id]
    gate = MagicMock()
    gate.user_has_permission.return_value = True  # route gate passes
    with (
        patch("src.middleware.rbac.RBACService", return_value=gate),
        patch("src.middleware.rbac.get_current_user_id", return_value=caller_id),
        patch("src.middleware.rbac.get_current_tenant_id", return_value=ORG),
        patch(
            "src.api.security.rbac_management.get_current_user_id",
            return_value=caller_id,
        ),
        patch(
            "src.api.security.rbac_management.get_current_tenant_id",
            return_value=ORG,
        ),
        patch.object(RBACService, "get_user_permissions", return_value=set(perms)),
        patch.object(RBACService, "get_user_roles", return_value=list(roles)),
        patch.object(RBACService, "_audit_role_change"),
    ):
        yield TestClient(app)


@pytest.fixture
def client_for() -> Iterator[ClientFor]:
    """Factory: ``client_for(caller_id, db)`` -> TestClient in that caller's context."""
    gens: list[Generator[TestClient, None, None]] = []

    def make(caller_id: str, db: MagicMock) -> TestClient:
        gen = _client(caller_id, db)
        gens.append(gen)
        return next(gen)

    yield make
    for gen in gens:
        gen.close()


def _assign(client: TestClient, user_id: str, role: SimpleNamespace) -> Any:
    return client.post(
        f"{API}/users/{user_id}/roles",
        json={"user_id": user_id, "role_id": role.id},
    )


@pytest.mark.unit
class TestRoleDelegationBound:
    def test_admin_cannot_self_assign_super_admin(self, client_for: ClientFor) -> None:
        db = _fake_db(SUPER_ROLE)
        resp = _assign(client_for(ADMIN_ID, db), ADMIN_ID, SUPER_ROLE)
        assert resp.status_code == 403
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_admin_can_assign_role_within_own_permissions(
        self, client_for: ClientFor
    ) -> None:
        db = _fake_db(EDITOR_ROLE)
        resp = _assign(client_for(ADMIN_ID, db), TARGET_ID, EDITOR_ROLE)
        assert resp.status_code == 200, resp.text
        assert resp.json()["user_id"] == TARGET_ID
        assert db.add.called and db.commit.called

    def test_admin_cannot_revoke_super_admin(self, client_for: ClientFor) -> None:
        assignment = MagicMock(is_active=True)
        assignment.role = SUPER_ROLE
        db = _fake_db(None, existing_assignment=assignment)
        resp = client_for(ADMIN_ID, db).delete(
            f"{API}/users/{SUPER_ID}/roles/{SUPER_ROLE.id}"
        )
        assert resp.status_code == 403
        assert assignment.is_active is True
        db.commit.assert_not_called()

    def test_super_admin_can_assign_admin(self, client_for: ClientFor) -> None:
        db = _fake_db(ADMIN_ROLE)
        resp = _assign(client_for(SUPER_ID, db), TARGET_ID, ADMIN_ROLE)
        assert resp.status_code == 200, resp.text

    def test_role_create_with_permission_caller_lacks_is_403(
        self, client_for: ClientFor
    ) -> None:
        db = _fake_db(None)
        resp = client_for(ADMIN_ID, db).post(
            f"{API}/roles",
            json={
                "name": "sneaky",
                "display_name": "Sneaky",
                "permission_names": ["document_read", "system_admin"],
                "priority": 100,
            },
        )
        assert resp.status_code == 403
        db.add.assert_not_called()


@pytest.mark.unit
class TestAssertCallerCanDelegate:
    def _svc(self, perms: set[str], roles: list[Any]) -> RBACService:
        svc = RBACService(MagicMock())
        svc.get_user_permissions = MagicMock(return_value=perms)  # type: ignore[method-assign]
        svc.get_user_roles = MagicMock(return_value=roles)  # type: ignore[method-assign]
        return svc

    def test_missing_caller_is_denied(self) -> None:
        from src.exceptions.analytics_exceptions import PermissionDeniedException

        with pytest.raises(PermissionDeniedException):
            self._svc(SUPER_PERMS, [SUPER_ROLE]).assert_caller_can_delegate(
                None, ORG, [], 0
            )

    def test_caller_without_roles_is_denied(self) -> None:
        from src.exceptions.analytics_exceptions import PermissionDeniedException

        with pytest.raises(PermissionDeniedException):
            self._svc(set(), []).assert_caller_can_delegate(ADMIN_ID, ORG, [], 0)

    def test_higher_priority_with_subset_permissions_is_denied(self) -> None:
        from src.exceptions.analytics_exceptions import PermissionDeniedException

        with pytest.raises(PermissionDeniedException):
            self._svc(ADMIN_PERMS, [ADMIN_ROLE]).assert_caller_can_delegate(
                ADMIN_ID, ORG, ["document_read"], 900
            )

    def test_equal_role_is_allowed(self) -> None:
        self._svc(ADMIN_PERMS, [ADMIN_ROLE]).assert_caller_can_delegate(
            ADMIN_ID, ORG, sorted(ADMIN_PERMS), 800
        )
