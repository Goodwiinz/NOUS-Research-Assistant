"""Owner-side listing and revocation of connected devices.

Revocation is enforced where access is checked: every grant use revalidates
its consent (`consent_revoked_at`) and device (`revoked_at`), so revoking here
takes effect on the device's next request. Grants are revoked too, so an
already-issued token fails even before that check.
"""

from datetime import datetime, timezone
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.bridge_device import BridgeDevice
from src.models.collection import Collection
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.user import User
from src.schemas.integration_connections import ConnectedDevice, ConnectionConsent

ACTIVE_STATUSES = ("approved", "consumed")


class ConnectionNotFound(Exception):
    """Opaque: missing, foreign, or already revoked."""


def _identity(user: User) -> tuple[UUID, UUID]:
    if user.organization_id is None:
        raise ConnectionNotFound()
    return cast(UUID, user.id), cast(UUID, user.organization_id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def list_connections(db: AsyncSession, user: User) -> list[ConnectedDevice]:
    user_id, organization_id = _identity(user)
    devices = (
        await db.scalars(
            select(BridgeDevice)
            .where(
                BridgeDevice.user_id == user_id,
                BridgeDevice.organization_id == organization_id,
                BridgeDevice.revoked_at.is_(None),
                BridgeDevice.is_deleted.is_(False),
            )
            .order_by(BridgeDevice.created_at.desc())
        )
    ).all()
    if not devices:
        return []
    rows = (
        await db.execute(
            select(IntegrationGrantRequest, Collection.name)
            .join(Collection, Collection.id == IntegrationGrantRequest.project_id)
            .where(
                IntegrationGrantRequest.user_id == user_id,
                IntegrationGrantRequest.organization_id == organization_id,
                IntegrationGrantRequest.device_id.in_([d.id for d in devices]),
                IntegrationGrantRequest.status.in_(ACTIVE_STATUSES),
                IntegrationGrantRequest.consent_revoked_at.is_(None),
                IntegrationGrantRequest.is_deleted.is_(False),
            )
            .order_by(IntegrationGrantRequest.created_at.desc())
        )
    ).all()
    by_device: dict[UUID, list[ConnectionConsent]] = {d.id: [] for d in devices}
    for consent, label in rows:
        by_device[consent.device_id].append(
            ConnectionConsent(
                request_id=consent.id,
                project_id=consent.project_id,
                project_label=str(label),
                scopes=sorted(consent.scopes or []),
                status=str(consent.status),
                approved_at=consent.approved_at,
            )
        )
    return [
        ConnectedDevice(
            device_id=d.id,
            device_label=d.label,
            connected_at=cast(datetime, d.created_at),
            consents=by_device[d.id],
        )
        for d in devices
    ]


async def _revoke_grants(db: AsyncSession, **match: Any) -> None:
    await db.execute(
        update(IntegrationGrant)
        .where(
            *(getattr(IntegrationGrant, k) == v for k, v in match.items()),
            IntegrationGrant.revoked_at.is_(None),
        )
        .values(revoked_at=_now())
    )


async def revoke_consent(db: AsyncSession, user: User, request_id: UUID) -> None:
    """End one device's access to one project, and every grant issued under it."""
    user_id, organization_id = _identity(user)
    revoked = await db.execute(
        update(IntegrationGrantRequest)
        .where(
            IntegrationGrantRequest.id == request_id,
            IntegrationGrantRequest.user_id == user_id,
            IntegrationGrantRequest.organization_id == organization_id,
            IntegrationGrantRequest.consent_revoked_at.is_(None),
            IntegrationGrantRequest.is_deleted.is_(False),
        )
        .values(consent_revoked_at=_now())
    )
    if cast(CursorResult[Any], revoked).rowcount != 1:
        # The conditional UPDATE matched nothing, so nothing needs undoing.
        raise ConnectionNotFound()
    await _revoke_grants(
        db, request_id=request_id, user_id=user_id, organization_id=organization_id
    )
    await db.commit()


async def disconnect_device(db: AsyncSession, user: User, device_id: UUID) -> None:
    """Disconnect a device: it, its consents and its grants stop working."""
    user_id, organization_id = _identity(user)
    revoked = await db.execute(
        update(BridgeDevice)
        .where(
            BridgeDevice.id == device_id,
            BridgeDevice.user_id == user_id,
            BridgeDevice.organization_id == organization_id,
            BridgeDevice.revoked_at.is_(None),
            BridgeDevice.is_deleted.is_(False),
        )
        .values(revoked_at=_now())
    )
    if cast(CursorResult[Any], revoked).rowcount != 1:
        # The conditional UPDATE matched nothing, so nothing needs undoing.
        raise ConnectionNotFound()
    await db.execute(
        update(IntegrationGrantRequest)
        .where(
            IntegrationGrantRequest.device_id == device_id,
            IntegrationGrantRequest.user_id == user_id,
            IntegrationGrantRequest.organization_id == organization_id,
            IntegrationGrantRequest.consent_revoked_at.is_(None),
        )
        .values(consent_revoked_at=_now())
    )
    await _revoke_grants(
        db, device_id=device_id, user_id=user_id, organization_id=organization_id
    )
    await db.commit()
