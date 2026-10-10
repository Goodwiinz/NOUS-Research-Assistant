"""GOO-406 (E2): encryption routes are bound to the caller's organization.

Per-org ``system_admin`` plus the legacy per-org ``users.role == "admin"``
used to unlock platform-wide key rotation (no organization_id), cross-tenant
organization-profile writes, and unscoped encryption audit-log reads. Every
route now binds to ``current_user.organization_id``; platform-wide actions
need the PLATFORM_OPERATOR_USER_IDS allowlist (``is_platform_operator``).
"""

import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.security.encryption import router as encryption_router
from src.core.config import settings
from src.core.database import get_db_sync

API = "/api/v1/security/encryption"

_USER_ID = uuid.uuid4()
_ORG_ID = uuid.uuid4()
_OTHER_ORG_ID = uuid.uuid4()

ClientWithDb = Callable[..., TestClient]
SetOperator = Callable[[bool], None]

ROTATION_RESULT: dict[str, Any] = {
    "key_type": "data",
    "old_key_id": "old",
    "new_key_id": "new",
    "rotated_resources": 0,
    "failed_resources": 0,
    "errors": [],
}


def _org_admin() -> MagicMock:
    """Tenant system_admin who is also a legacy users.role admin."""
    user = MagicMock()
    user.id = _USER_ID
    user.organization_id = _ORG_ID
    user.role.value = "admin"
    return user


class _RecordingQuery:
    """Query stand-in that records filter() expressions."""

    def __init__(self, rows: list[Any]) -> None:
        self.filters: list[Any] = []
        self._rows = rows

    def filter(self, *exprs: Any) -> "_RecordingQuery":
        self.filters.extend(exprs)
        return self

    def order_by(self, *args: Any) -> "_RecordingQuery":
        return self

    def offset(self, *args: Any) -> "_RecordingQuery":
        return self

    def limit(self, *args: Any) -> "_RecordingQuery":
        return self

    def all(self) -> list[Any]:
        return self._rows


@pytest.fixture
def operator_allowlist(monkeypatch: pytest.MonkeyPatch) -> SetOperator:
    """Toggle whether _USER_ID is on PLATFORM_OPERATOR_USER_IDS."""

    def set_operator(enabled: bool) -> None:
        monkeypatch.setattr(
            settings, "PLATFORM_OPERATOR_USER_IDS", str(_USER_ID) if enabled else ""
        )

    set_operator(False)
    return set_operator


@pytest.fixture
def client_with_db() -> Iterator[ClientWithDb]:
    """Factory: client_with_db(db) -> TestClient for an org admin holding
    system_admin (route gate passes) with ``db`` as the sync session."""
    from src.core.dependencies import is_active_user
    from src.main import http_exception_handler

    gate = MagicMock()
    gate.user_has_permission.return_value = True
    patches = [
        patch("src.middleware.rbac.RBACService", return_value=gate),
        patch("src.middleware.rbac.get_current_user_id", return_value=str(_USER_ID)),
        patch("src.middleware.rbac.get_current_tenant_id", return_value=str(_ORG_ID)),
    ]
    for p in patches:
        p.start()

    def make(
        db: Optional[MagicMock] = None,
        user: Callable[[], MagicMock] = _org_admin,
    ) -> TestClient:
        app = FastAPI()
        app.include_router(encryption_router, prefix="/api/v1/security")
        app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
        session = db if db is not None else MagicMock()
        app.dependency_overrides[get_db_sync] = lambda: session
        app.dependency_overrides[is_active_user] = user
        return TestClient(app)

    yield make
    for p in patches:
        p.stop()


