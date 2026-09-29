"""
API Key Management endpoints for administrators
"""

import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.api_key_auth import (
    APIKey,
    APIKeyCreate,
    APIKeyData,
    APIKeyResponse,
    api_key_auth,
    generate_api_key,
)
from src.core.database import get_db
from src.core.dependencies import require_admin
from src.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


@router.post("/", response_model=APIKeyResponse)
async def create_api_key(
    api_key_create: APIKeyCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new API key (Admin only).

    **WARNING**: The raw API key is only shown once during creation.
    Store it securely as it cannot be retrieved later.
    """
    try:
        # Enforce organization scoping — API keys must be tied to an organization
        if not current_user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot create API key: user is not associated with an organization",
            )

        # Generate API key
        raw_key, key_hash = generate_api_key()
        key_prefix = raw_key[:8]

        # Calculate expiration date
        expires_at = None
        if api_key_create.expires_days:
            expires_at = datetime.utcnow() + timedelta(days=api_key_create.expires_days)

        # Create API key record
        new_api_key = APIKey(
            name=api_key_create.name,
            key_hash=key_hash,
            key_prefix=key_prefix,
            rate_limit_per_hour=api_key_create.rate_limit_per_hour,
            description=api_key_create.description,
            created_by=f"{current_user.email} ({current_user.id})",
            expires_at=expires_at,
            allowed_endpoints=(
                json.dumps(api_key_create.allowed_endpoints)
                if api_key_create.allowed_endpoints
                else None
            ),
            organization_id=str(current_user.organization_id),
        )

        db.add(new_api_key)
        await db.commit()
        await db.refresh(new_api_key)

        logger.info(
            f"API key created: {new_api_key.name} ({key_prefix}***) by {current_user.email}"
        )

        return APIKeyResponse(
            id=new_api_key.id,
            name=new_api_key.name,
            api_key=raw_key,  # Only shown during creation
            key_prefix=key_prefix,
            rate_limit_per_hour=new_api_key.rate_limit_per_hour,
            expires_at=expires_at,
            organization_id=new_api_key.organization_id,
        )

    except Exception as e:
        logger.error(f"Error creating API key: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create API key")


@router.get("/", response_model=List[APIKeyData])
async def list_api_keys(
    active_only: bool = Query(default=True, description="Show only active API keys"),
    limit: int = Query(
        default=50, ge=1, le=100, description="Number of keys to return"
    ),
    offset: int = Query(default=0, ge=0, description="Number of keys to skip"),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    List all API keys (Admin only).

    Note: Raw API keys are never returned, only metadata.
    """
    try:
        # Prevent IDOR by scoping to the admin's organization
        stmt = select(APIKey).where(
            APIKey.organization_id == str(current_user.organization_id)
        )

        if active_only:
            stmt = stmt.where(APIKey.is_active == True)

        # Check for non-expired keys if active_only is True
        if active_only:
            stmt = stmt.where(
                or_(APIKey.expires_at.is_(None), APIKey.expires_at > datetime.utcnow())
            )

        stmt = stmt.order_by(APIKey.created_at.desc()).offset(offset).limit(limit)
        result = await db.execute(stmt)
        api_keys = result.scalars().all()

        return [
            APIKeyData(
                id=key.id,
                name=key.name,
                key_prefix=key.key_prefix,
                is_active=key.is_active,
                rate_limit_per_hour=key.rate_limit_per_hour,
                last_used_at=key.last_used_at,
                usage_count=key.usage_count,
                organization_id=key.organization_id,
            )
            for key in api_keys
        ]

    except Exception as e:
        logger.error(f"Error listing API keys: {e}")
        raise HTTPException(status_code=500, detail="Failed to list API keys")


@router.get("/{api_key_id}", response_model=APIKeyData)
async def get_api_key(
    api_key_id: str,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Get API key details by ID (Admin only).
    """
    try:
        # Prevent IDOR by scoping to the admin's organization
        result = await db.execute(
            select(APIKey).where(
                APIKey.id == api_key_id,
                APIKey.organization_id == str(current_user.organization_id),
            )
        )
        api_key = result.scalars().first()

        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")

        return APIKeyData(
            id=api_key.id,
            name=api_key.name,
            key_prefix=api_key.key_prefix,
            is_active=api_key.is_active,
            rate_limit_per_hour=api_key.rate_limit_per_hour,
            last_used_at=api_key.last_used_at,
            usage_count=api_key.usage_count,
            organization_id=api_key.organization_id,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting API key {api_key_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get API key")


@router.patch("/{api_key_id}")
async def update_api_key(
    api_key_id: str,
    name: Optional[str] = None,
    is_active: Optional[bool] = None,
    rate_limit_per_hour: Optional[int] = None,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Update API key properties (Admin only).
    """
    try:
        # Prevent IDOR by scoping to the admin's organization
        result = await db.execute(
            select(APIKey).where(
                APIKey.id == api_key_id,
                APIKey.organization_id == str(current_user.organization_id),
            )
        )
        api_key = result.scalars().first()

        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")

        # Update fields if provided
        if name is not None:
            api_key.name = name
        if is_active is not None:
            api_key.is_active = is_active
        if rate_limit_per_hour is not None:
            if rate_limit_per_hour < 1:
                raise HTTPException(
                    status_code=400,
                    detail="Rate limit must be at least 1 request per hour",
                )
            api_key.rate_limit_per_hour = rate_limit_per_hour

        await db.commit()
        await db.refresh(api_key)

        logger.info(
            f"API key updated: {api_key.name} ({api_key.key_prefix}***) by {current_user.email}"
        )

        return {
            "message": "API key updated successfully",
            "api_key": APIKeyData(
                id=api_key.id,
                name=api_key.name,
                key_prefix=api_key.key_prefix,
                is_active=api_key.is_active,
                rate_limit_per_hour=api_key.rate_limit_per_hour,
                last_used_at=api_key.last_used_at,
                usage_count=api_key.usage_count,
                organization_id=api_key.organization_id,
            ),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating API key {api_key_id}: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update API key")


@router.delete("/{api_key_id}")
async def delete_api_key(
    api_key_id: str,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete (deactivate) an API key (Admin only).

    Note: API keys are not permanently deleted for audit purposes,
    but are marked as inactive.
    """
    try:
        # Prevent IDOR by scoping to the admin's organization
        result = await db.execute(
            select(APIKey).where(
                APIKey.id == api_key_id,
                APIKey.organization_id == str(current_user.organization_id),
            )
        )
        api_key = result.scalars().first()

        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")

        # Deactivate instead of deleting for audit trail
        api_key.is_active = False
        await db.commit()

        logger.info(
            f"API key deactivated: {api_key.name} ({api_key.key_prefix}***) by {current_user.email}"
        )

        return {
            "message": f"API key '{api_key.name}' has been deactivated",
            "api_key_id": api_key_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting API key {api_key_id}: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to delete API key")


@router.get("/{api_key_id}/usage")
async def get_api_key_usage(
    api_key_id: str,
    days: int = Query(
        default=7, ge=1, le=30, description="Number of days of usage data"
    ),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Get usage statistics for an API key (Admin only).
    """
    try:
        # Prevent IDOR by scoping to the admin's organization
        result = await db.execute(
            select(APIKey).where(
                APIKey.id == api_key_id,
                APIKey.organization_id == str(current_user.organization_id),
            )
        )
        api_key = result.scalars().first()

        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")

        # Get current hour usage from in-memory tracking
        current_usage = await api_key_auth.get_current_usage(api_key_id)

        return {
            "api_key": {
                "id": api_key.id,
                "name": api_key.name,
                "key_prefix": api_key.key_prefix,
            },
            "usage": {
                "total_requests": api_key.usage_count,
                "current_hour_requests": current_usage,
                "rate_limit_per_hour": api_key.rate_limit_per_hour,
                "last_used_at": api_key.last_used_at,
                "created_at": api_key.created_at,
            },
            "status": {
                "is_active": api_key.is_active,
                "is_rate_limited": current_usage >= api_key.rate_limit_per_hour,
                "requests_remaining": max(
                    0, api_key.rate_limit_per_hour - current_usage
                ),
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting usage for API key {api_key_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get API key usage")


@router.post("/{api_key_id}/regenerate", response_model=APIKeyResponse)
async def regenerate_api_key(
    api_key_id: str,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Regenerate an API key (Admin only).

    **WARNING**: This will invalidate the old key immediately.
    The new key is only shown once.
    """
    try:
        # Prevent IDOR by scoping to the admin's organization
        result = await db.execute(
            select(APIKey).where(
                APIKey.id == api_key_id,
                APIKey.organization_id == str(current_user.organization_id),
            )
        )
        api_key = result.scalars().first()

        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")

        # Generate new key
        raw_key, key_hash = generate_api_key()
        key_prefix = raw_key[:8]

        # Update existing record
        old_prefix = api_key.key_prefix
        api_key.key_hash = key_hash
        api_key.key_prefix = key_prefix
        api_key.usage_count = 0  # Reset usage count
        api_key.last_used_at = None  # Reset last used

        await db.commit()
        await db.refresh(api_key)

        logger.warning(
            f"API key regenerated: {api_key.name} (old: {old_prefix}***, new: {key_prefix}***) by {current_user.email}"
        )

        return APIKeyResponse(
            id=api_key.id,
            name=api_key.name,
            api_key=raw_key,  # Only shown during regeneration
            key_prefix=key_prefix,
            rate_limit_per_hour=api_key.rate_limit_per_hour,
            expires_at=api_key.expires_at,
            organization_id=api_key.organization_id,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error regenerating API key {api_key_id}: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to regenerate API key")


@router.get("/usage/summary")
async def get_api_keys_usage_summary(
    current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    """
    Get overall usage summary for all API keys (Admin only).
    """
    try:
        # Prevent IDOR by scoping to the admin's organization
        org_id = str(current_user.organization_id)

        # Get basic statistics
        total_result = await db.execute(
            select(func.count())
            .select_from(APIKey)
            .where(APIKey.organization_id == org_id)
        )
        total_keys = total_result.scalar()

        active_result = await db.execute(
            select(func.count())
            .select_from(APIKey)
            .where(APIKey.organization_id == org_id, APIKey.is_active == True)
        )
        active_keys = active_result.scalar()

        # Get most active keys
        most_active_result = await db.execute(
            select(APIKey)
            .where(APIKey.organization_id == org_id, APIKey.is_active == True)
            .order_by(APIKey.usage_count.desc())
            .limit(5)
        )
        most_active = most_active_result.scalars().all()

        # Get recently created keys
        recent_result = await db.execute(
            select(APIKey)
            .where(APIKey.organization_id == org_id)
            .order_by(APIKey.created_at.desc())
            .limit(5)
        )
        recent_keys = recent_result.scalars().all()

        return {
            "summary": {
                "total_api_keys": total_keys,
                "active_api_keys": active_keys,
                "inactive_api_keys": total_keys - active_keys,
            },
            "most_active_keys": [
                {
                    "name": key.name,
                    "key_prefix": key.key_prefix,
                    "usage_count": key.usage_count,
                    "last_used_at": key.last_used_at,
                }
                for key in most_active
            ],
            "recent_keys": [
                {
                    "name": key.name,
                    "key_prefix": key.key_prefix,
                    "created_at": key.created_at,
                    "is_active": key.is_active,
                }
                for key in recent_keys
            ],
        }

    except Exception as e:
        logger.error(f"Error getting API keys usage summary: {e}")
        raise HTTPException(status_code=500, detail="Failed to get usage summary")
