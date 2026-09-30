"""Browser-only transport for listing and revoking connected devices."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.integrations.auth import require_interactive_user
from src.core.database import get_db
from src.models.user import User
from src.schemas.integration_connections import ConnectedDevice
from src.services.integrations.connections import (
    ConnectionNotFound,
    disconnect_device,
    list_connections,
    revoke_consent,
)

router = APIRouter(tags=["integrations"])


@router.get("/connections", response_model=list[ConnectedDevice])
async def get_connections(
    user: User = Depends(require_interactive_user),
    db: AsyncSession = Depends(get_db),
) -> list[ConnectedDevice]:
    try:
        return await list_connections(db, user)
    except ConnectionNotFound as error:
        raise HTTPException(404, "Connection not found") from error


@router.post("/grant-requests/{request_id}/revoke", status_code=204)
async def post_revoke_consent(
    request_id: UUID,
    user: User = Depends(require_interactive_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        await revoke_consent(db, user, request_id)
    except ConnectionNotFound as error:
        raise HTTPException(404, "Connection not found") from error
    return Response(status_code=204)


@router.post("/devices/{device_id}/revoke", status_code=204)
async def post_disconnect_device(
    device_id: UUID,
    user: User = Depends(require_interactive_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        await disconnect_device(db, user, device_id)
    except ConnectionNotFound as error:
        raise HTTPException(404, "Connection not found") from error
    return Response(status_code=204)
