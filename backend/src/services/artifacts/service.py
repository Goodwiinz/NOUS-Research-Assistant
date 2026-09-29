"""Artifact publication: reserve, store, finalize, read.

Every method owns one transaction. Identity and scope come from the
authorized context, never from request payloads. Bytes are verified against
the reservation before any metadata commits, and a repeated finalize returns
the same version.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.artifact import (
    Artifact,
    ArtifactReference,
    ArtifactUpload,
    ArtifactVersion,
)
from src.models.collection import Collection
from src.models.workspace import Workspace
from src.schemas.artifact import (
    ArtifactAccessDenied,
    ArtifactConflict,
    ArtifactDigestMismatch,
    ArtifactNotFound,
    ArtifactProvenance,
    ArtifactQuotaExceeded,
    ArtifactReferenceDTO,
    ArtifactStorageUnavailable,
    ArtifactTooLarge,
    ArtifactUploadDTO,
    ArtifactVersionDTO,
    PublishVersionRequest,
    ReserveArtifactUploadRequest,
    ThreadArtifactDTO,
)
from src.schemas.integration_context import IntegrationContext
from src.services.artifacts.storage import get_artifact_storage
from src.services.integrations.context import (
    IntegrationAccessDenied,
    authorized_project,
)

logger = logging.getLogger(__name__)

MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
PROJECT_QUOTA_BYTES = 100 * 1024 * 1024
UPLOAD_TTL = timedelta(minutes=15)
# Previewable or safely downloadable types; anything else is served as octet-stream.
ALLOWED_MIME_TYPES = frozenset(
    {
        "text/markdown",
        "text/plain",
        "text/csv",
        "text/html",
        "text/x-python",
        "application/json",
        "application/javascript",
        "image/png",
        "image/jpeg",
        "application/pdf",
    }
)
ArtifactAction = Literal["read", "edit", "share"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    # SQLite drops tzinfo on round-trip; PostgreSQL keeps it.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def _normalize_mime(mime_type: str) -> str:
    base = mime_type.split(";", 1)[0].strip().lower()
    return base if base in ALLOWED_MIME_TYPES else "application/octet-stream"


async def _authorize_context(db: AsyncSession, context: IntegrationContext) -> None:
    try:
        await authorized_project(
            db, context.user_id, context.organization_id, context.project_id
        )
    except IntegrationAccessDenied as error:
        raise ArtifactAccessDenied() from error


async def _upload_for(
    db: AsyncSession, context: IntegrationContext, upload_id: UUID
) -> ArtifactUpload:
    upload = await db.scalar(
        select(ArtifactUpload).where(
            ArtifactUpload.id == upload_id,
            ArtifactUpload.grant_id == context.grant_id,
            ArtifactUpload.organization_id == context.organization_id,
            ArtifactUpload.project_id == context.project_id,
        )
    )
    if upload is None:
        raise ArtifactNotFound()
    return upload


def _version_dto(version: ArtifactVersion) -> ArtifactVersionDTO:
    return ArtifactVersionDTO(
        artifact_id=version.artifact_id,
        version_id=version.id,
        parent_version_id=version.parent_version_id,
        title=version.title,
        mime_type=version.mime_type,
        byte_size=version.byte_size,
        sha256=version.sha256,
        created_at=_aware(version.created_at),
        provenance=ArtifactProvenance.model_validate(version.provenance),
    )


async def reserve_upload(
    db: AsyncSession, context: IntegrationContext, request: ReserveArtifactUploadRequest
) -> ArtifactUploadDTO:
    await _authorize_context(db, context)
    if request.byte_size > MAX_ARTIFACT_BYTES:
        raise ArtifactTooLarge()
    request_hash = _hash(request.model_dump(mode="json"))
    existing = await db.scalar(
        select(ArtifactUpload).where(
            ArtifactUpload.grant_id == context.grant_id,
            ArtifactUpload.publication_id == request.publication_id,
        )
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise ArtifactConflict()
        return ArtifactUploadDTO(
            upload_id=existing.id, expires_at=_aware(existing.expires_at)
        )
    # Quota counts committed versions plus reservations that could still land.
    now = _now()
    committed = await db.scalar(
        select(func.coalesce(func.sum(ArtifactVersion.byte_size), 0))
        .join(Artifact, Artifact.id == ArtifactVersion.artifact_id)
        .where(
            Artifact.project_id == context.project_id, Artifact.is_deleted.is_(False)
        )
    )
    reserved = await db.scalar(
        select(func.coalesce(func.sum(ArtifactUpload.byte_size), 0)).where(
            ArtifactUpload.project_id == context.project_id,
            ArtifactUpload.version_id.is_(None),
            or_(ArtifactUpload.expires_at > now, ArtifactUpload.stored_at.is_not(None)),
        )
    )
    if (
        int(committed or 0) + int(reserved or 0) + request.byte_size
        > PROJECT_QUOTA_BYTES
    ):
        raise ArtifactQuotaExceeded()
    upload = ArtifactUpload(
        id=uuid4(),
        organization_id=context.organization_id,
        project_id=context.project_id,
        grant_id=context.grant_id,
        publication_id=request.publication_id,
        byte_size=request.byte_size,
        mime_type=_normalize_mime(request.mime_type),
        sha256=request.sha256,
        request_hash=request_hash,
        expires_at=now + UPLOAD_TTL,
    )
    db.add(upload)
    await db.commit()
    return ArtifactUploadDTO(upload_id=upload.id, expires_at=upload.expires_at)


async def store_upload(
    db: AsyncSession, context: IntegrationContext, upload_id: UUID, content: bytes
) -> None:
    await _authorize_context(db, context)
    upload = await _upload_for(db, context, upload_id)
    if (
        len(content) != upload.byte_size
        or hashlib.sha256(content).hexdigest() != upload.sha256
    ):
        raise ArtifactDigestMismatch()
    if upload.stored_at is not None:
        return
    if _aware(upload.expires_at) <= _now():
        raise ArtifactConflict()
    key = f"artifacts/{context.organization_id}/uploads/{upload.id}"
    try:
        await get_artifact_storage().put(key, content, upload.mime_type)
    except Exception as error:  # noqa: BLE001 - storage backends raise arbitrary errors
        logger.warning("artifact upload storage failed", exc_info=error)
        raise ArtifactStorageUnavailable() from error
    upload.storage_key = key
    upload.stored_at = _now()
    await db.commit()


async def publish_version(
    db: AsyncSession, context: IntegrationContext, request: PublishVersionRequest
) -> ArtifactVersionDTO:
    await _authorize_context(db, context)
    upload = await _upload_for(db, context, request.upload_id)
    if upload.publication_id != request.publication_id or upload.storage_key is None:
        raise ArtifactNotFound()
    publish_hash = _hash(request.model_dump(mode="json"))
    if upload.version_id is not None:
        if upload.publish_hash != publish_hash:
            raise ArtifactConflict()
        version = await db.get(ArtifactVersion, upload.version_id)
        if version is None:
            raise ArtifactNotFound()
        return _version_dto(version)
    if request.artifact_id is not None:
        artifact = await db.scalar(
            select(Artifact).where(
                Artifact.id == request.artifact_id,
                Artifact.organization_id == context.organization_id,
                Artifact.project_id == context.project_id,
                Artifact.is_deleted.is_(False),
            )
        )
        if artifact is None:
            raise ArtifactNotFound()
        if artifact.current_version_id != request.expected_parent_version_id:
            raise ArtifactConflict()
    else:
        if request.expected_parent_version_id is not None:
            raise ArtifactConflict()
        artifact = Artifact(
            id=uuid4(),
            organization_id=context.organization_id,
            project_id=context.project_id,
            owner_id=context.user_id,
            title=request.title,
        )
        db.add(artifact)
    # Verify the blob before any metadata commits; a missing object keeps the
    # current pointer unchanged.
    if not await get_artifact_storage().exists(upload.storage_key):
        await db.rollback()
        raise ArtifactStorageUnavailable()
    version = ArtifactVersion(
        id=uuid4(),
        artifact_id=artifact.id,
        parent_version_id=artifact.current_version_id,
        upload_id=upload.id,
        title=request.title,
        mime_type=upload.mime_type,
        byte_size=upload.byte_size,
        sha256=upload.sha256,
        storage_key=upload.storage_key,
        producer=request.provenance.producer,
        provenance=request.provenance.model_dump(mode="json"),
        run_id=str(context.run_id) if context.run_id else None,
        thread_id=context.thread_id,
        grant_id=context.grant_id,
    )
    db.add(version)
    db.add(
        ArtifactReference(
            id=uuid4(),
            artifact_id=artifact.id,
            version_id=version.id,
            run_id=version.run_id,
            thread_id=context.thread_id,
        )
    )
    artifact.current_version_id = version.id
    artifact.title = request.title
    upload.version_id = version.id
    upload.publish_hash = publish_hash
    await db.commit()
    return _version_dto(version)


async def authorize_artifact(
    db: AsyncSession,
    *,
    user_id: UUID,
    organization_id: UUID,
    artifact_id: UUID,
    action: ArtifactAction,
) -> Artifact:
    """Current-principal access: same org, live ancestors, project owner or member."""
    artifact = await db.scalar(
        select(Artifact).where(
            Artifact.id == artifact_id,
            Artifact.organization_id == organization_id,
            Artifact.is_deleted.is_(False),
        )
    )
    if artifact is None:
        raise ArtifactNotFound()
    try:
        await authorized_project(db, user_id, organization_id, artifact.project_id)
    except IntegrationAccessDenied as error:
        raise ArtifactNotFound() from error
    return artifact


async def list_thread_artifacts(
    db: AsyncSession, *, user_id: UUID, organization_id: UUID, thread_id: UUID
) -> list[ThreadArtifactDTO]:
    rows = (
        await db.execute(
            select(ArtifactVersion, ArtifactReference)
            .join(ArtifactReference, ArtifactReference.version_id == ArtifactVersion.id)
            .join(Artifact, Artifact.id == ArtifactVersion.artifact_id)
            .join(Collection, Collection.id == Artifact.project_id)
            .join(Workspace, Workspace.id == Collection.workspace_id)
            .where(
                ArtifactReference.thread_id == thread_id,
                Artifact.organization_id == organization_id,
                Artifact.is_deleted.is_(False),
                ArtifactVersion.is_deleted.is_(False),
                Collection.is_deleted.is_(False),
                Workspace.is_deleted.is_(False),
            )
            .order_by(ArtifactVersion.created_at, ArtifactVersion.id)
        )
    ).all()
    items: list[ThreadArtifactDTO] = []
    checked: dict[UUID, bool] = {}
    for version, reference in rows:
        project_id = (await db.get(Artifact, version.artifact_id)).project_id  # type: ignore[union-attr]
        if project_id not in checked:
            try:
                await authorized_project(db, user_id, organization_id, project_id)
                checked[project_id] = True
            except IntegrationAccessDenied:
                checked[project_id] = False
        if not checked[project_id]:
            continue
        items.append(
            ThreadArtifactDTO(
                version=_version_dto(version),
                reference=ArtifactReferenceDTO(
                    artifact_id=reference.artifact_id,
                    version_id=reference.version_id,
                    run_id=UUID(reference.run_id) if reference.run_id else None,
                    thread_id=reference.thread_id,
                    message_id=reference.message_id,
                ),
            )
        )
    return items


async def list_versions(
    db: AsyncSession, *, user_id: UUID, organization_id: UUID, artifact_id: UUID
) -> list[ArtifactVersionDTO]:
    await authorize_artifact(
        db,
        user_id=user_id,
        organization_id=organization_id,
        artifact_id=artifact_id,
        action="read",
    )
    versions = (
        await db.scalars(
            select(ArtifactVersion)
            .where(
                ArtifactVersion.artifact_id == artifact_id,
                ArtifactVersion.is_deleted.is_(False),
            )
            .order_by(ArtifactVersion.created_at, ArtifactVersion.id)
        )
    ).all()
    return [_version_dto(version) for version in versions]


async def read_version_content(
    db: AsyncSession, *, user_id: UUID, organization_id: UUID, version_id: UUID
) -> tuple[bytes, str, str]:
    version = await db.scalar(
        select(ArtifactVersion).where(
            ArtifactVersion.id == version_id, ArtifactVersion.is_deleted.is_(False)
        )
    )
    if version is None:
        raise ArtifactNotFound()
    await authorize_artifact(
        db,
        user_id=user_id,
        organization_id=organization_id,
        artifact_id=version.artifact_id,
        action="read",
    )
    try:
        content = await get_artifact_storage().get(version.storage_key)
    except Exception as error:  # noqa: BLE001
        logger.warning("artifact download failed", exc_info=error)
        raise ArtifactStorageUnavailable() from error
    if content is None or hashlib.sha256(content).hexdigest() != version.sha256:
        raise ArtifactStorageUnavailable()
    return content, version.mime_type, version.title
