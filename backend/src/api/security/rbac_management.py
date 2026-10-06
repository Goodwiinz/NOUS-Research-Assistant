"""
RBAC management API endpoints
Provides role and permission management for fine-grained access control
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.exceptions.analytics_exceptions import (
    ConfigurationException,
    PermissionDeniedException,
    create_permission_denied_http_exception,
)
from src.middleware.multi_tenancy import get_current_tenant_id, get_current_user_id
from src.middleware.rbac import require_permission_dep
from src.models.permission import (
    Permission,
    PermissionCategory,
    Role,
    UserRoleAssignment,
)
from src.models.user import User
from src.services.security.rbac_service import RBACService, get_rbac_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rbac", tags=["RBAC Management"])


# Pydantic models for request/response


class PermissionResponse(BaseModel):
    """Response model for permission data"""

    id: str
    name: str
    display_name: str
    description: Optional[str]
    category: str
    scope: str
    resource: Optional[str]
    is_system: bool
    is_active: bool
    created_at: str
    updated_at: Optional[str]


class RoleCreate(BaseModel):
    """Request model for creating role"""

    name: str = Field(..., min_length=2, max_length=100, description="Role name")
    display_name: str = Field(
        ..., min_length=2, max_length=255, description="Display name"
    )
    description: Optional[str] = Field(
        None, max_length=1000, description="Role description"
    )
    permission_names: List[str] = Field(
        default=[], description="List of permission names"
    )
    priority: int = Field(
        default=0, description="Role priority (higher overrides lower)"
    )


class RoleUpdate(BaseModel):
    """Request model for updating role"""

    display_name: Optional[str] = Field(None, min_length=2, max_length=255)
    description: Optional[str] = Field(None, max_length=1000)
    permission_names: Optional[List[str]] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None


class RoleResponse(BaseModel):
    """Response model for role data"""

    id: str
    name: str
    display_name: str
    description: Optional[str]
    organization_id: str
    is_system: bool
    is_active: bool
    priority: int
    permissions: List[PermissionResponse]
    created_at: str
    updated_at: Optional[str]


class RoleAssignmentCreate(BaseModel):
    """Request model for assigning role to user"""

    user_id: str = Field(..., description="User ID")
    role_id: str = Field(..., description="Role ID")
    expires_at: Optional[datetime] = Field(
        None, description="Expiration time (optional)"
    )


class RoleAssignmentResponse(BaseModel):
    """Response model for role assignment data"""

    id: str
    user_id: str
    role_id: str
    organization_id: str
    assigned_by: Optional[str]
    assigned_at: str
    expires_at: Optional[str]
    is_active: bool
    role: Optional[RoleResponse]


class UserPermissionsResponse(BaseModel):
    """Response model for user permissions"""

    user_id: str
    organization_id: str
    permissions: List[str]
    roles: List[RoleResponse]


class PermissionCategoryResponse(BaseModel):
    """Response model for permission category"""

    category: str
    permissions: List[PermissionResponse]


# Helper functions


def get_current_user_role():
    """Get current user role from RBAC service"""
    try:
        user_id = get_current_user_id()
        organization_id = get_current_tenant_id()

        if not user_id or not organization_id:
            return None

        with RBACService() as rbac:
            roles = rbac.get_user_roles(user_id, organization_id)
            return roles[0].name if roles else None
    except Exception:
        return None


# API Endpoints


@router.get("/permissions", response_model=List[PermissionResponse])
async def get_permissions(
    category: Optional[str] = Query(None, description="Filter by category"),
    scope: Optional[str] = Query(None, description="Filter by scope"),
    active_only: bool = Query(True, description="Only active permissions"),
    rbac_service: RBACService = Depends(get_rbac_service),
    _: str = Depends(require_permission_dep("permission_read")),
):
    """Get list of available permissions"""
    try:
        query = rbac_service.db.query(Permission)

        if category:
            query = query.filter(Permission.category == category)
        if scope:
            query = query.filter(Permission.scope == scope)
        if active_only:
            query = query.filter(Permission.is_active == True)

        permissions = query.order_by(
            Permission.category, Permission.scope, Permission.display_name
        ).all()

        return [PermissionResponse(**perm.to_dict()) for perm in permissions]

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve permissions",
        )


@router.get("/permissions/categories", response_model=List[PermissionCategoryResponse])
async def get_permission_categories(
    rbac_service: RBACService = Depends(get_rbac_service),
    _: str = Depends(require_permission_dep("permission_read")),
):
    """Get permissions grouped by category"""
    try:
        categories = rbac_service.get_permission_categories()
        return [PermissionCategoryResponse(**cat) for cat in categories]

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve permission categories",
        )


@router.post("/roles", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    role_data: RoleCreate,
    rbac_service: RBACService = Depends(get_rbac_service),
    _: str = Depends(require_permission_dep("role_create")),
):
    """Create a new role"""
    try:
        organization_id = get_current_tenant_id()
        if not organization_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Organization context required",
            )

        role = rbac_service.create_role(
            organization_id=organization_id,
            name=role_data.name,
            display_name=role_data.display_name,
            description=role_data.description,
            permission_names=role_data.permission_names,
            priority=role_data.priority,
        )

        # Refresh role with permissions
        rbac_service.db.refresh(role)
        return RoleResponse(**role.to_dict())

    except ConfigurationException:
        logger.warning("Invalid RBAC configuration request", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid RBAC configuration request",
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create role",
        )


@router.get("/roles", response_model=List[RoleResponse])
async def get_roles(
    include_system: bool = Query(True, description="Include system roles"),
    include_custom: bool = Query(True, description="Include custom roles"),
    active_only: bool = Query(True, description="Only active roles"),
    rbac_service: RBACService = Depends(get_rbac_service),
    _: str = Depends(require_permission_dep("role_read")),
):
    """Get roles for current organization"""
    try:
        organization_id = get_current_tenant_id()
        if not organization_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Organization context required",
            )

        roles = rbac_service.get_organization_roles(
            organization_id=organization_id,
            include_system=include_system,
            include_custom=include_custom,
            active_only=active_only,
        )

        return [RoleResponse(**role.to_dict()) for role in roles]

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve roles",
        )


@router.get("/roles/{role_id}", response_model=RoleResponse)
async def get_role(
    role_id: str = Path(..., description="Role ID"),
    rbac_service: RBACService = Depends(get_rbac_service),
    _: str = Depends(require_permission_dep("role_read")),
):
    """Get specific role details"""
    try:
        organization_id = get_current_tenant_id()

        role = (
            rbac_service.db.query(Role)
            .filter(Role.id == role_id, Role.organization_id == organization_id)
            .first()
        )

        if not role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
            )

        return RoleResponse(**role.to_dict())

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve role",
        )


@router.put("/roles/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: str,
    role_data: RoleUpdate,
    rbac_service: RBACService = Depends(get_rbac_service),
    _: str = Depends(require_permission_dep("role_update")),
):
    """Update role details"""
    try:
        organization_id = get_current_tenant_id()

        role = (
            rbac_service.db.query(Role)
            .filter(Role.id == role_id, Role.organization_id == organization_id)
            .first()
        )

        if not role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
            )

        if role.is_system:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="System roles cannot be modified",
            )

        # Update allowed fields
        update_data = role_data.dict(exclude_unset=True)
        permission_names = update_data.pop("permission_names", None)

        for field, value in update_data.items():
            if hasattr(role, field):
                setattr(role, field, value)

        # Update permissions if provided
        if permission_names is not None:
            # Remove existing permissions
            rbac_service.db.execute(
                "DELETE FROM role_permissions WHERE role_id = :role_id",
                {"role_id": role_id},
            )

            # Add new permissions
            if permission_names:
                rbac_service.assign_permissions_to_role(role_id, permission_names)

        rbac_service.db.commit()
        rbac_service.db.refresh(role)

        return RoleResponse(**role.to_dict())

    except HTTPException:
        raise
    except Exception:
        rbac_service.db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update role",
        )


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: str,
    rbac_service: RBACService = Depends(get_rbac_service),
    _: str = Depends(require_permission_dep("role_delete")),
):
    """Delete a role"""
    try:
        organization_id = get_current_tenant_id()

        role = (
            rbac_service.db.query(Role)
            .filter(Role.id == role_id, Role.organization_id == organization_id)
            .first()
        )

        if not role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
            )

        if role.is_system:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="System roles cannot be deleted",
            )

        # Check for active assignments
        active_assignments = (
            rbac_service.db.query(UserRoleAssignment)
            .filter(
                UserRoleAssignment.role_id == role_id,
                UserRoleAssignment.is_active == True,
            )
            .count()
        )

        if active_assignments > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete role with active user assignments",
            )

        rbac_service.db.delete(role)
        rbac_service.db.commit()

    except HTTPException:
        raise
    except Exception:
        rbac_service.db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete role",
        )


@router.post("/users/{user_id}/roles", response_model=RoleAssignmentResponse)
async def assign_role_to_user(
    user_id: str,
    assignment_data: RoleAssignmentCreate,
    rbac_service: RBACService = Depends(get_rbac_service),
    _: str = Depends(require_permission_dep("user_manage_roles")),
):
    """Assign a role to a user"""
    try:
        organization_id = get_current_tenant_id()
        current_user_id = get_current_user_id()

        if assignment_data.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="User ID mismatch"
            )

        assignment = rbac_service.assign_role_to_user(
            user_id=user_id,
            role_id=assignment_data.role_id,
            organization_id=organization_id,
            assigned_by=current_user_id,
            expires_at=assignment_data.expires_at,
        )

        # Refresh with role data
        rbac_service.db.refresh(assignment)
        return RoleAssignmentResponse(**assignment.to_dict())

    except ConfigurationException:
        logger.warning("Invalid RBAC configuration request", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid RBAC configuration request",
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to assign role to user",
        )


@router.delete(
    "/users/{user_id}/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_role_from_user(
    user_id: str,
    role_id: str,
    rbac_service: RBACService = Depends(get_rbac_service),
    _: str = Depends(require_permission_dep("user_manage_roles")),
):
    """Revoke a role from a user"""
    try:
        organization_id = get_current_tenant_id()

        success = rbac_service.revoke_role_from_user(
            user_id=user_id, role_id=role_id, organization_id=organization_id
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role assignment not found",
            )

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to revoke role from user",
        )


@router.get("/users/{user_id}/permissions", response_model=UserPermissionsResponse)
async def get_user_permissions(
    user_id: str,
    rbac_service: RBACService = Depends(get_rbac_service),
    _: str = Depends(require_permission_dep("user_read")),
):
    """Get user's permissions and roles"""
    try:
        organization_id = get_current_tenant_id()

        permissions = rbac_service.get_user_permissions(user_id, organization_id)
        roles = rbac_service.get_user_roles(user_id, organization_id)

        return UserPermissionsResponse(
            user_id=user_id,
            organization_id=organization_id,
            permissions=list(permissions),
            roles=[RoleResponse(**role.to_dict()) for role in roles],
        )

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve user permissions",
        )


