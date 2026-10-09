"""Reserve, upload and publish one artifact version through the service layer.

Shared by the artifact unit suite and the PostgreSQL same-project journey.
Callers own storage (monkeypatch ``service.get_artifact_storage``).
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from src.schemas.artifact import (
    ArtifactProvenance,
    ArtifactVersionDTO,
    PublishVersionRequest,
    ReserveArtifactUploadRequest,
)
from src.schemas.integration_context import IntegrationContext
from src.services.artifacts.service import publish_version, reserve_upload, store_upload

DEFAULT_CONTENT = b"report\n"


def reserve_request(
    publication_id: UUID | None = None,
    *,
    content: bytes = DEFAULT_CONTENT,
    **overrides: Any,
) -> ReserveArtifactUploadRequest:
    return ReserveArtifactUploadRequest(
        publication_id=publication_id or uuid4(),
        byte_size=overrides.pop("byte_size", len(content)),
        mime_type=overrides.pop("mime_type", "text/markdown"),
        sha256=overrides.pop("sha256", hashlib.sha256(content).hexdigest()),
    )


def publish_request(
    reserve: ReserveArtifactUploadRequest, upload_id: UUID, **overrides: Any
) -> PublishVersionRequest:
    return PublishVersionRequest(
        publication_id=reserve.publication_id,
        upload_id=upload_id,
        title=overrides.pop("title", "report.md"),
        provenance=overrides.pop("provenance", ArtifactProvenance(producer="harness")),
        **overrides,
    )


async def publish_content(
    db: AsyncSession,
    context: IntegrationContext,
    *,
    content: bytes = DEFAULT_CONTENT,
    mime_type: str = "text/markdown",
    **overrides: Any,
) -> tuple[PublishVersionRequest, ArtifactVersionDTO]:
    """A fresh publication (new ``publication_id`` and reservation) of
    ``content``; ``overrides`` go to the publish request (title, artifact_id,
    expected_parent_version_id, ...)."""
    reserve = reserve_request(content=content, mime_type=mime_type)
    upload = await reserve_upload(db, context, reserve)
    await store_upload(db, context, upload.upload_id, content)
    request = publish_request(reserve, upload.upload_id, **overrides)
    return request, await publish_version(db, context, request)
