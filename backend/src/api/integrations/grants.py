"""Grant transport; immutable consent and transactions are service-owned."""

from typing import Awaitable, TypeVar
from uuid import UUID

T = TypeVar("T")

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.integrations.auth import (
    integration_context,
    require_cli_user,
    require_interactive_user,
    require_pairing_user,
)
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.core.security import TokenData, get_current_user_token
from src.models.user import User
from src.schemas.integration_context import (
    GrantDecision,
    GrantRequestCreate,
    GrantRequestDTO,
    IssuedGrant,
)
from src.services.integrations import context as service

router = APIRouter()


async def call(operation: Awaitable[T]) -> T:
    try:
        return await operation
    except service.IntegrationAccessDenied as error:
        raise HTTPException(403, "Integration access denied") from error
    except service.IntegrationConflict as error:
        raise HTTPException(
            409, "Integration request is no longer available"
        ) from error


@router.post("/grant-requests", response_model=GrantRequestDTO)
async def create_grant_request(
    data: GrantRequestCreate,
    user: User = Depends(require_cli_user),
    db: AsyncSession = Depends(get_db),
) -> GrantRequestDTO:
    return await call(service.create_request(db, user, data))


@router.get("/grant-requests/{request_id}", response_model=GrantRequestDTO)
async def get_grant_request(
    request_id: UUID,
    user: User = Depends(require_pairing_user),
    db: AsyncSession = Depends(get_db),
) -> GrantRequestDTO:
    return await call(service.request_dto(db, user, request_id))


@router.post("/grant-requests/{request_id}/decision", response_model=GrantRequestDTO)
async def decide_grant_request(
    request_id: UUID,
    data: GrantDecision,
    user: User = Depends(require_interactive_user),
    db: AsyncSession = Depends(get_db),
) -> GrantRequestDTO:
    return await call(service.decide_request(db, user, request_id, data.approved))


@router.post("/grant-requests/{request_id}/exchange", response_model=IssuedGrant)
async def exchange_grant_request(
    request_id: UUID,
    user: User = Depends(require_cli_user),
    db: AsyncSession = Depends(get_db),
) -> IssuedGrant:
    return await call(service.exchange_request(db, user, request_id))


@router.post("/grants/{grant_id}/renew", response_model=IssuedGrant)
async def renew_grant(
    grant_id: UUID,
    request: Request,
    user: User = Depends(require_cli_user),
    db: AsyncSession = Depends(get_db),
) -> IssuedGrant:
    # Require possession of this grant as well as the verified CLI JWT.
    grant = await call(service.owned_grant(db, user, grant_id))
    context = await call(
        service.resolve_integration_context(
            db,
            request.headers.get("x-nous-integration-grant", ""),
            required_scope=grant.scopes[0],
        )
    )
    if (
        context.grant_id != grant_id
        or context.user_id != user.id
        or context.organization_id != user.organization_id
    ):
        raise HTTPException(403, "Integration access denied")
    return await call(service.renew_grant(db, user, grant_id))


@router.delete("/grants/{grant_id}", status_code=204)
async def delete_grant(
    grant_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    token: TokenData = Depends(get_current_user_token),
    db: AsyncSession = Depends(get_db),
) -> Response:
    grant = await call(service.owned_grant(db, user, grant_id))
    if token.is_cli:
        context = await integration_context(
            request, next(iter(grant.scopes), ""), db, token, user
        )
        if context.grant_id != grant_id:
            raise HTTPException(403, "Integration access denied")
    else:
        await require_interactive_user(request, token, user)
    await service.revoke_integration_grant(db, grant_id, end_cli_sessions=token.is_cli)
    return Response(status_code=204)