@pytest.mark.unit
class TestKeyRotationScope:
    def test_org_admin_global_rotation_is_403(
        self, client_with_db: ClientWithDb, operator_allowlist: SetOperator
    ) -> None:
        with patch("src.api.security.encryption.EncryptionService") as svc_cls:
            resp = client_with_db().post(
                f"{API}/keys/rotate", json={"key_type": "data"}
            )
        assert resp.status_code == 403
        svc_cls.return_value.rotate_encryption_keys.assert_not_called()

    def test_org_admin_own_org_real_rotation_is_403(
        self, client_with_db: ClientWithDb, operator_allowlist: SetOperator
    ) -> None:
        """The key is process-global; naming your own org does not scope it."""
        with patch("src.api.security.encryption.EncryptionService") as svc_cls:
            resp = client_with_db().post(
                f"{API}/keys/rotate",
                json={"key_type": "data", "organization_id": str(_ORG_ID)},
            )
        assert resp.status_code == 403
        svc_cls.return_value.rotate_encryption_keys.assert_not_called()

    def test_org_admin_dry_run_is_bound_to_own_org(
        self, client_with_db: ClientWithDb, operator_allowlist: SetOperator
    ) -> None:
        with patch("src.api.security.encryption.EncryptionService") as svc_cls:
            svc_cls.return_value.rotate_encryption_keys.return_value = ROTATION_RESULT
            resp = client_with_db().post(
                f"{API}/keys/rotate", json={"key_type": "data", "dry_run": True}
            )
        assert resp.status_code == 200, resp.text
        kwargs = svc_cls.return_value.rotate_encryption_keys.call_args.kwargs
        assert kwargs["organization_id"] == _ORG_ID

    def test_platform_operator_cannot_rotate_nonpersistent_data_key(
        self, client_with_db: ClientWithDb, operator_allowlist: SetOperator
    ) -> None:
        operator_allowlist(True)
        with patch("src.api.security.encryption.EncryptionService") as svc_cls:
            svc_cls.return_value.rotate_encryption_keys.return_value = ROTATION_RESULT
            resp = client_with_db().post(
                f"{API}/keys/rotate", json={"key_type": "data"}
            )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["message"] == (
            "DATA key rotation is unavailable until versioned keys are durable"
        )
        svc_cls.return_value.rotate_encryption_keys.assert_not_called()

    def test_platform_operator_can_rotate_file_key(
        self, client_with_db: ClientWithDb, operator_allowlist: SetOperator
    ) -> None:
        operator_allowlist(True)
        with patch("src.api.security.encryption.EncryptionService") as svc_cls:
            svc_cls.return_value.rotate_encryption_keys.return_value = {
                **ROTATION_RESULT,
                "key_type": "file",
            }
            resp = client_with_db().post(
                f"{API}/keys/rotate", json={"key_type": "file"}
            )
        assert resp.status_code == 200, resp.text
        kwargs = svc_cls.return_value.rotate_encryption_keys.call_args.kwargs
        assert kwargs["organization_id"] is None


@pytest.mark.unit
class TestOrganizationProfileScope:
    def test_write_other_org_profile_is_403(
        self, client_with_db: ClientWithDb, operator_allowlist: SetOperator
    ) -> None:
        with patch("src.api.security.encryption.EncryptionService") as svc_cls:
            resp = client_with_db().post(
                f"{API}/profiles/organization",
                json={
                    "organization_id": str(_OTHER_ORG_ID),
                    "profile_data": {"dba_name": "pwned"},
                },
            )
        assert resp.status_code == 403
        svc_cls.return_value.encrypt_organization_profile.assert_not_called()

    def test_write_own_org_profile_is_200(
        self, client_with_db: ClientWithDb, operator_allowlist: SetOperator
    ) -> None:
        profile = SimpleNamespace(id="p-1", organization_id=_ORG_ID)
        with patch("src.api.security.encryption.EncryptionService") as svc_cls:
            svc_cls.return_value.encrypt_organization_profile.return_value = profile
            resp = client_with_db().post(
                f"{API}/profiles/organization",
                json={
                    "organization_id": str(_ORG_ID),
                    "profile_data": {"dba_name": "ours"},
                },
            )
        assert resp.status_code == 200, resp.text
        kwargs = svc_cls.return_value.encrypt_organization_profile.call_args.kwargs
        assert kwargs["organization_id"] == _ORG_ID


@pytest.mark.unit
class TestStatusScope:
    def test_other_org_status_is_403(
        self, client_with_db: ClientWithDb, operator_allowlist: SetOperator
    ) -> None:
        with patch("src.api.security.encryption.EncryptionService") as svc_cls:
            resp = client_with_db().get(
                f"{API}/status", params={"organization_id": str(_OTHER_ORG_ID)}
            )
        assert resp.status_code == 403
        svc_cls.return_value.get_encryption_status.assert_not_called()


