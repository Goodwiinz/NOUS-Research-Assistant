"""
FastAPI dependencies for authentication and authorization
"""

from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.config import settings
from src.core.database import get_db
from src.core.security import get_current_user_token
from src.models.organization import Organization
from src.models.user import User, UserRole


async def get_current_user(
    token_data: dict = Depends(get_current_user_token),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Get current authenticated user with eagerly loaded organization"""
    # Reject a CLI token that has been revoked (per-user "revoked before"
    # cutoff). Fail-open if Redis is down — see cli_token_revocation.
    if getattr(token_data, "is_cli", False):
        from src.core.cli_token_revocation import is_cli_token_revoked

        if await is_cli_token_revoked(
            token_data.user_id, getattr(token_data, "issued_at", None)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )

    stmt = (
        select(User)
        .options(selectinload(User.organization))
        .where(
            User.id == token_data.user_id,
            User.is_active == True,
            User.is_deleted == False,
        )
    )
    result = await db.execute(stmt)
    user = result.scalars().first()

    if user is None:
        from src.core.user_provisioning import ensure_user_and_org

        provisioned = await ensure_user_and_org(db, token_data)
        if provisioned:
            await db.commit()
            result = await db.execute(
                select(User)
                .options(selectinload(User.organization))
                .where(
                    User.id == token_data.user_id,
                    User.is_active == True,
                    User.is_deleted == False,
                )
            )
            user = result.scalars().first()

        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive",
            )

    return user


def get_current_organization(
    current_user: User = Depends(get_current_user),
) -> Organization:
    """Get current user's organization"""
    if not current_user.organization:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found"
        )

    return current_user.organization


def require_role(required_role: UserRole):
    """Dependency factory for requiring specific user role"""

    def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if not current_user.has_permission(required_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required role: {required_role.value}",
            )
        return current_user

    return role_checker


def require_admin(current_user: User = Depends(require_role(UserRole.ADMIN))) -> User:
    """Require admin role"""
    return current_user


def require_platform_operator(
    current_user: User = Depends(get_current_user),
) -> User:
    """Require an explicitly allowlisted platform-operator user UUID.

    Tenant roles intentionally do not participate in this decision.  An empty
    or malformed ``PLATFORM_OPERATOR_USER_IDS`` value produces an empty parsed
    allowlist and therefore denies every caller.
    """
    try:
        user_id = UUID(str(current_user.id))
    except (AttributeError, TypeError, ValueError):
        user_id = None

    if user_id is None or user_id not in settings.platform_operator_user_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform operator access required",
        )
    return current_user


def require_content_manager(
    current_user: User = Depends(require_role(UserRole.CONTENT_MANAGER)),
) -> User:
    """Require content manager role"""
    return current_user


def require_analyst(
    current_user: User = Depends(require_role(UserRole.ANALYST)),
) -> User:
    """Require analyst role"""
    return current_user


def require_user_role(
    current_user: User = Depends(require_role(UserRole.USER)),
) -> User:
    """Require user role (minimum role)"""
    return current_user


def can_upload_documents(current_user: User = Depends(get_current_user)) -> User:
    """Check if user can upload documents"""
    if not current_user.can_upload_documents():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not have permission to upload documents",
        )
    return current_user


def can_manage_users(current_user: User = Depends(get_current_user)) -> User:
    """Check if user can manage other users"""
    if not current_user.can_manage_users():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not have permission to manage users",
        )
    return current_user


def can_view_analytics(current_user: User = Depends(get_current_user)) -> User:
    """Check if user can view analytics"""
    if not current_user.can_view_analytics():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not have permission to view analytics",
        )
    return current_user


def can_manage_organization(current_user: User = Depends(get_current_user)) -> User:
    """Check if user can manage organization settings"""
    if not current_user.has_permission(UserRole.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not have permission to manage organization",
        )
    return current_user


def is_active_user(current_user: User = Depends(get_current_user)) -> User:
    """Check if user is active"""
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="User account is inactive"
        )
    return current_user


def belongs_to_organization(
    organization_id: str, current_user: User = Depends(get_current_user)
) -> User:
    """Check if user belongs to specified organization"""
    if str(current_user.organization_id) != organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not belong to this organization",
        )
    return current_user


def is_organization_member(
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(get_current_user),
) -> tuple[User, Organization]:
    """Check if user is member of the organization"""
    if current_user.organization_id != organization.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not a member of this organization",
        )
    return current_user, organization


def has_storage_quota(
    file_size_bytes: int, current_org: Organization = Depends(get_current_organization)
) -> Organization:
    """Check if organization has sufficient storage quota"""
    if not current_org.can_upload_file(file_size_bytes):
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Insufficient storage quota. "
                f"Available: {current_org.storage_available_gb:.2f}GB, "
                f"Required: {file_size_bytes / (1024**3):.2f}GB"
            ),
        )
    return current_org


def is_self_or_admin(
    user_id: str, current_user: User = Depends(get_current_user)
) -> User:
    """Check if user is accessing their own data or is admin"""
    if str(current_user.id) != user_id and not current_user.has_permission(
        UserRole.ADMIN
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only access your own data or require admin role",
        )
    return current_user


async def can_access_document(
    document_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> tuple[User, any]:
    """Check if user can access a document"""
    from src.models.document import Document

    stmt = select(Document).where(
        Document.id == document_id, Document.is_deleted == False
    )
    result = await db.execute(stmt)
    document = result.scalars().first()

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

    # Check if user belongs to same organization
    if document.organization_id != current_user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this document",
        )

    # Check if document is public or user has sufficient permissions.
    #
    # R4-L18: this used to read
    #   `not document.is_public and not current_user.has_permission(UserRole.USER)`
    # which can never fire — UserRole.USER is rank 0 in the role hierarchy
    # (models/user.py has_permission), so has_permission(UserRole.USER) is
    # True for every authenticated user and the branch is dead. The org
    # membership check above already restricts access to same-org users; a
    # private (non-public) document's evident additional intent is to
    # further restrict to its uploader or an admin, not every coworker in
    # the org.
    if (
        not document.is_public
        and current_user.id != document.uploaded_by_user_id
        and not current_user.has_permission(UserRole.ADMIN)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to private document",
        )

    return current_user, document


# Optional authentication dependency (doesn't raise exception if not authenticated)
async def get_current_user_optional(
    token_data: Optional[dict] = Depends(get_current_user_token),
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    """Get current user if authenticated, otherwise return None (with eagerly loaded organization).

    R4-L12: this dependency has no callers today (grepped repo-wide), so
    nothing currently relies on the "anonymous access" path it advertises.
    ``token_data`` is typed ``Optional`` but can never actually be ``None``:
    ``get_current_user_token``'s own dependency chain uses the module-level
    ``security = HTTPBearer()`` (``auto_error=True``, the default) in
    ``core/security.py``, which raises 401/403 before FastAPI even calls
    ``get_current_user_token`` if the Authorization header is missing/invalid
    — and ``get_current_user_token`` itself only ever returns a ``TokenData``
    or raises, never ``None``. If a future caller wants genuine optional
    auth, wire a separate ``HTTPBearer(auto_error=False)`` dependency chain
    (so a missing header reaches this function as ``None``) rather than
    resurrecting this branch as-is.
    """
    try:
        stmt = (
            select(User)
            .options(selectinload(User.organization))
            .where(
                User.id == token_data.user_id,
                User.is_active == True,
                User.is_deleted == False,
            )
        )
        result = await db.execute(stmt)
        user = result.scalars().first()
        return user
    except Exception:
        return None
