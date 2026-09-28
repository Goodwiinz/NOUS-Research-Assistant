"""Device registration and opaque workspace binding transport."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.integrations.auth import require_pairing_user
from src.api.integrations.grants import call
from src.core.database import get_db
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.user import User
from src.schemas.integration_context import (
    DeviceCreate,
    DeviceDTO,
    WorkspaceBindingCreate,
    WorkspaceBindingDTO,
)
from src.services.integrations import context as service

router = APIRouter()


@router.post("/devices", response_model=DeviceDTO)
async def create_device(
    data: DeviceCreate,
    user: User = Depends(require_pairing_user),
    db: AsyncSession = Depends(get_db),
) -> BridgeDevice:
    return await call(service.register_device(db, user, data))


@router.get("/devices", response_model=list[DeviceDTO])
async def get_devices(
    user: User = Depends(require_pairing_user), db: AsyncSession = Depends(get_db)
) -> list[BridgeDevice]:
    return await call(service.list_devices(db, user))


@router.post("/devices/{device_id}/workspaces", response_model=WorkspaceBindingDTO)
async def create_workspace_binding(
    device_id: UUID,
    data: WorkspaceBindingCreate,
    user: User = Depends(require_pairing_user),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceBinding:
    return await call(service.bind_workspace(db, user, device_id, data))


@router.get("/devices/{device_id}/workspaces", response_model=list[WorkspaceBindingDTO])
async def get_workspace_bindings(
    device_id: UUID,
    user: User = Depends(require_pairing_user),
    db: AsyncSession = Depends(get_db),
) -> list[WorkspaceBinding]:
    return await call(service.list_workspaces(db, user, device_id))
