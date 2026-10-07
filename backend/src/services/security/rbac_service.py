"""
Role-Based Access Control (RBAC) service
Manages roles, permissions, and user assignments for fine-grained access control
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set

from fastapi import Depends
from sqlalchemy import and_, exists, func, or_
from sqlalchemy.orm import Session, joinedload

from src.core.database import get_db_sync
from src.exceptions.analytics_exceptions import (
    ConfigurationException,
    PermissionDeniedException,
)
from src.middleware.multi_tenancy import get_current_tenant_id, get_current_user_id
from src.models.organization import Organization
from src.models.permission import (
    SYSTEM_PERMISSIONS,
    SYSTEM_ROLES,
    Permission,
    PermissionCategory,
    PermissionScope,
    Role,
    UserRoleAssignment,
    role_permissions,
)
from src.models.user import User

logger = logging.getLogger(__name__)


class RBACService:
    """Service for managing RBAC operations"""

    def __init__(self, db: Session = None):
        self.db = db

    def initialize_system_permissions(self) -> bool:
        """Initialize system permissions if they don't exist"""
        try:
            existing_count = (
                self.db.query(Permission).filter(Permission.is_system == True).count()
            )
            if existing_count > 0:
                logger.info(
                    f"System permissions already initialized ({existing_count} found)"
                )
                return True

            for perm_data in SYSTEM_PERMISSIONS:
                permission = Permission(
                    name=perm_data[0],
                    display_name=perm_data[1],
                    description=perm_data[2],
                    category=perm_data[3],
                    scope=perm_data[4],
                    is_system=True,
                    is_active=True,
                )
                self.db.add(permission)

            self.db.commit()
            logger.info(f"Initialized {len(SYSTEM_PERMISSIONS)} system permissions")
            return True

        except Exception as e:
            self.db.rollback()
            logger.error(f"Failed to initialize system permissions: {e}")
            return False

    def initialize_system_roles(self, organization_id: str) -> bool:
        """Initialize system roles for an organization"""
        try:
            # Get system permissions
            system_permissions = {
                perm.name: perm
                for perm in self.db.query(Permission)
                .filter(Permission.is_system == True)
                .all()
            }

            # Define role-permission mappings
            role_permission_mappings = {
                "super_admin": list(system_permissions.keys()),  # All permissions
                "admin": [
                    "organization_read",
                    "organization_update",
                    "user_read",
                    "user_create",
                    "user_update",
                    "user_manage_roles",
                    "document_read",
                    "document_create",
                    "document_update",
                    "document_delete",
                    "document_share",
                    "analytics_read",
                    "analytics_export",
                    "analytics_manage",
                    "api_read",
                    "api_write",
                    "api_delete",
                    "billing_read",
                    "billing_manage",
                    "audit_read",
                ],
                "content_manager": [
                    "organization_read",
                    "user_read",
                    "document_read",
                    "document_create",
                    "document_update",
                    "document_share",
                    "analytics_read",
                ],
                "analyst": [
                    "organization_read",
                    "user_read",
                    "document_read",
                    "analytics_read",
                    "analytics_export",
                ],
                "user": [
                    "organization_read",
                    "document_read",
                    "document_create",
                    "analytics_read",
                ],
                "viewer": ["organization_read", "document_read", "analytics_read"],
            }

            for role_data in SYSTEM_ROLES:
                role_name, display_name, description, priority = role_data

                # Check if role already exists for this organization
                existing_role = (
                    self.db.query(Role)
                    .filter(
                        and_(
                            Role.name == role_name,
                            Role.organization_id == organization_id,
                        )
                    )
                    .first()
                )

                if existing_role:
                    logger.debug(
                        f"Role {role_name} already exists for organization {organization_id}"
                    )
                    continue

                # Create new role
                role = Role(
                    name=role_name,
                    display_name=display_name,
                    description=description,
                    organization_id=organization_id,
                    is_system=True,
                    priority=priority,
                    is_active=True,
                )
                self.db.add(role)
                self.db.flush()  # Get the role ID

                # Assign permissions to role
                permission_names = role_permission_mappings.get(role_name, [])
                for perm_name in permission_names:
                    if perm_name in system_permissions:
                        permission = system_permissions[perm_name]
                        # Add permission to role
                        stmt = role_permissions.insert().values(
                            role_id=role.id,
                            permission_id=permission.id,
                            granted_at=datetime.utcnow(),
                        )
                        self.db.execute(stmt)

            self.db.commit()
            logger.info(f"Initialized system roles for organization {organization_id}")
            return True

        except Exception as e:
            self.db.rollback()
            logger.error(
                f"Failed to initialize system roles for organization {organization_id}: {e}"
            )
            return False

    def create_role(
        self,
        organization_id: str,
        name: str,
        display_name: str,
        description: str = None,
        permission_names: List[str] = None,
        priority: int = 0,
        created_by: Optional[str] = None,
    ) -> Role:
        """Create a new custom role for an organization"""
        try:
            # GOO-406 (A4): a caller may only mint a role it could itself hold.
            self.assert_caller_can_delegate(
                created_by, organization_id, permission_names or [], priority
            )

            # Check if role name already exists for this organization
            existing_role = (
                self.db.query(Role)
                .filter(
                    and_(Role.name == name, Role.organization_id == organization_id)
                )
                .first()
            )

            if existing_role:
                raise ConfigurationException(
                    config_key="role_name",
                    config_value=name,
                    reason="Role name already exists for this organization",
                )

            # Create new role
            role = Role(
                name=name,
                display_name=display_name,
                description=description,
                organization_id=organization_id,
                is_system=False,
                priority=priority,
                is_active=True,
            )
            self.db.add(role)
            self.db.flush()

            # Assign permissions if provided
            if permission_names:
                self.assign_permissions_to_role(role.id, permission_names)

            self.db.commit()
            logger.info(f"Created role {name} for organization {organization_id}")
            return role

        except PermissionDeniedException:
            # GOO-406: an expected delegation denial (403), not a server error.
            self.db.rollback()
            raise
        except Exception as e:
            self.db.rollback()
            logger.error(f"Failed to create role {name}: {e}")
            raise

    def assign_permissions_to_role(
        self, role_id: str, permission_names: List[str]
    ) -> bool:
        """Assign permissions to a role"""
        try:
            role = self.db.query(Role).filter(Role.id == role_id).first()
            if not role:
                raise ConfigurationException(
                    config_key="role_id", config_value=role_id, reason="Role not found"
                )

            # Get permissions
            permissions = (
                self.db.query(Permission)
                .filter(
                    and_(
                        Permission.name.in_(permission_names),
                        Permission.is_active == True,
                    )
                )
                .all()
            )

            if len(permissions) != len(permission_names):
                found_names = {perm.name for perm in permissions}
                missing_names = set(permission_names) - found_names
                raise ConfigurationException(
                    config_key="permissions",
                    config_value=",".join(missing_names),
                    reason="One or more permissions not found or inactive",
                )

            # Assign permissions
            for permission in permissions:
                # Check if already assigned
                existing = self.db.execute(
                    role_permissions.select().where(
                        and_(
                            role_permissions.c.role_id == role_id,
                            role_permissions.c.permission_id == permission.id,
                        )
                    )
                ).fetchone()

                if not existing:
                    stmt = role_permissions.insert().values(
                        role_id=role_id,
                        permission_id=permission.id,
                        granted_at=datetime.utcnow(),
                    )
                    self.db.execute(stmt)

            self.db.commit()
            logger.info(f"Assigned {len(permissions)} permissions to role {role.name}")
            return True

        except Exception as e:
            self.db.rollback()
            logger.error(f"Failed to assign permissions to role {role_id}: {e}")
            raise

    def assign_role_to_user(
        self,
        user_id: str,
        role_id: str,
        organization_id: str,
        assigned_by: str = None,
        expires_at: datetime = None,
    ) -> UserRoleAssignment:
        """Assign a role to a user within an organization"""
        try:
            # Validate role belongs to organization
            role = (
                self.db.query(Role)
                .filter(
                    and_(
                        Role.id == role_id,
                        Role.organization_id == organization_id,
                        Role.is_active == True,
                    )
                )
                .first()
            )

            if not role:
                raise ConfigurationException(
                    config_key="role_id",
                    config_value=role_id,
                    reason="Role not found or inactive for this organization",
                )

            # GOO-406 (A4): the grantor's own authority bounds what it can grant.
            self.assert_caller_can_delegate(
                assigned_by,
                organization_id,
                [p.name for p in role.permissions if p.is_active],
                role.priority,
            )

            # Validate the target user is a member of this organization. Without
            # this check a caller could grant a role to a user in another tenant
            # (the user_id is path-controlled), since only the role's org was
            # being verified above.
            user_in_org = (
                self.db.query(User.id)
                .filter(
                    and_(
                        User.id == user_id,
                        User.organization_id == organization_id,
                    )
                )
                .first()
            )

            if not user_in_org:
                raise ConfigurationException(
                    config_key="user_id",
                    config_value=user_id,
                    reason="User is not a member of this organization",
                )

            # Check if assignment already exists
            existing_assignment = (
                self.db.query(UserRoleAssignment)
                .filter(
                    and_(
                        UserRoleAssignment.user_id == user_id,
                        UserRoleAssignment.role_id == role_id,
                        UserRoleAssignment.organization_id == organization_id,
                        UserRoleAssignment.is_active == True,
                    )
                )
                .first()
            )

            if existing_assignment:
                # Update existing assignment
                existing_assignment.assigned_by = assigned_by
                existing_assignment.assigned_at = datetime.utcnow()
                existing_assignment.expires_at = expires_at
                existing_assignment.is_active = True
                assignment = existing_assignment
            else:
                # Create new assignment
                assignment = UserRoleAssignment(
                    user_id=user_id,
                    role_id=role_id,
                    organization_id=organization_id,
                    assigned_by=assigned_by,
                    expires_at=expires_at,
                    is_active=True,
                )
                self.db.add(assignment)

            self.db.commit()
            logger.info(
                f"Assigned role {role.name} to user {user_id} in organization {organization_id}"
            )
            self._audit_role_change(
                "role_assigned", user_id, role_id, organization_id, assigned_by
            )
            return assignment

        except PermissionDeniedException:
            # GOO-406: an expected delegation denial (403), not a server error.
            self.db.rollback()
            raise
        except Exception as e:
            self.db.rollback()
            logger.error(f"Failed to assign role {role_id} to user {user_id}: {e}")
            raise

    def revoke_role_from_user(
        self,
        user_id: str,
        role_id: str,
        organization_id: str,
        revoked_by: Optional[str] = None,
    ) -> bool:
        """Revoke a role from a user"""
        try:
            assignment = (
                self.db.query(UserRoleAssignment)
                .filter(
                    and_(
                        UserRoleAssignment.user_id == user_id,
                        UserRoleAssignment.role_id == role_id,
                        UserRoleAssignment.organization_id == organization_id,
                        UserRoleAssignment.is_active == True,
                    )
                )
                .first()
            )

            if assignment:
                # GOO-406 (A4): a caller cannot strip a role it could not grant.
                role = assignment.role
                self.assert_caller_can_delegate(
                    revoked_by,
                    organization_id,
                    [p.name for p in role.permissions if p.is_active] if role else [],
                    role.priority if role else 0,
                )
                assignment.is_active = False
                self.db.commit()
                logger.info(
                    f"Revoked role {role_id} from user {user_id} in organization {organization_id}"
                )
                self._audit_role_change(
                    "role_revoked", user_id, role_id, organization_id, revoked_by
                )
                return True

            return False

        except PermissionDeniedException:
            # GOO-406: an expected delegation denial (403), not a server error.
            self.db.rollback()
            raise
        except Exception as e:
            self.db.rollback()
            logger.error(f"Failed to revoke role {role_id} from user {user_id}: {e}")
            raise

    def assert_caller_can_delegate(
        self,
        caller_id: Optional[str],
        organization_id: str,
        permission_names: Iterable[str],
        priority: Optional[int],
    ) -> None:
        """Refuse to delegate authority the caller does not itself hold.

        GOO-406 (A4): ``user_manage_roles`` alone let an RBAC ``admin`` grant
        itself ``super_admin`` (or revoke it from the real super admin). A
        caller may grant, revoke, create or edit a role only when
        (a) every permission on that role is in the caller's own effective
        permission set for this organization, and
        (b) the role's priority is <= the caller's highest active role priority.
        Fails closed: no caller id, no roles, or a lookup error (which
        ``get_user_permissions``/``get_user_roles`` turn into empty results)
        all deny.
        """
        if not caller_id:
            raise PermissionDeniedException(
                required_permission="role_delegation",
                user_role="unknown",
                details={"reason": "Caller identity is required to delegate roles"},
            )

        caller_permissions = self.get_user_permissions(caller_id, organization_id)
        caller_roles = self.get_user_roles(caller_id, organization_id)
        caller_priority = max((r.priority or 0 for r in caller_roles), default=None)
        missing = sorted(set(permission_names) - caller_permissions)
        requested_priority = priority or 0

        if caller_priority is None or missing or requested_priority > caller_priority:
            raise PermissionDeniedException(
                required_permission=",".join(missing) or "role_priority",
                user_role=caller_roles[0].name if caller_roles else "none",
                details={
                    "reason": "Role exceeds the caller's own authority",
                    "missing_permissions": missing,
                    "role_priority": requested_priority,
                    "caller_priority": caller_priority,
                },
            )

    def _audit_role_change(
        self,
        action: str,
        target_user_id: str,
        role_id: str,
        organization_id: str,
        actor_id: Optional[str],
    ) -> None:
        """Best-effort security audit of a privilege change. Never let an audit
        failure break the role mutation (which is already committed).

        Trade-off (intentional): the audit write is a SEPARATE transaction from
        the role change — availability of the privilege operation is prioritized
        over a guaranteed audit row, so under a DB error the change can persist
        with only a logged warning. If the audit trail must be compliance-grade
        (atomic with the mutation), move the audit add() before the single
        commit in the caller instead."""
        try:
            from src.services.security.audit_service import AuditService

            AuditService(self.db).log_security_event(
                event_type=action,
                description=(
                    f"{action}: role {role_id} for user {target_user_id} "
                    f"in org {organization_id}"
                ),
                organization_id=organization_id,
                user_id=actor_id,  # the admin performing the change
                details={
                    "action": action,
                    "target_user_id": str(target_user_id),
                    "role_id": str(role_id),
                },
            )
        except Exception as e:  # noqa: BLE001 - audit must not break the op
            logger.warning("Failed to write RBAC audit event (%s): %s", action, e)

    def get_user_permissions(
        self, user_id: str, organization_id: str = None
    ) -> Set[str]:
        """Get all permissions for a user in an organization"""
        try:
            if not organization_id:
                organization_id = get_current_tenant_id()

            if not organization_id:
                return set()

            # Get active role assignments for user in organization
            assignments = (
                self.db.query(UserRoleAssignment)
                .options(
                    joinedload(UserRoleAssignment.role).joinedload(Role.permissions)
                )
                .filter(
                    and_(
                        UserRoleAssignment.user_id == user_id,
                        UserRoleAssignment.organization_id == organization_id,
                        UserRoleAssignment.is_active == True,
                        or_(
                            UserRoleAssignment.expires_at.is_(None),
                            UserRoleAssignment.expires_at > datetime.utcnow(),
                        ),
                    )
                )
                .all()
            )

            # Collect all permissions from assigned roles
            permissions = set()
            for assignment in assignments:
                if assignment.role and assignment.role.is_active:
                    for permission in assignment.role.permissions:
                        if permission.is_active:
                            permissions.add(permission.name)

            return permissions

        except Exception as e:
            logger.error(f"Failed to get permissions for user {user_id}: {e}")
            return set()

    def user_has_permission(
        self, user_id: str, permission_name: str, organization_id: str = None
    ) -> bool:
        """Check if user has specific permission"""
        permissions = self.get_user_permissions(user_id, organization_id)
        return permission_name in permissions

    def user_has_any_permission(
        self, user_id: str, permission_names: List[str], organization_id: str = None
    ) -> bool:
        """Check if user has any of the specified permissions"""
        permissions = self.get_user_permissions(user_id, organization_id)
        return any(perm in permissions for perm in permission_names)

    def user_has_all_permissions(
        self, user_id: str, permission_names: List[str], organization_id: str = None
    ) -> bool:
        """Check if user has all of the specified permissions"""
        permissions = self.get_user_permissions(user_id, organization_id)
        return all(perm in permissions for perm in permission_names)

    def get_user_roles(self, user_id: str, organization_id: str = None) -> List[Role]:
        """Get active roles for a user in an organization"""
        try:
            if not organization_id:
                organization_id = get_current_tenant_id()

            if not organization_id:
                return []

            assignments = (
                self.db.query(UserRoleAssignment)
                .options(joinedload(UserRoleAssignment.role))
                .filter(
                    and_(
                        UserRoleAssignment.user_id == user_id,
                        UserRoleAssignment.organization_id == organization_id,
                        UserRoleAssignment.is_active == True,
                        or_(
                            UserRoleAssignment.expires_at.is_(None),
                            UserRoleAssignment.expires_at > datetime.utcnow(),
                        ),
                    )
                )
                .all()
            )

            roles = []
            for assignment in assignments:
                if assignment.role and assignment.role.is_active:
                    roles.append(assignment.role)

            # Sort by priority (highest first)
            roles.sort(key=lambda r: r.priority, reverse=True)
            return roles

        except Exception as e:
            logger.error(f"Failed to get roles for user {user_id}: {e}")
            return []

    def get_organization_roles(
        self,
        organization_id: str,
        include_system: bool = True,
        include_custom: bool = True,
        active_only: bool = True,
    ) -> List[Role]:
        """Get all roles for an organization"""
        try:
            query = self.db.query(Role).filter(Role.organization_id == organization_id)

            if not include_system:
                query = query.filter(Role.is_system == False)

            if not include_custom:
                query = query.filter(Role.is_system == True)

            if active_only:
                query = query.filter(Role.is_active == True)

            return query.order_by(Role.priority.desc()).all()

        except Exception as e:
            logger.error(f"Failed to get roles for organization {organization_id}: {e}")
            return []

    def get_users_with_role(self, role_id: str, organization_id: str) -> List[User]:
        """Get all users assigned to a specific role"""
        try:
            assignments = (
                self.db.query(UserRoleAssignment)
                .options(joinedload(UserRoleAssignment.user))
                .filter(
                    and_(
                        UserRoleAssignment.role_id == role_id,
                        UserRoleAssignment.organization_id == organization_id,
                        UserRoleAssignment.is_active == True,
                        or_(
                            UserRoleAssignment.expires_at.is_(None),
                            UserRoleAssignment.expires_at > datetime.utcnow(),
                        ),
                    )
                )
                .all()
            )

            users = []
            for assignment in assignments:
                if assignment.user:
                    users.append(assignment.user)

            return users

        except Exception as e:
            logger.error(f"Failed to get users for role {role_id}: {e}")
            return []

    def cleanup_expired_assignments(self, organization_id: str) -> int:
        """Clean up expired role assignments for a single organization.

        Scoped to ``organization_id`` (tenant isolation): without it the UPDATE
        deactivates every org's expired assignments, so one tenant's admin
        mutates other tenants' rows. Callers pass the authenticated caller's org,
        never client input.
        """
        try:
            expired_count = (
                self.db.query(UserRoleAssignment)
                .filter(
                    and_(
                        UserRoleAssignment.organization_id == organization_id,
                        UserRoleAssignment.is_active == True,
                        UserRoleAssignment.expires_at < datetime.utcnow(),
                    )
                )
                .update({"is_active": False})
            )

            self.db.commit()
            logger.info(f"Deactivated {expired_count} expired role assignments")
            return expired_count

        except Exception as e:
            self.db.rollback()
            logger.error(f"Failed to cleanup expired assignments: {e}")
            return 0

    def get_permission_categories(self) -> List[Dict[str, Any]]:
        """Get all permission categories with their permissions"""
        try:
            permissions = (
                self.db.query(Permission)
                .filter(Permission.is_active == True)
                .order_by(
                    Permission.category, Permission.scope, Permission.display_name
                )
                .all()
            )

            categories = {}
            for perm in permissions:
                if perm.category not in categories:
                    categories[perm.category] = {
                        "category": perm.category,
                        "permissions": [],
                    }

                categories[perm.category]["permissions"].append(perm.to_dict())

            return list(categories.values())

        except Exception as e:
            logger.error(f"Failed to get permission categories: {e}")
            return []

    def close(self):
        """Close database session"""
        if self.db:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# Utility functions


