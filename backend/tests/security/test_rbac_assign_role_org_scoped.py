"""Regression: assign_role_to_user is scoped to the target user's org.

assign_role_to_user validated that the *role* belonged to organization_id but
never checked that the target user_id was a member of that org. Because the
user_id is path-controlled (POST /users/{user_id}/roles), a caller could grant
a role to a user in another tenant. The fix verifies the target user belongs to
organization_id before creating/updating the assignment.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.exceptions.analytics_exceptions import ConfigurationException
from src.models.permission import Role, UserRoleAssignment
from src.models.user import User
from src.services.security.rbac_service import RBACService


@pytest.fixture(autouse=True)
def _delegation_check_passes():
    """These tests pin org scoping and audit emission; the GOO-406 delegation
    bound is covered in test_rbac_delegation_bound.py."""
    with patch.object(RBACService, "assert_caller_can_delegate"):
        yield


class _FakeQuery:
    """Minimal stand-in for a SQLAlchemy Query that ignores filters and
    returns a preset ``first()`` result."""

    def __init__(self, result):
        """Store the value ``first()`` should return."""
        self._result = result

    def filter(self, *args, **kwargs):
        """Ignore filter args; return self for chaining."""
        return self

    def first(self):
        """Return the preset result."""
        return self._result


def _make_db(*, role, user_in_org, existing_assignment=None):
    """Route db.query(...) by the queried entity.

    Role -> role validation; User -> membership check;
    UserRoleAssignment -> existing-assignment lookup.
    """

    def query(entity):
        """Resolve the queried entity to the matching preset result."""
        # db.query(User.id) passes a column; resolve to its parent class.
        target = getattr(entity, "class_", entity)
        if target is Role:
            return _FakeQuery(role)
        if target is User:
            return _FakeQuery(user_in_org)
        if target is UserRoleAssignment:
            return _FakeQuery(existing_assignment)
        raise AssertionError(f"unexpected query entity: {entity!r}")

    db = MagicMock()
    db.query.side_effect = query
    return db


@pytest.mark.unit
def test_assign_rejects_user_outside_org():
    """A target user not in the org is rejected before any write."""
    role = MagicMock(name="role", name_attr="r")
    db = _make_db(role=role, user_in_org=None)
    svc = RBACService(db)

    with pytest.raises(ConfigurationException):
        svc.assign_role_to_user(
            user_id="user-other-org",
            role_id="role-1",
            organization_id="org-1",
        )

    db.add.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.unit
def test_assign_creates_for_member():
    """A target user in the org gets the assignment created and committed."""
    role = MagicMock()
    role.name = "editor"
    db = _make_db(role=role, user_in_org=(1,), existing_assignment=None)
    svc = RBACService(db)

    assignment = svc.assign_role_to_user(
        user_id="user-1",
        role_id="role-1",
        organization_id="org-1",
    )

    assert isinstance(assignment, UserRoleAssignment)
    assert assignment.user_id == "user-1"
    assert assignment.organization_id == "org-1"
    # The role assignment is added + committed. (A second add/commit now also
    # occurs for the best-effort audit event via AuditService on the same mock
    # session, so assert "called" rather than "called once".)
    assert db.add.called
    assert db.commit.called


@pytest.mark.unit
def test_assign_emits_security_audit_event():
    """A successful role grant writes a security audit event (privilege change)."""
    role = MagicMock()
    role.name = "editor"
    db = _make_db(role=role, user_in_org=(1,), existing_assignment=None)
    svc = RBACService(db)

    with patch("src.services.security.audit_service.AuditService") as audit_cls:
        svc.assign_role_to_user(
            user_id="user-1", role_id="role-1", organization_id="org-1"
        )

    audit_cls.assert_called_once_with(db)
    audit_cls.return_value.log_security_event.assert_called_once()
    kwargs = audit_cls.return_value.log_security_event.call_args.kwargs
    assert kwargs["event_type"] == "role_assigned"
    assert kwargs["organization_id"] == "org-1"


@pytest.mark.unit
def test_audit_failure_does_not_break_assignment():
    """An audit write failure must not fail the (already-committed) role grant."""
    role = MagicMock()
    role.name = "editor"
    db = _make_db(role=role, user_in_org=(1,), existing_assignment=None)
    svc = RBACService(db)

    with patch(
        "src.services.security.audit_service.AuditService",
        side_effect=RuntimeError("audit down"),
    ):
        assignment = svc.assign_role_to_user(
            user_id="user-1", role_id="role-1", organization_id="org-1"
        )

    assert isinstance(assignment, UserRoleAssignment)


@pytest.mark.unit
def test_audit_log_call_failure_swallowed():
    """A failure inside log_security_event (not just construction) is swallowed."""
    role = MagicMock()
    role.name = "editor"
    db = _make_db(role=role, user_in_org=(1,), existing_assignment=None)
    svc = RBACService(db)

    with patch("src.services.security.audit_service.AuditService") as audit_cls:
        audit_cls.return_value.log_security_event.side_effect = RuntimeError("db err")
        assignment = svc.assign_role_to_user(
            user_id="user-1", role_id="role-1", organization_id="org-1"
        )

    assert isinstance(assignment, UserRoleAssignment)


@pytest.mark.unit
def test_revoke_emits_role_revoked_audit():
    """Revoking a role writes a role_revoked security event."""
    existing = MagicMock(is_active=True)
    db = _make_db(role=None, user_in_org=None, existing_assignment=existing)
    svc = RBACService(db)

    with patch("src.services.security.audit_service.AuditService") as audit_cls:
        result = svc.revoke_role_from_user(
            user_id="user-1", role_id="role-1", organization_id="org-1"
        )

    assert result is True
    audit_cls.return_value.log_security_event.assert_called_once()
    kwargs = audit_cls.return_value.log_security_event.call_args.kwargs
    assert kwargs["event_type"] == "role_revoked"
