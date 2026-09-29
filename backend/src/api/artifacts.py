"""Artifact transport; the publication service owns authorization and commits."""

from typing import cast
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.integrations.auth import require_integration_context
from src.core.config import settings
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.artifact import (
    ArtifactAccessDenied,
    ArtifactConflict,
    ArtifactDigestMismatch,
    ArtifactError,
    ArtifactNotFound,
    ArtifactQuotaExceeded,
    ArtifactStorageUnavailable,
    ArtifactTooLarge,
    ArtifactUploadDTO,
    ArtifactVersionDTO,
    PublishVersionRequest,
    ReserveArtifactUploadRequest,
    ThreadArtifactDTO,
)
from src.schemas.integration_context import IntegrationContext
from src.services.artifacts.service import (
    MAX_ARTIFACT_BYTES,
    list_thread_artifacts,
    list_versions,
    publish_version,
    read_version_content,
    reserve_upload,
    store_upload,
)

router = APIRouter(prefix="/artifacts", tags=["artifacts"])
_PUBLISH = Depends(require_integration_context("artifacts:publish"))
_STATUS: dict[type[ArtifactError], tuple[int, str]] = {
    ArtifactNotFound: (404, "Artifact not found"),
    ArtifactAccessDenied: (403, "Integration access denied"),
    ArtifactConflict: (409, "Artifact publication conflict"),
    ArtifactDigestMismatch: (422, "Uploaded bytes do not match the reservation"),
    ArtifactTooLarge: (413, "Artifact exceeds the size limit"),
    ArtifactQuotaExceeded: (413, "Project artifact quota exceeded"),
    ArtifactStorageUnavailable: (503, "Artifact storage unavailable"),
}


def _http(error: ArtifactError) -> HTTPException:
    status, detail = _STATUS.get(type(error), (500, "Artifact request failed"))
    return HTTPException(status, detail)


def _identity(user: User) -> tuple[UUID, UUID]:
    """Current principal; the legacy ORM columns are untyped."""
    if user.organization_id is None:
        raise HTTPException(403, "Organization membership required")
    return cast(UUID, user.id), cast(UUID, user.organization_id)


def _require_publishing() -> None:
    if not settings.ARTIFACTS_ENABLED:
        raise HTTPException(503, "Artifact publication is disabled")


@router.post("/uploads", response_model=ArtifactUploadDTO)
async def reserve(
    request: ReserveArtifactUploadRequest,
    context: IntegrationContext = _PUBLISH,
    db: AsyncSession = Depends(get_db),
) -> ArtifactUploadDTO:
    _require_publishing()
    try:
        return await reserve_upload(db, context, request)
    except ArtifactError as error:
        raise _http(error) from error


@router.put("/uploads/{upload_id}/content", status_code=204)
async def upload_content(
    upload_id: UUID,
    request: Request,
    context: IntegrationContext = _PUBLISH,
    db: AsyncSession = Depends(get_db),
) -> Response:
    _require_publishing()
    # Refuse before reading the body; the service re-verifies exact size and digest.
    length = request.headers.get("content-length")
    if length is None or not length.isdigit() or int(length) > MAX_ARTIFACT_BYTES:
        raise HTTPException(413, "Artifact exceeds the size limit")
    content = await request.body()
    try:
        await store_upload(db, context, upload_id, content)
    except ArtifactError as error:
        raise _http(error) from error
    return Response(status_code=204)


@router.post("/versions", response_model=ArtifactVersionDTO)
async def publish(
    request: PublishVersionRequest,
    context: IntegrationContext = _PUBLISH,
    db: AsyncSession = Depends(get_db),
) -> ArtifactVersionDTO:
    _require_publishing()
    try:
        return await publish_version(db, context, request)
    except ArtifactError as error:
        raise _http(error) from error


@router.get("/threads/{thread_id}", response_model=list[ThreadArtifactDTO])
async def thread_artifacts(
    thread_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ThreadArtifactDTO]:
    user_id, organization_id = _identity(user)
    return await list_thread_artifacts(
        db, user_id=user_id, organization_id=organization_id, thread_id=thread_id
    )


@router.get("/{artifact_id}/versions", response_model=list[ArtifactVersionDTO])
async def artifact_versions(
    artifact_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ArtifactVersionDTO]:
    user_id, organization_id = _identity(user)
    try:
        return await list_versions(
            db,
            user_id=user_id,
            organization_id=organization_id,
            artifact_id=artifact_id,
        )
    except ArtifactError as error:
        raise _http(error) from error


@router.get("/versions/{version_id}/content")
async def version_content(
    version_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    user_id, organization_id = _identity(user)
    try:
        content, mime_type, title = await read_version_content(
            db,
            user_id=user_id,
            organization_id=organization_id,
            version_id=version_id,
        )
    except ArtifactError as error:
        raise _http(error) from error
    # ASCII fallback for the legacy parameter; the exact title goes in filename*.
    ascii_name = (
        "".join(
            c if c.isascii() and (c.isalnum() or c in "._-") else "_" for c in title
        )[:120]
        or "artifact"
    )
    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(title[:120], safe='')}"
    )
    return Response(
        content=content,
        media_type=mime_type,
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
