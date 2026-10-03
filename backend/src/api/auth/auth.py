"""
Authentication API endpoints.

Supabase handles registration, login, token refresh, and password change /
reset (the hosted GoTrue flows). This module provides profile management,
session info, admin user management, and API key endpoints.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import get_db
from src.core.dependencies import get_current_user, require_admin
from src.models.user import User, UserRole
from src.services.security.auth_service import (
    AuthenticationError,
    AuthService,
    RegistrationError,
    get_auth_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["authentication"])


# Request/Response Models
class ProfileUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[EmailStr] = None


class RoleUpdate(BaseModel):
    role: UserRole


@router.get("/me")
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Get current user information"""
    org = current_user.organization
    return {
        "user": current_user.to_dict(exclude_sensitive=True),
        "organization": org.to_dict() if org else None,
    }


@router.get("/session")
async def get_session_info(current_user: User = Depends(get_current_user)):
    """Get current session configuration info

    Returns session duration settings so frontend can configure proactive refresh.
    """
    return {
        "access_token_expires_minutes": settings.ACCESS_TOKEN_EXPIRE_MINUTES,
        "refresh_token_expires_days": settings.REFRESH_TOKEN_EXPIRE_DAYS,
        "remember_me_expires_days": settings.REMEMBER_ME_REFRESH_TOKEN_DAYS,
        "user_id": str(current_user.id),
    }


@router.put("/me")
async def update_profile(
    profile_data: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    """Update current user profile"""
    try:
        updated_user = await auth_service.update_user_profile(
            user=current_user,
            first_name=profile_data.first_name,
            last_name=profile_data.last_name,
            email=profile_data.email,
        )

        return {
            "message": "Profile updated successfully",
            "user": updated_user.to_dict(exclude_sensitive=True),
        }

    except (AuthenticationError, RegistrationError):
        logger.warning("Authentication request failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Authentication failed",
        )
    except Exception as e:
        logger.error(f"Error in update_profile: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An error occurred while processing the request",
        )


# NOTE: POST /auth/change-password was retired. Under hosted GoTrue there is no
# backend register/login, so `User.password_hash` only ever holds the random
# secret written by JIT provisioning — the "verify current password" gate could
# never pass. Password changes go through Supabase's own reset-password email.


@router.get("/users")
async def get_organization_users(
    skip: int = 0,
    limit: int = 100,
    role: Optional[UserRole] = None,
    is_active: Optional[bool] = None,
    current_user: User = Depends(require_admin),
    auth_service: AuthService = Depends(get_auth_service),
):
    """Get users in the organization (admin only)"""
    users = await auth_service.get_organization_users(
        organization_id=str(current_user.organization_id),
        skip=skip,
        limit=limit,
        role=role,
        is_active=is_active,
    )

    return {
        "users": [user.to_dict(exclude_sensitive=True) for user in users],
        "total": len(users),
        "skip": skip,
        "limit": limit,
    }


@router.put("/users/{user_id}/role")
async def update_user_role(
    user_id: str,
    role_data: RoleUpdate,
    current_user: User = Depends(require_admin),
    auth_service: AuthService = Depends(get_auth_service),
    db: AsyncSession = Depends(get_db),
):
    """Update user role (admin only)"""
    stmt = select(User).where(
        User.id == user_id,
        User.organization_id == current_user.organization_id,
        User.is_deleted == False,
    )
    result = await db.execute(stmt)
    target_user = result.scalars().first()

    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    try:
        updated_user = await auth_service.update_user_role(
            admin_user=current_user, target_user=target_user, new_role=role_data.role
        )

        return {
            "message": "User role updated successfully",
            "user": updated_user.to_dict(exclude_sensitive=True),
        }

    except (AuthenticationError, RegistrationError):
        logger.warning("Authentication request failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Authentication failed",
        )
    except Exception as e:
        logger.error(f"Error in update_user_role: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An error occurred while processing the request",
        )


@router.post("/users/{user_id}/deactivate")
async def deactivate_user(
    user_id: str,
    current_user: User = Depends(require_admin),
    auth_service: AuthService = Depends(get_auth_service),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a user (admin only)"""
    stmt = select(User).where(
        User.id == user_id,
        User.organization_id == current_user.organization_id,
        User.is_deleted == False,
    )
    result = await db.execute(stmt)
    target_user = result.scalars().first()

    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    try:
        await auth_service.deactivate_user(
            admin_user=current_user, target_user=target_user
        )

        return {"message": "User deactivated successfully"}

    except (AuthenticationError, RegistrationError):
        logger.warning("Authentication request failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Authentication failed",
        )
    except Exception as e:
        logger.error(f"Error in deactivate_user: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An error occurred while processing the request",
        )


@router.get("/statistics")
async def get_user_statistics(
    current_user: User = Depends(require_admin),
    auth_service: AuthService = Depends(get_auth_service),
):
    """Get user statistics for the organization (admin only)"""
    stats = await auth_service.get_user_statistics(
        organization_id=str(current_user.organization_id)
    )

    return stats


@router.post("/cleanup")
async def cleanup_inactive_users(
    days_inactive: int = 90,
    current_user: User = Depends(require_admin),
    auth_service: AuthService = Depends(get_auth_service),
):
    """Clean up inactive users (admin only)"""
    try:
        deleted_count = await auth_service.cleanup_inactive_users(
            organization_id=str(current_user.organization_id),
            days_inactive=days_inactive,
        )

        return {
            "message": f"Cleaned up {deleted_count} inactive users",
            "deleted_count": deleted_count,
        }

    except (AuthenticationError, RegistrationError):
        logger.warning("Authentication request failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Authentication failed",
        )
    except Exception as e:
        logger.error(f"Error in cleanup_inactive_users: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An error occurred while processing the request",
        )
