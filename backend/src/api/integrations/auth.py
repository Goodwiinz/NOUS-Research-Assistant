"""Interactive consent and dual-credential integration authentication."""

from typing import Awaitable, Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.core.security import TokenData, get_current_user_token
from src.models.user import User
from src.schemas.integration_context import IntegrationContext
from src.services.integrations.context import (
    IntegrationAccessDenied,
    resolve_integration_context,
)


async def require_interactive_user(
    request: Request,
    token: TokenData = Depends(get_current_user_token),
    user: User = Depends(get_current_user),
) -> User:
    if token.is_cli or "x-nous-integration-grant" in request.headers:
        raise HTTPException(403, "Interactive browser authentication required")
    return user


async def require_pairing_user(
    request: Request, user: User = Depends(get_current_user)
) -> User:
    """Bootstrap routes accept NOUS login, never a restricted grant as authority."""
    if "x-nous-integration-grant" in request.headers or user.organization_id is None:
        raise HTTPException(403, "Pairing authentication required")
    return user


async def require_cli_user(
    request: Request,
    token: TokenData = Depends(get_current_user_token),
    user: User = Depends(get_current_user),
) -> User:
    if not token.is_cli or user.organization_id is None:
        raise HTTPException(403, "CLI authentication required")
    if str(token.user_id) != str(user.id) or str(token.organization_id) != str(
        user.organization_id
    ):
        raise HTTPException(403, "Integration access denied")
    return user


async def integration_context(
    request: Request,
    required_scope: str,
    db: AsyncSession,
    token: TokenData,
    user: User,
) -> IntegrationContext:
    """Reusable transport adapter for sibling plans; scope is server-selected."""
    await require_cli_user(request, token, user)
    try:
        context = await resolve_integration_context(
            db,
            request.headers.get("x-nous-integration-grant", ""),
            required_scope=required_scope,
        )
    except IntegrationAccessDenied as error:
        raise HTTPException(403, "Integration access denied") from error
    if context.user_id != user.id or context.organization_id != user.organization_id:
        raise HTTPException(403, "Integration access denied")
    return context


def require_integration_context(
    required_scope: str,
) -> Callable[..., Awaitable[IntegrationContext]]:
    async def dependency(
        request: Request,
        db: AsyncSession = Depends(get_db),
        token: TokenData = Depends(get_current_user_token),
        user: User = Depends(get_current_user),
    ) -> IntegrationContext:
        return await integration_context(request, required_scope, db, token, user)

    return dependency