@router.get("/users/current/permissions", response_model=UserPermissionsResponse)
async def get_current_user_permissions(
    rbac_service: RBACService = Depends(get_rbac_service),
):
    """Get current user's permissions and roles"""
    try:
        user_id = get_current_user_id()
        organization_id = get_current_tenant_id()

        if not user_id or not organization_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )

        permissions = rbac_service.get_user_permissions(user_id, organization_id)
        roles = rbac_service.get_user_roles(user_id, organization_id)

        return UserPermissionsResponse(
            user_id=user_id,
            organization_id=organization_id,
            permissions=list(permissions),
            roles=[RoleResponse(**role.to_dict()) for role in roles],
        )

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve current user permissions",
        )


@router.get("/roles/{role_id}/users", response_model=List[Dict[str, Any]])
async def get_users_with_role(
    role_id: str,
    rbac_service: RBACService = Depends(get_rbac_service),
    _: str = Depends(require_permission_dep("user_read")),
):
    """Get all users assigned to a specific role"""
    try:
        organization_id = get_current_tenant_id()

        users = rbac_service.get_users_with_role(role_id, organization_id)

        user_data = []
        for user in users:
            user_data.append(
                {
                    "id": str(user.id),
                    "email": user.email,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "is_active": user.is_active,
                    "created_at": (
                        user.created_at.isoformat() if user.created_at else None
                    ),
                }
            )

        return user_data

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve users for role",
        )