def _orgless_admin() -> MagicMock:
    """Non-operator caller with no organization context."""
    user = _org_admin()
    user.organization_id = None
    return user


@pytest.mark.unit
class TestMissingOrgContext:
    def test_orgless_status_is_403_not_global(
        self, client_with_db: ClientWithDb, operator_allowlist: SetOperator
    ) -> None:
        with patch("src.api.security.encryption.EncryptionService") as svc_cls:
            resp = client_with_db(user=_orgless_admin).get(f"{API}/status")
        assert resp.status_code == 403
        assert "Organization context required" in resp.text
        svc_cls.return_value.get_encryption_status.assert_not_called()

    def test_orgless_dry_run_rotation_is_403(
        self, client_with_db: ClientWithDb, operator_allowlist: SetOperator
    ) -> None:
        with patch("src.api.security.encryption.EncryptionService") as svc_cls:
            resp = client_with_db(user=_orgless_admin).post(
                f"{API}/keys/rotate", json={"key_type": "data", "dry_run": True}
            )
        assert resp.status_code == 403
        svc_cls.return_value.rotate_encryption_keys.assert_not_called()

    def test_orgless_audit_logs_is_403_not_is_null(
        self, client_with_db: ClientWithDb, operator_allowlist: SetOperator
    ) -> None:
        query = _RecordingQuery([])
        db = MagicMock()
        db.query.return_value = query
        resp = client_with_db(db, user=_orgless_admin).get(f"{API}/audit/logs")
        assert resp.status_code == 403
        assert "Organization context required" in resp.text
        assert query.filters == []


def _log_row() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        operation_type="ENCRYPT",
        resource_type="user_profile",
        resource_id=uuid.uuid4(),
        key_id="k",
        performed_by=_USER_ID,
        organization_id=_ORG_ID,
        ip_address=None,
        user_agent=None,
        success=True,
        error_message=None,
        created_at=datetime.utcnow(),
    )


def _org_filter_values(query: _RecordingQuery) -> list[Any]:
    """Right-hand values of ``EncryptionAuditLog.organization_id == X`` filters."""
    values = []
    for expr in query.filters:
        left = getattr(expr, "left", None)
        if getattr(left, "key", None) == "organization_id":
            values.append(expr.right.value)
    return values


@pytest.mark.unit
class TestAuditLogScope:
    def test_org_admin_sees_only_own_org_rows(
        self, client_with_db: ClientWithDb, operator_allowlist: SetOperator
    ) -> None:
        query = _RecordingQuery([_log_row()])
        db = MagicMock()
        db.query.return_value = query
        resp = client_with_db(db).get(f"{API}/audit/logs")
        assert resp.status_code == 200, resp.text
        assert _org_filter_values(query) == [_ORG_ID]
        assert all(row["organization_id"] == str(_ORG_ID) for row in resp.json())

    def test_platform_operator_reads_unscoped(
        self, client_with_db: ClientWithDb, operator_allowlist: SetOperator
    ) -> None:
        operator_allowlist(True)
        query = _RecordingQuery([_log_row()])
        db = MagicMock()
        db.query.return_value = query
        resp = client_with_db(db).get(f"{API}/audit/logs")
        assert resp.status_code == 200, resp.text
        assert _org_filter_values(query) == []


@pytest.mark.unit
def test_status_recent_operations_are_org_scoped() -> None:
    """get_encryption_status(org) must not list other tenants' audit rows."""
    from src.services.security.encryption_service import EncryptionService

    svc = EncryptionService.__new__(EncryptionService)
    svc.key_manager = MagicMock()
    svc.key_manager.get_active_key.return_value = None
    recent = _RecordingQuery([])
    counts = MagicMock()
    counts.join.return_value.filter.return_value.count.return_value = 0
    counts.filter.return_value.count.return_value = 0
    svc.db = MagicMock()
    svc.db.query.side_effect = [counts, counts, recent]

    svc.get_encryption_status(_ORG_ID)

    assert _org_filter_values(recent) == [_ORG_ID]
