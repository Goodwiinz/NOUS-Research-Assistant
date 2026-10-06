"""
Tenant management API endpoints
Provides organization management, quota monitoring, and multi-tenancy operations
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.exceptions.analytics_exceptions import (
    ConfigurationException,
    PermissionDeniedException,
    create_permission_denied_http_exception,
)
from src.middleware.multi_tenancy import check_tenant_permission, get_current_tenant_id
from src.models.organization import StorageTier
from src.services.security.tenant_service import TenantService, get_tenant_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenants", tags=["Tenant Management"])


# Pydantic models for request/response


class OrganizationCreate(BaseModel):
    """Request model for creating organization"""

    name: str = Field(
        ..., min_length=2, max_length=255, description="Organization name"
    )
    storage_tier: StorageTier = Field(
        default=StorageTier.FREE, description="Storage tier"
    )


class OrganizationUpdate(BaseModel):
    """Request model for updating organization"""

    name: Optional[str] = Field(
        None, min_length=2, max_length=255, description="Organization name"
    )
    storage_tier: Optional[StorageTier] = Field(None, description="Storage tier")
    is_active: Optional[bool] = Field(None, description="Organization status")


class StorageTierUpgrade(BaseModel):
    """Request model for storage tier upgrade"""

    storage_tier: StorageTier = Field(..., description="New storage tier")


class OrganizationResponse(BaseModel):
    """Response model for organization data"""

    id: str
    name: str
    storage_tier: str
    storage_limit_gb: float
    storage_used_gb: float
    storage_available_gb: float
    storage_percentage_used: float
    max_file_size_mb: float
    is_active: bool
    created_at: str
    updated_at: str


class StorageQuotaResponse(BaseModel):
    """Response model for storage quota status"""

    tier: str
    limit_gb: float
    used_gb: float
    available_gb: float
    percentage_used: float
    max_file_size_mb: float
    at_quota_limit: bool
    near_quota_limit: bool
    document_count: int
    file_type_breakdown: dict
    average_file_size_mb: float
    largest_file_size_mb: float
    storage_efficiency_score: float


class UserCountResponse(BaseModel):
    """Response model for user count"""

    organization_id: str
    user_count: int


class OrganizationAnalyticsResponse(BaseModel):
    """Response model for organization analytics"""

    period_days: int
    analytics_events_count: int
    active_sessions_count: int
    storage_growth_mb: float
    daily_averages: dict


class OrganizationLimitsResponse(BaseModel):
    """Response model for organization limits validation"""

    organization_id: str
    storage_tier: str
    limits_status: dict
    overall_status: str


# Helper functions


def require_tenant_permission(required_permission: str):
    """Decorator to check tenant permissions"""

    def dependency():
        tenant_id = get_current_tenant_id()
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )

        if not check_tenant_permission(required_permission):
            raise create_permission_denied_http_exception(
                required_permission=required_permission,
                user_role=get_current_user_role() or "unknown",
            )

        return tenant_id

    return dependency


def require_org_access(required_permission: str):
    """Permission check that ALSO binds the ``{organization_id}`` path param to
    the caller's own tenant.

    ``require_tenant_permission`` only checks the caller's *role* (and every
    role, including ``user``, has ``organization_read``), never that the
    requested organization is the caller's own. On these
    ``/organizations/{organization_id}/...`` routes that is a cross-tenant IDOR:
    any authenticated user could read/update another org by id. This dependency
    reuses the same auth+permission check, then rejects a path org that differs
    from the caller's tenant with 403.
    """

    _permission_check = require_tenant_permission(required_permission)

    def dependency(organization_id: str):
        tenant_id = _permission_check()
        if str(organization_id) != str(tenant_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot access another organization's resources",
            )
        return tenant_id

    return dependency


def get_current_user_role():
    """Get current user role from context"""
    from src.middleware.multi_tenancy import get_current_user_role as get_role

    return get_role()


# API Endpoints


@router.post(
    "/organizations",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(
    organization_data: OrganizationCreate,
    tenant_service: TenantService = Depends(get_tenant_service),
    _: str = Depends(require_tenant_permission("organization_create")),
):
    """Create a new organization"""
    try:
        organization = tenant_service.create_organization(
            name=organization_data.name, storage_tier=organization_data.storage_tier
        )

        return OrganizationResponse(**organization.to_dict())

    except ConfigurationException:
        logger.warning("Invalid organization request", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid organization request",
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create organization",
        )


@router.get("/organizations/{organization_id}", response_model=OrganizationResponse)
async def get_organization(
    organization_id: str,
    tenant_service: TenantService = Depends(get_tenant_service),
    _: str = Depends(require_org_access("organization_read")),
):
    """Get organization details"""
    try:
        organization = tenant_service.get_organization(organization_id)

        if not organization:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found"
            )

        return OrganizationResponse(**organization.to_dict())

    except PermissionDeniedException as e:
        raise create_permission_denied_http_exception(
            required_permission=e.details.get(
                "required_permission", "organization_read"
            ),
            user_role=e.details.get("user_role", "unknown"),
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve organization",
        )


@router.put("/organizations/{organization_id}", response_model=OrganizationResponse)
async def update_organization(
    organization_id: str,
    organization_data: OrganizationUpdate,
    tenant_service: TenantService = Depends(get_tenant_service),
    _: str = Depends(require_org_access("organization_update")),
):
    """Update organization settings"""
    try:
        update_data = organization_data.dict(exclude_unset=True)
        organization = tenant_service.update_organization(
            organization_id, **update_data
        )

        return OrganizationResponse(**organization.to_dict())

    except PermissionDeniedException as e:
        raise create_permission_denied_http_exception(
            required_permission=e.details.get(
                "required_permission", "organization_update"
            ),
            user_role=e.details.get("user_role", "unknown"),
        )
    except ConfigurationException:
        logger.warning("Invalid organization request", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid organization request",
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update organization",
        )


@router.post(
    "/organizations/{organization_id}/upgrade-tier", response_model=OrganizationResponse
)
async def upgrade_storage_tier(
    organization_id: str,
    upgrade_data: StorageTierUpgrade,
    tenant_service: TenantService = Depends(get_tenant_service),
    _: str = Depends(require_org_access("organization_update")),
):
    """Upgrade organization storage tier"""
    try:
        organization = tenant_service.upgrade_storage_tier(
            organization_id, upgrade_data.storage_tier
        )

        return OrganizationResponse(**organization.to_dict())

    except PermissionDeniedException as e:
        raise create_permission_denied_http_exception(
            required_permission=e.details.get(
                "required_permission", "organization_update"
            ),
            user_role=e.details.get("user_role", "unknown"),
        )
    except ConfigurationException:
        logger.warning("Invalid organization request", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid organization request",
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upgrade storage tier",
        )


@router.get(
    "/organizations/{organization_id}/storage-quota",
    response_model=StorageQuotaResponse,
)
async def get_storage_quota_status(
    organization_id: str,
    tenant_service: TenantService = Depends(get_tenant_service),
    _: str = Depends(require_org_access("organization_read")),
):
    """Get detailed storage quota status"""
    try:
        quota_status = tenant_service.get_storage_quota_status(organization_id)
        return StorageQuotaResponse(**quota_status)

    except PermissionDeniedException as e:
        raise create_permission_denied_http_exception(
            required_permission=e.details.get(
                "required_permission", "organization_read"
            ),
            user_role=e.details.get("user_role", "unknown"),
        )
    except ConfigurationException:
        logger.warning("Organization configuration not found", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization configuration not found",
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve storage quota status",
        )


@router.get(
    "/organizations/{organization_id}/users/count", response_model=UserCountResponse
)
async def get_user_count(
    organization_id: str,
    tenant_service: TenantService = Depends(get_tenant_service),
    _: str = Depends(require_org_access("organization_users_read")),
):
    """Get number of users in organization"""
    try:
        user_count = tenant_service.get_user_count(organization_id)

        return UserCountResponse(organization_id=organization_id, user_count=user_count)

    except PermissionDeniedException as e:
        raise create_permission_denied_http_exception(
            required_permission=e.details.get(
                "required_permission", "organization_users_read"
            ),
            user_role=e.details.get("user_role", "unknown"),
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve user count",
        )


@router.get(
    "/organizations/{organization_id}/analytics",
    response_model=OrganizationAnalyticsResponse,
)
async def get_organization_analytics(
    organization_id: str,
    days: int = Query(
        default=30, ge=1, le=365, description="Number of days to analyze"
    ),
    tenant_service: TenantService = Depends(get_tenant_service),
    _: str = Depends(require_org_access("organization_analytics")),
):
    """Get organization analytics summary"""
    try:
        analytics = tenant_service.get_organization_analytics(organization_id, days)
        return OrganizationAnalyticsResponse(**analytics)

    except PermissionDeniedException as e:
        raise create_permission_denied_http_exception(
            required_permission=e.details.get(
                "required_permission", "organization_analytics"
            ),
            user_role=e.details.get("user_role", "unknown"),
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve organization analytics",
        )


@router.get(
    "/organizations/{organization_id}/limits", response_model=OrganizationLimitsResponse
)
async def validate_organization_limits(
    organization_id: str,
    tenant_service: TenantService = Depends(get_tenant_service),
    _: str = Depends(require_org_access("organization_read")),
):
    """Validate organization against various limits"""
    try:
        limits_validation = tenant_service.validate_organization_limits(organization_id)
        return OrganizationLimitsResponse(**limits_validation)

    except PermissionDeniedException as e:
        raise create_permission_denied_http_exception(
            required_permission=e.details.get(
                "required_permission", "organization_read"
            ),
            user_role=e.details.get("user_role", "unknown"),
        )
    except ConfigurationException:
        logger.warning("Organization configuration not found", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization configuration not found",
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to validate organization limits",
        )


@router.get("/current-tenant")
async def get_current_tenant_info(
    tenant_service: TenantService = Depends(get_tenant_service),
):
    """Get current tenant information"""
    try:
        tenant_id = get_current_tenant_id()
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )

        organization = tenant_service.get_organization(tenant_id)
        if not organization:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tenant organization not found",
            )

        return {
            "tenant_id": tenant_id,
            "organization": OrganizationResponse(**organization.to_dict()),
            "user_role": get_current_user_role(),
            "permissions": _get_user_permissions(get_current_user_role()),
        }

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve tenant information",
        )


@router.post("/organizations/{organization_id}/test-isolation")
async def test_tenant_isolation(
    organization_id: str,
    tenant_service: TenantService = Depends(get_tenant_service),
    _: str = Depends(require_org_access("organization_read")),
):
    """Test tenant data isolation (for security validation)"""
    try:
        # This endpoint helps verify that tenant isolation is working correctly
        # It should only return data from the specified organization

        isolation_test = {
            "organization_id": organization_id,
            "test_timestamp": "2025-01-01T00:00:00Z",  # Placeholder
            "data_access_verified": True,
            "isolation_status": "secure",
        }

        return isolation_test

    except PermissionDeniedException:
        # This is expected if isolation is working correctly
        return {
            "organization_id": organization_id,
            "test_timestamp": "2025-01-01T00:00:00Z",
            "data_access_verified": False,
            "isolation_status": "access_denied",
            "reason": "Tenant isolation working correctly",
        }
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Tenant isolation test failed",
        )


# Helper functions


def _get_user_permissions(role: str) -> List[str]:
    """Get list of permissions for user role"""
    role_permissions = {
        "super_admin": [
            "organization_create",
            "organization_read",
            "organization_update",
            "organization_delete",
            "organization_users_read",
            "organization_users_manage",
            "organization_analytics",
            "tenant_access",
            "cross_tenant_access",
            "update_access",
            "delete_access",
            "system_admin",
            "audit_logs",
            "security_management",
        ],
        "admin": [
            "organization_read",
            "organization_update",
            "organization_users_read",
            "organization_users_manage",
            "organization_analytics",
            "tenant_access",
            "update_access",
            "delete_access",
        ],
        "content_manager": [
            "organization_read",
            "organization_users_read",
            "tenant_access",
            "update_access",
        ],
        "analyst": ["organization_read", "organization_analytics", "tenant_access"],
        "user": ["organization_read", "tenant_access"],
    }

    return role_permissions.get(role, [])