def get_rbac_service(db: Session = Depends(get_db_sync)) -> RBACService:
    """Get RBAC service instance.

    Injects the *sync* session: ``RBACService`` is entirely synchronous
    (``self.db.query``/``.execute``/``.commit``). The async ``get_db`` would hand
    it an ``AsyncSession``, whose sync ``.query`` raises ``AttributeError`` — so
    every ``/api/v1/rbac`` endpoint that touches the DB 500'd.
    """
    return RBACService(db)


def check_permission(
    user_id: str, permission_name: str, organization_id: str = None
) -> bool:
    """Utility function to check user permission"""
    with RBACService() as rbac:
        return rbac.user_has_permission(user_id, permission_name, organization_id)


def require_permission(permission_name: str):
    """Decorator to require specific permission"""

    def decorator(func):
        def wrapper(*args, **kwargs):
            user_id = get_current_user_id()
            organization_id = get_current_tenant_id()

            if not user_id or not organization_id:
                raise PermissionDeniedException(
                    required_permission=permission_name,
                    user_role="unknown",
                    details={"reason": "Authentication required"},
                )

            with RBACService() as rbac:
                if not rbac.user_has_permission(
                    user_id, permission_name, organization_id
                ):
                    raise PermissionDeniedException(
                        required_permission=permission_name,
                        user_role="unknown",
                        details={
                            "user_id": user_id,
                            "organization_id": organization_id,
                        },
                    )

            return func(*args, **kwargs)

        return wrapper

    return decorator
