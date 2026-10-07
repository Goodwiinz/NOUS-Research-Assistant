"""Owner-or-admin edits of a document's metadata (title, tags, visibility).

``PUT /files/{file_id}`` used to look the document up, check ownership and
commit inline in the router. This module owns that write, so the router stays
transport only and the integration action ``update_document_metadata`` (Plan
07 Slice 3) can apply the same edit inside its own transaction.

Documents are organization scoped: the lookup filters
``Document.organization_id`` by the caller's organization and skips
soft-deleted rows, so a foreign or deleted document reads as missing. The
caller's organization is ``user.organization_id``, the same row the HTTP
dependency ``get_current_organization`` resolves.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.document import Document
from src.models.user import User, UserRole


class FileMetadataPermissionError(PermissionError):
    """The caller neither uploaded the document nor holds the admin role."""


class FileMetadataUpdateError(Exception):
    """The metadata write failed. Callers must not surface the chained cause."""


async def update_file_metadata(
    db: AsyncSession,
    file_id: UUID,
    user: User,
    *,
    title: str | None = None,
    tags: list[str] | None = None,
    is_public: bool | None = None,
    commit: bool = True,
) -> Document | None:
    """Set each given (non-``None``) field on one of the caller's documents.

    Returns ``None`` when no live document with ``file_id`` belongs to the
    caller's organization. Raises ``FileMetadataPermissionError``, before
    anything changes, unless ``user`` uploaded the document or is an admin.

    ``commit=True`` (HTTP callers): this call is the transaction boundary. It
    commits and refreshes; on failure it rolls back and raises
    ``FileMetadataUpdateError`` chained to the cause.
    ``commit=False`` (callers that own a wider transaction): it only flushes.
    On failure it raises ``FileMetadataUpdateError`` and leaves the rollback
    to the caller; it never commits or rolls back the caller's transaction.
    """
    if user.organization_id is None:
        # Fail closed: no organization, no documents. HTTP callers never get
        # here (get_current_organization answers 404 first).
        return None
    result = await db.execute(
        select(Document).where(
            Document.id == file_id,
            Document.organization_id == user.organization_id,
            Document.is_deleted == False,  # noqa: E712
        )
    )
    document: Document | None = result.scalars().first()
    if document is None:
        return None
    if document.uploaded_by_user_id != user.id and not user.has_permission(
        UserRole.ADMIN
    ):
        raise FileMetadataPermissionError(
            "only the uploader or an admin may edit this document"
        )

    try:
        # Document declares legacy Column() attributes, which mypy types as
        # Column[...] on instances; the ignores cover only that.
        if title is not None:
            document.title = title  # type: ignore[assignment]
        if tags is not None:
            document.tags = tags  # type: ignore[assignment]
        if is_public is not None:
            document.is_public = is_public  # type: ignore[assignment]
        if commit:
            await db.commit()
        else:
            await db.flush()
        await db.refresh(document)
    except Exception as error:
        if commit:
            await db.rollback()
        raise FileMetadataUpdateError("file metadata update failed") from error
    return document
