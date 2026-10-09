"""
Encryption API endpoints for managing data encryption and protection.

This module provides REST API endpoints for:
- User profile encryption management
- Organization profile encryption management
- Key rotation and management
- Encryption status and auditing
- Data protection operations
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session

from src.core.database import get_db_sync
from src.core.dependencies import is_active_user, is_platform_operator
from src.core.encryption import EncryptionError, EncryptionKeyType

# audit I12: the analytics RBAC decorators module (deleted) expected an
# AnalyticsPermission enum, not a list[str], and crashed (TypeError/500) for
# every authenticated caller. Use the repo-canonical dependency instead — the
# same one the compliance and RBAC-management routers use. No "encryption:*"
# permission exists in SYSTEM_PERMISSIONS (src/models/permission.py), so the
# closest existing permission, "system_admin", is required (sibling precedent:
# compliance.py / rbac_management.py guard their most sensitive operations
# with it).
from src.middleware.rbac import require_permission_dep
from src.models.organization import Organization
from src.models.user import User
from src.services.security.encryption_service import EncryptionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/encryption", tags=["encryption"])
security = HTTPBearer()


# Request/Response Models
class UserProfileEncryptionRequest(BaseModel):
    """Request model for encrypting user profile data"""

    user_id: UUID = Field(..., description="User ID to encrypt profile for")
    profile_data: Dict[str, Any] = Field(..., description="Profile data to encrypt")

    @validator("profile_data")
    def validate_profile_data(cls, v):
        # Basic validation for common profile fields
        allowed_fields = [
            "first_name",
            "last_name",
            "middle_name",
            "email_personal",
            "phone_mobile",
            "phone_work",
            "address_home",
            "address_work",
            "ssn",
            "passport_number",
            "driver_license",
            "emergency_contact_name",
            "emergency_contact_phone",
            "emergency_contact_relationship",
            "personal_notes",
            "preferences",
            "job_title",
            "department",
            "employee_id",
        ]

        for field in v.keys():
            if field not in allowed_fields:
                raise ValueError(f"Field not allowed for encryption: {field}")

        return v


class OrganizationProfileEncryptionRequest(BaseModel):
    """Request model for encrypting organization profile data"""

    organization_id: UUID = Field(
        ..., description="Organization ID to encrypt profile for"
    )
    profile_data: Dict[str, Any] = Field(..., description="Profile data to encrypt")

    @validator("profile_data")
    def validate_profile_data(cls, v):
        allowed_fields = [
            "legal_business_name",
            "dba_name",
            "tax_id",
            "duns_number",
            "billing_address",
            "shipping_address",
            "billing_phone",
            "billing_email",
            "bank_account_number",
            "bank_routing_number",
            "payment_method",
            "legal_contact_name",
            "legal_contact_email",
            "legal_contact_phone",
            "business_notes",
            "custom_attributes",
        ]

        for field in v.keys():
            if field not in allowed_fields:
                raise ValueError(f"Field not allowed for encryption: {field}")

        return v


class KeyRotationRequest(BaseModel):
    """Request model for key rotation"""

    key_type: str = Field(..., description="Type of key to rotate")
    organization_id: Optional[UUID] = Field(
        None, description="Organization scope (admin only)"
    )
    dry_run: bool = Field(False, description="Preview rotation without executing")

    @validator("key_type")
    def validate_key_type(cls, v):
        if v not in [EncryptionKeyType.DATA, EncryptionKeyType.FILE]:
            raise ValueError(
                f"Invalid key type. Must be one of: {EncryptionKeyType.DATA}, {EncryptionKeyType.FILE}"
            )
        return v


class DecryptionRequest(BaseModel):
    """Request model for decrypting data"""

    resource_type: str = Field(..., description="Type of resource to decrypt")
    resource_id: UUID = Field(..., description="ID of resource to decrypt")
    fields: Optional[List[str]] = Field(
        None, description="Specific fields to decrypt (all if None)"
    )


class EncryptionStatusResponse(BaseModel):
    """Response model for encryption status"""

    key_management: Dict[str, Any]
    encrypted_resources: Dict[str, Any]
    recent_operations: List[Dict[str, Any]]


class KeyRotationResponse(BaseModel):
    """Response model for key rotation"""

    key_type: str
    old_key_id: str
    new_key_id: str
    rotated_resources: int
    failed_resources: int
    errors: List[str]
    dry_run: bool = False


class EncryptionValidationResponse(BaseModel):
    """Response model for encryption validation"""

    user_profiles_tested: int
    user_profiles_passed: int
    organization_profiles_tested: int
    organization_profiles_passed: int
    user_profile_success_rate: float
    org_profile_success_rate: float
    overall_success_rate: float
    errors: List[str]


# Tenant binding (GOO-406 E2)


def _bind_caller_org(requested_org_id: Optional[UUID], current_user: User) -> UUID:
    """Return the caller's own organization id.

    A caller without an organization, or a different ``requested_org_id``,
    is refused with 403: per-org ``system_admin`` and the legacy
    ``users.role == "admin"`` are tenant roles and never authorize another
    tenant's data (or a global view).
    """
    own_org_id = current_user.organization_id
    if own_org_id is None:
        # Never fall through to an unscoped (global) view.
        raise HTTPException(
            status_code=403,
            detail="Organization context required",
        )
    if requested_org_id is not None and str(requested_org_id) != str(own_org_id):
        raise HTTPException(
            status_code=403,
            detail="Not authorized for another organization",
        )
    return own_org_id


def _require_platform_operator(current_user: User, action: str) -> None:
    """403 unless the caller is on the PLATFORM_OPERATOR_USER_IDS allowlist."""
    if not is_platform_operator(current_user):
        raise HTTPException(
            status_code=403,
            detail=f"Platform operator access required to {action}",
        )


# API Endpoints


@router.post("/profiles/user", response_model=Dict[str, Any])
async def encrypt_user_profile(
    request: UserProfileEncryptionRequest,
    current_user: User = Depends(is_active_user),
    db: Session = Depends(get_db_sync),
    _: str = Depends(require_permission_dep("system_admin")),
):
    """
    Encrypt user profile data

    This endpoint encrypts sensitive personal information in user profiles.
    Requires the system_admin permission (see module docstring, audit I12).
    """
    try:
        encryption_service = EncryptionService(db)

        # Check if user has permission to encrypt the target user's profile
        if request.user_id != current_user.id and current_user.role.value != "admin":
            raise HTTPException(
                status_code=403, detail="Not authorized to encrypt this user's profile"
            )

        # Verify target user belongs to same organization
        if request.user_id != current_user.id:
            target_user = db.query(User).filter(User.id == request.user_id).first()
            if not target_user or str(target_user.organization_id) != str(
                current_user.organization_id
            ):
                raise HTTPException(status_code=404, detail="User not found")

        # Encrypt the profile
        encrypted_profile = encryption_service.encrypt_user_profile(
            user_id=request.user_id,
            profile_data=request.profile_data,
            performed_by=current_user.id,
        )

        return {
            "message": "User profile encrypted successfully",
            "profile_id": encrypted_profile.id,
            "user_id": encrypted_profile.user_id,
            "encrypted_fields": list(request.profile_data.keys()),
        }

    except HTTPException:
        raise
    except EncryptionError as e:
        logger.error(f"Encryption failed: {e}")
        raise HTTPException(status_code=500, detail="Encryption failed")
    except Exception as e:
        logger.error(f"Internal server error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/profiles/organization", response_model=Dict[str, Any])
async def encrypt_organization_profile(
    request: OrganizationProfileEncryptionRequest,
    current_user: User = Depends(is_active_user),
    db: Session = Depends(get_db_sync),
    _: str = Depends(require_permission_dep("system_admin")),
):
    """
    Encrypt organization profile data

    This endpoint encrypts sensitive business information in organization profiles.
    Requires the system_admin permission (audit I12: no encryption:*
    permission exists in SYSTEM_PERMISSIONS).
    """
    try:
        encryption_service = EncryptionService(db)

        # GOO-406 E2: only the caller's own organization profile is writable.
        organization_id = _bind_caller_org(request.organization_id, current_user)

        # Encrypt the profile
        encrypted_profile = encryption_service.encrypt_organization_profile(
            organization_id=organization_id,
            profile_data=request.profile_data,
            performed_by=current_user.id,
        )

        return {
            "message": "Organization profile encrypted successfully",
            "profile_id": encrypted_profile.id,
            "organization_id": encrypted_profile.organization_id,
            "encrypted_fields": list(request.profile_data.keys()),
        }

    except HTTPException:
        raise
    except EncryptionError as e:
        logger.error(f"Encryption failed: {e}")
        raise HTTPException(status_code=500, detail="Encryption failed")
    except Exception as e:
        logger.error(f"Internal server error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/decrypt", response_model=Dict[str, Any])
async def decrypt_data(
    request: DecryptionRequest,
    current_user: User = Depends(is_active_user),
    db: Session = Depends(get_db_sync),
    _: str = Depends(require_permission_dep("system_admin")),
):
    """
    Decrypt sensitive data

    This endpoint decrypts sensitive data for authorized users.
    Requires the system_admin permission (see module docstring, audit I12).
    """
    try:
        encryption_service = EncryptionService(db)

        # Validate access permissions
        if request.resource_type == "user_profile":
            # Check if user can access this profile
            if (
                request.resource_id != current_user.id
                and current_user.role.value != "admin"
            ):
                raise HTTPException(
                    status_code=403, detail="Not authorized to decrypt this user's data"
                )

            # Verify target user belongs to same organization
            if request.resource_id != current_user.id:
                target_user = (
                    db.query(User).filter(User.id == request.resource_id).first()
                )
                if not target_user or str(target_user.organization_id) != str(
                    current_user.organization_id
                ):
                    raise HTTPException(status_code=404, detail="User not found")

            decrypted_data = encryption_service.decrypt_user_profile(
                user_id=request.resource_id,
                fields=request.fields,
                requested_by=current_user.id,
            )

        elif request.resource_type == "organization_profile":
            # GOO-406 E2: only the caller's own organization.
            _bind_caller_org(request.resource_id, current_user)

            # Implementation for organization profile decryption would go here
            decrypted_data = {
                "message": "Organization profile decryption not yet implemented"
            }

        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported resource type: {request.resource_type}",
            )

        return {
            "resource_type": request.resource_type,
            "resource_id": str(request.resource_id),
            "decrypted_data": decrypted_data,
            "fields_decrypted": (
                list(decrypted_data.keys()) if isinstance(decrypted_data, dict) else []
            ),
        }

    except HTTPException:
        raise
    except EncryptionError as e:
        logger.error(f"Decryption failed: {e}")
        raise HTTPException(status_code=500, detail="Decryption failed")
    except Exception as e:
        logger.error(f"Internal server error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/keys/rotate", response_model=KeyRotationResponse)
async def rotate_encryption_key(
    request: KeyRotationRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(is_active_user),
    db: Session = Depends(get_db_sync),
    _: str = Depends(require_permission_dep("system_admin")),
):
    """
    Rotate encryption keys

    This endpoint rotates encryption keys for enhanced security.
    Requires the system_admin permission (see module docstring, audit I12).
    """
    try:
        encryption_service = EncryptionService(db)

        # GOO-406 E2: the DATA/FILE keys are process-global and
        # rotate_encryption_keys re-encrypts every tenant's rows regardless of
        # organization_id, so a real rotation is a platform action. A tenant
        # system_admin may only dry-run, scoped to its own organization.
        organization_id: Optional[UUID] = request.organization_id
        if not is_platform_operator(current_user):
            if not request.dry_run:
                _require_platform_operator(current_user, "rotate encryption keys")
            organization_id = _bind_caller_org(organization_id, current_user)

        # Perform key rotation
        rotation_results = encryption_service.rotate_encryption_keys(
            key_type=request.key_type,
            performed_by=current_user.id,
            organization_id=organization_id,
            dry_run=request.dry_run,
        )

        return KeyRotationResponse(**rotation_results)

    except HTTPException:
        raise
    except EncryptionError as e:
        logger.error(f"Key rotation failed: {e}")
        raise HTTPException(status_code=500, detail="Key rotation failed")
    except Exception as e:
        logger.error(f"Internal server error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/status", response_model=EncryptionStatusResponse)
async def get_encryption_status(
    organization_id: Optional[UUID] = Query(
        None, description="Organization scope (admin only)"
    ),
    current_user: User = Depends(is_active_user),
    db: Session = Depends(get_db_sync),
    _: str = Depends(require_permission_dep("system_admin")),
):
    """
    Get encryption status and statistics

    This endpoint returns the current encryption status and statistics.
    Requires the system_admin permission (see module docstring, audit I12).
    """
    try:
        encryption_service = EncryptionService(db)

        # GOO-406 E2: tenants see only their own organization; the global
        # view (organization_id omitted) is reserved for platform operators.
        if not is_platform_operator(current_user):
            organization_id = _bind_caller_org(organization_id, current_user)

        status = encryption_service.get_encryption_status(organization_id)

        return EncryptionStatusResponse(**status)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Internal server error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/validate", response_model=EncryptionValidationResponse)
async def validate_encryption_integrity(
    sample_size: int = Query(10, ge=1, le=100, description="Number of records to test"),
    current_user: User = Depends(is_active_user),
    db: Session = Depends(get_db_sync),
    _: str = Depends(require_permission_dep("system_admin")),
):
    """
    Validate encryption integrity

    This endpoint validates the integrity of encrypted data by testing sample records.
    Requires the system_admin permission (see module docstring, audit I12).
    """
    try:
        encryption_service = EncryptionService(db)

        # GOO-406 E2: validation samples every tenant's encrypted rows.
        _require_platform_operator(current_user, "validate encryption integrity")

        validation_results = encryption_service.validate_encryption_integrity(
            sample_size
        )

        return EncryptionValidationResponse(**validation_results)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Internal server error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/audit/logs", response_model=List[Dict[str, Any]])
async def get_encryption_audit_logs(
    limit: int = Query(50, ge=1, le=500, description="Number of logs to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    operation_type: Optional[str] = Query(None, description="Filter by operation type"),
    resource_type: Optional[str] = Query(None, description="Filter by resource type"),
    current_user: User = Depends(is_active_user),
    db: Session = Depends(get_db_sync),
    _: str = Depends(require_permission_dep("system_admin")),
):
    """
    Get encryption audit logs

    This endpoint returns encryption operation audit logs.
    Requires the system_admin permission (see module docstring, audit I12).
    """
    try:
        from src.models.encrypted_user import EncryptionAuditLog

        query = db.query(EncryptionAuditLog)

        # Apply filters
        if operation_type:
            query = query.filter(EncryptionAuditLog.operation_type == operation_type)
        if resource_type:
            query = query.filter(EncryptionAuditLog.resource_type == resource_type)

        # GOO-406 E2: tenant callers see only their own organization's rows;
        # the legacy users.role "admin" is per-org and no longer unscopes this.
        # _bind_caller_org 403s an org-less caller (never an IS NULL filter).
        if not is_platform_operator(current_user):
            own_org_id = _bind_caller_org(None, current_user)
            query = query.filter(EncryptionAuditLog.organization_id == own_org_id)

        # Apply pagination and ordering
        logs = (
            query.order_by(EncryptionAuditLog.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        return [
            {
                "id": str(log.id),
                "operation_type": log.operation_type,
                "resource_type": log.resource_type,
                "resource_id": str(log.resource_id),
                "key_id": log.key_id,
                "performed_by": str(log.performed_by) if log.performed_by else None,
                "organization_id": (
                    str(log.organization_id) if log.organization_id else None
                ),
                "ip_address": log.ip_address,
                "user_agent": log.user_agent,
                "success": log.success,
                "error_message": log.error_message,
                "created_at": log.created_at.isoformat(),
            }
            for log in logs
        ]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Internal server error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/config/sensitive-fields", response_model=List[str])
async def get_sensitive_fields_config(
    current_user: User = Depends(is_active_user),
    _: str = Depends(require_permission_dep("system_admin")),
):
    """
    Get list of configured sensitive field patterns

    This endpoint returns the list of field patterns that are automatically encrypted.
    Requires the system_admin permission (see module docstring, audit I12).
    """
    try:
        from src.middleware.encryption_middleware import EncryptionMiddleware

        # Return the default sensitive fields
        middleware = EncryptionMiddleware(
            None
        )  # Create instance to access default fields
        return middleware.sensitive_fields + middleware.sensitive_patterns

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Internal server error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