@router.post("/initialize", response_model=Dict[str, Any])
async def initialize_rbac_system(
    rbac_service: RBACService = Depends(get_rbac_service),
    _: str = Depends(require_permission_dep("system_admin")),
):
    """Initialize RBAC system with permissions and default roles"""
    try:
        organization_id = get_current_tenant_id()
        if not organization_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Organization context required",
            )

        # Initialize system permissions
        permissions_success = rbac_service.initialize_system_permissions()

        # Initialize system roles for organization
        roles_success = rbac_service.initialize_system_roles(organization_id)

        return {
            "message": "RBAC system initialization completed",
            "permissions_initialized": permissions_success,
            "roles_initialized": roles_success,
            "organization_id": organization_id,
        }

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initialize RBAC system",
        )


@router.post("/cleanup-expired", response_model=Dict[str, Any])
async def cleanup_expired_assignments(
    rbac_service: RBACService = Depends(get_rbac_service),
    _: str = Depends(require_permission_dep("system_admin")),
):
    """Clean up expired role assignments"""
    # Derived before the try so the 401 is not swallowed by the broad except
    # below. organization_id comes from the authenticated tenant context, never
    # client input — the cleanup is scoped to the caller's org.
    organization_id = get_current_tenant_id()
    if not organization_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Organization context required",
        )

    try:
        expired_count = rbac_service.cleanup_expired_assignments(organization_id)

        return {
            "message": "Cleanup completed",
            "expired_assignments_removed": expired_count,
        }

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cleanup expired assignments",
        )
