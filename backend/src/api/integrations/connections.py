"""Browser-only transport for listing and revoking connected devices."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.integrations.auth import require_interactive_user
from src.core.cli_token_revocation import CliTokenRevocationUnavailable
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


@router.post(
    "/devices/{device_id}/revoke",
    status_code=204,
    description=(
        "Disconnect this device and revoke its integration grants. Also ends all "
        "existing CLI sign-ins for this account because CLI tokens are not device "
        "bound, so every other connected device fails its next request until "
        "`nous-harness connect` runs on it again; that registers a new device "
        "and consent and leaves the old ones listed until revoked. Revoke the "
        "old consent rather than disconnecting the old device, which would end "
        "every CLI sign-in again; the old device then stays listed with no "
        "access. To remove one device's access without ending every sign-in, "
        "revoke its consents with POST "
        "/api/v1/integrations/grant-requests/{request_id}/revoke. Returns 503 "
        "without committing device revocation if the shared CLI cutoff fails."
    ),
    responses={503: {"description": "Revocation unavailable; retry disconnect"}},
)
async def post_disconnect_device(
    device_id: UUID,
    user: User = Depends(require_interactive_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        await disconnect_device(db, user, device_id)
    except ConnectionNotFound as error:
        raise HTTPException(404, "Connection not found") from error
    except CliTokenRevocationUnavailable as error:
        raise HTTPException(503, "Disconnect unavailable. Please retry.") from error
    return Response(status_code=204)
