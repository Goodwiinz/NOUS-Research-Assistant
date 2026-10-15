"""Exact-version sharing, with live creator rights and explicit recipients.

The audience grants access to the pinned bytes only. It never grants project,
source, or provenance access, and current authority is checked on each fetch.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.artifact import Artifact, ArtifactVersion
from src.models.artifact_share import ArtifactShare, ArtifactShareAudience
from src.models.user import User
from src.schemas.artifact import (
    ArtifactAccessDenied,
    ArtifactInvalidShareRequest,
    ArtifactNotFound,
    ArtifactShareDTO,
    ArtifactSharingDisabled,
    ArtifactStorageUnavailable,
    SharedArtifactVersionDTO,
)
from src.schemas.integration_context import IntegrationContext
from src.services.artifacts import service

logger = logging.getLogger(__name__)
MAX_SHARE_LIFETIME = timedelta(days=7)


def _require_sharing() -> None:
    if not settings.ARTIFACTS_ENABLED or not settings.ARTIFACT_SHARING_ENABLED:
        raise ArtifactSharingDisabled()


def _dto(share: ArtifactShare) -> ArtifactShareDTO:
    return ArtifactShareDTO(
        id=share.id,
        version_id=share.version_id,
        expires_at=service._aware(share.expires_at),
        revoked_at=service._aware(share.revoked_at) if share.revoked_at else None,
    )


async def _authorize_creator(
    db: AsyncSession, *, user_id: UUID, organization_id: UUID, artifact_id: UUID
) -> None:
    artifact = await db.scalar(
        select(Artifact)
        .where(
            Artifact.id == artifact_id,
            Artifact.organization_id == organization_id,
            Artifact.owner_id == user_id,
            Artifact.is_deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if artifact is None:
        raise ArtifactNotFound()
    try:
        await service._authorize_context(
            db,
            IntegrationContext(
                user_id=user_id,
                organization_id=organization_id,
                project_id=artifact.project_id,
                grant_id=artifact.id,
            ),
            edit=True,
        )
    except ArtifactAccessDenied as error:
        raise ArtifactNotFound() from error


async def create_share(
    db: AsyncSession,
    *,
    user_id: UUID,
    organization_id: UUID,
    version_id: UUID,
    audience_user_ids: list[UUID],
    expires_at: datetime,
) -> ArtifactShareDTO:
    """The service owns the audience and share's single commit boundary."""
    _require_sharing()
    now = datetime.now(timezone.utc)
    if (
        not 1 <= len(audience_user_ids) <= 100
        or len(set(audience_user_ids)) != len(audience_user_ids)
        or expires_at.tzinfo is None
        or expires_at.utcoffset() is None
        or not now < expires_at <= now + MAX_SHARE_LIFETIME
    ):
        raise ArtifactInvalidShareRequest()
    version = await db.scalar(
        select(ArtifactVersion).where(
            ArtifactVersion.id == version_id, ArtifactVersion.is_deleted.is_(False)
        )
    )
    if version is None:
        raise ArtifactNotFound()
    await _authorize_creator(
        db,
        user_id=user_id,
        organization_id=organization_id,
        artifact_id=version.artifact_id,
    )
    recipients = set(
        (
            await db.scalars(
                select(User.id).where(
                    User.id.in_(audience_user_ids),
                    User.organization_id == organization_id,
                    User.is_active.is_(True),
                    User.is_deleted.is_(False),
                )
            )
        ).all()
    )
    if recipients != set(audience_user_ids):
        raise ArtifactInvalidShareRequest()
    share = ArtifactShare(
        id=uuid4(),
        organization_id=organization_id,
        creator_id=user_id,
        version_id=version_id,
        expires_at=expires_at,
        revoked_at=None,
    )
    result = _dto(share)
    try:
        db.add(share)
        # The explicit flush orders the parent before the audience FK rows.
        await db.flush()
        db.add_all(
            [
                ArtifactShareAudience(share_id=share.id, user_id=recipient)
                for recipient in audience_user_ids
            ]
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return result


async def _authorized_share(
    db: AsyncSession, *, user_id: UUID, share_id: UUID
) -> ArtifactVersion:
    row = (
        await db.execute(
            select(ArtifactShare, ArtifactVersion)
            .join(ArtifactVersion, ArtifactVersion.id == ArtifactShare.version_id)
            .join(
                ArtifactShareAudience,
                ArtifactShareAudience.share_id == ArtifactShare.id,
            )
            .join(User, User.id == ArtifactShareAudience.user_id)
            .where(
                ArtifactShare.id == share_id,
                ArtifactShare.is_deleted.is_(False),
                ArtifactShare.revoked_at.is_(None),
                ArtifactShare.expires_at > datetime.now(timezone.utc),
                ArtifactVersion.is_deleted.is_(False),
                ArtifactShareAudience.is_deleted.is_(False),
                User.id == user_id,
                User.organization_id == ArtifactShare.organization_id,
                User.is_active.is_(True),
                User.is_deleted.is_(False),
            )
            .execution_options(populate_existing=True)
        )
    ).first()
    if row is None:
        raise ArtifactNotFound()
    share, version = row
    await _authorize_creator(
        db,
        user_id=share.creator_id,
        organization_id=share.organization_id,
        artifact_id=version.artifact_id,
    )
    return cast(ArtifactVersion, version)


async def read_share(
    db: AsyncSession, *, user_id: UUID, share_id: UUID
) -> SharedArtifactVersionDTO:
    version = await _authorized_share(db, user_id=user_id, share_id=share_id)
    return SharedArtifactVersionDTO(
        artifact_id=version.artifact_id,
        version_id=version.id,
        title=version.title,
        mime_type=version.mime_type,
        byte_size=version.byte_size,
        sha256=version.sha256,
    )


async def read_share_content(
    db: AsyncSession, *, user_id: UUID, share_id: UUID
) -> tuple[bytes, str, str]:
    version = await _authorized_share(db, user_id=user_id, share_id=share_id)
    try:
        content = await service.get_artifact_storage().get(version.storage_key)
    except Exception as error:
        logger.warning("shared artifact download failed", exc_info=error)
        raise ArtifactStorageUnavailable() from error
    # Storage can await network I/O: recheck after loading before releasing bytes.
    version = await _authorized_share(db, user_id=user_id, share_id=share_id)
    if (
        content is None
        or len(content) != version.byte_size
        or hashlib.sha256(content).hexdigest() != version.sha256
    ):
        raise ArtifactStorageUnavailable()
    return content, version.mime_type, version.title


async def revoke_share(db: AsyncSession, *, user_id: UUID, share_id: UUID) -> None:
    share = await db.scalar(
        select(ArtifactShare)
        .where(
            ArtifactShare.id == share_id,
            ArtifactShare.creator_id == user_id,
            ArtifactShare.is_deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if share is None:
        raise ArtifactNotFound()
    version = await db.scalar(
        select(ArtifactVersion).where(
            ArtifactVersion.id == share.version_id,
            ArtifactVersion.is_deleted.is_(False),
        )
    )
    if version is None:
        raise ArtifactNotFound()
    await _authorize_creator(
        db,
        user_id=user_id,
        organization_id=share.organization_id,
        artifact_id=version.artifact_id,
    )
    try:
        # Repeated revocations retain the first timestamp.
        await db.execute(
            update(ArtifactShare)
            .where(ArtifactShare.id == share_id, ArtifactShare.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def list_artifact_shares(
    db: AsyncSession, *, user_id: UUID, organization_id: UUID, artifact_id: UUID
) -> list[ArtifactShareDTO]:
    await _authorize_creator(
        db, user_id=user_id, organization_id=organization_id, artifact_id=artifact_id
    )
    shares = (
        await db.scalars(
            select(ArtifactShare)
            .join(ArtifactVersion, ArtifactVersion.id == ArtifactShare.version_id)
            .where(
                ArtifactVersion.artifact_id == artifact_id,
                ArtifactVersion.is_deleted.is_(False),
                ArtifactShare.creator_id == user_id,
                ArtifactShare.organization_id == organization_id,
                ArtifactShare.is_deleted.is_(False),
            )
            .order_by(ArtifactShare.created_at.desc(), ArtifactShare.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    return [_dto(share) for share in shares]
