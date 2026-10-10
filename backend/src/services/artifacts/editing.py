"""Browser-owned text edits through the immutable publication protocol.

The server derives receipt authority from actor and artifact, never from an
external grant. Existing reservation, digest and parent compare-and-swap guards
keep retries and concurrent edits safe without an additional persistence path.
"""

from __future__ import annotations

import hashlib
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.artifact import ArtifactVersion
from src.schemas.artifact import (
    ArtifactConflict,
    ArtifactDigestMismatch,
    ArtifactNotFound,
    ArtifactProvenance,
    ArtifactTooLarge,
    ArtifactVersionDTO,
    PublishVersionRequest,
    ReserveArtifactUploadRequest,
)
from src.schemas.integration_context import IntegrationContext
from src.services.artifacts.service import (
    _authorize_context,
    _existing_reservation,
    authorize_artifact,
    publish_version,
    reserve_upload,
    store_upload,
)

MAX_EDIT_BYTES = 2 * 1024 * 1024
_EDITABLE_MIMES = frozenset(
    {
        "application/json",
        "application/javascript",
        "application/xml",
        "application/x-python",
    }
)


async def edit_version(
    db: AsyncSession,
    *,
    user_id: UUID,
    organization_id: UUID,
    artifact_id: UUID,
    expected_parent_version_id: UUID,
    publication_id: UUID,
    text: str,
) -> ArtifactVersionDTO:
    artifact = await authorize_artifact(
        db,
        user_id=user_id,
        organization_id=organization_id,
        artifact_id=artifact_id,
        action="edit",
    )
    parent = await db.scalar(
        select(ArtifactVersion).where(
            ArtifactVersion.id == expected_parent_version_id,
            ArtifactVersion.artifact_id == artifact_id,
            ArtifactVersion.is_deleted.is_(False),
        )
    )
    if parent is None:
        raise ArtifactNotFound()
    mime_type, title, thread_id = parent.mime_type, parent.title, parent.thread_id
    if not (mime_type.startswith("text/") or mime_type in _EDITABLE_MIMES):
        raise ArtifactDigestMismatch()
    try:
        content = text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ArtifactDigestMismatch() from error
    if len(content) > MAX_EDIT_BYTES:
        raise ArtifactTooLarge()
    context = IntegrationContext(
        user_id=user_id,
        organization_id=organization_id,
        project_id=artifact.project_id,
        thread_id=thread_id,
        grant_id=uuid5(
            NAMESPACE_URL, f"nous:browser-artifact-edit:{user_id}:{artifact_id}"
        ),
    )
    # reserve_upload enforces Workspace.can_user_edit on every invocation.
    # An identical already-committed retry must return its stored version
    # even if a later edit advanced the pointer, so only a fresh publication
    # of a stale parent is rejected here, before it reserves quota or stores
    # bytes. A parent that goes stale after this check still loses in
    # publish_version.
    try:
        if artifact.current_version_id != expected_parent_version_id:
            await _authorize_context(db, context, edit=True)
            if await _existing_reservation(db, context, publication_id) is None:
                raise ArtifactConflict()
        reservation = await reserve_upload(
            db,
            context,
            ReserveArtifactUploadRequest(
                publication_id=publication_id,
                byte_size=len(content),
                mime_type=mime_type,
                sha256=hashlib.sha256(content).hexdigest(),
            ),
        )
        await store_upload(db, context, reservation.upload_id, content)
        return await publish_version(
            db,
            context,
            PublishVersionRequest(
                publication_id=publication_id,
                upload_id=reservation.upload_id,
                artifact_id=artifact_id,
                expected_parent_version_id=expected_parent_version_id,
                title=title,
                provenance=ArtifactProvenance(producer="user"),
            ),
        )
    except ArtifactConflict as error:
        # A competing writer may have committed after our initial read.
        # Refresh through authorization before exposing the latest identity.
        await db.rollback()
        current = await authorize_artifact(
            db,
            user_id=user_id,
            organization_id=organization_id,
            artifact_id=artifact_id,
            action="read",
        )
        await db.refresh(current)
        error.current_version_id = current.current_version_id
        raise
