"""Canonical storage and document references for alphaXiv-shaped uploads.

URLs are identifiers only. This module never fetches a URL supplied by a client.
FileService remains the storage, quota, deduplication and ingestion job owner.
"""

import logging
import re
from collections.abc import AsyncIterator, Sequence
from pathlib import PurePath
from tempfile import SpooledTemporaryFile
from urllib.parse import unquote, urlsplit
from uuid import UUID

from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload
from starlette.datastructures import Headers

from src.core.config import settings
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.organization import Organization
from src.models.user import User
from src.schemas.alphaxiv_files import (
    AlphaXivFileReference,
    AlphaXivUploadedFile,
    AlphaXivUploadResponse,
)
from src.services.compat.alphaxiv import CompatibilityError
from src.services.documents.file_service import (
    FileService,
    FileStorageError,
    FileValidationError,
)
from src.services.threads.workspace_access import filter_owned_document_ids
from src.shared.enums import ApiDocumentStatus

logger = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
_REFERENCE = re.compile(r"/api/v1/files/([0-9a-f-]{36})/download")
_TEXT_MIMES = {"text/plain", "text/markdown"}


def file_url(document_id: UUID) -> str:
    return f"/api/v1/files/{document_id}/download"


def reference_id(url: str) -> UUID:
    """Accept canonical relative refs or exactly the configured public base."""
    base = settings.ALPHAXIV_FILE_BASE_URL.rstrip("/")
    if base and url.startswith(base + "/"):
        parsed = urlsplit(base)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise CompatibilityError(503, "The file reference base is not configured")
        url = url[len(base) :]
    matched = _REFERENCE.fullmatch(url)
    if matched is None:
        raise CompatibilityError(422, "files must reference canonical NOUS uploads")
    try:
        identifier = UUID(matched.group(1))
    except ValueError as exc:
        raise CompatibilityError(422, "Invalid file reference") from exc
    if file_url(identifier) != url:
        raise CompatibilityError(422, "Invalid file reference")
    return identifier


def document_content_type(document: Document) -> str:
    mime = (document.mime_type or "").lower().split(";", 1)[0].strip()
    if document.document_type == DocumentType.PDF and mime == "application/pdf":
        return mime
    if document.document_type == DocumentType.TEXT and mime in _TEXT_MIMES:
        return mime
    raise CompatibilityError(
        422, "Only PDF, plain text and Markdown files are supported"
    )


async def authorize_files(
    db: AsyncSession, *, user: User, document_ids: Sequence[UUID]
) -> list[Document]:
    """Re-check live organization ownership and readiness on every use/read."""
    if not document_ids:
        return []
    owned = await filter_owned_document_ids(db, document_ids, user.id)
    if set(owned) != set(document_ids):
        raise CompatibilityError(404, "File not found")
    # History/replay need metadata only. Never materialize extracted text for
    # each attachment in a large conversation just to test whether it exists.
    rows = (
        await db.execute(
            select(
                Document, func.length(func.trim(Document.content_text, " \t\r\n\v\f"))
            )
            .where(
                Document.id.in_(document_ids),
                Document.organization_id == user.organization_id,
                Document.is_deleted.is_(False),
            )
            .options(
                load_only(
                    Document.id,
                    Document.title,
                    Document.mime_type,
                    Document.document_type,
                    Document.processing_status,
                    Document.organization_id,
                    Document.is_deleted,
                )
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    by_id = {document.id: (document, text_length) for document, text_length in rows}
    documents: list[Document] = []
    for document_id in document_ids:
        if document_id not in by_id:
            raise CompatibilityError(404, "File not found")
        document, text_length = by_id[document_id]
        document_content_type(document)
        if document.processing_status != ProcessingStatus.COMPLETED:
            raise CompatibilityError(409, "File processing is not complete")
        if not text_length:
            raise CompatibilityError(409, "File text is unavailable")
        documents.append(document)
    return documents


async def normalize_files(
    db: AsyncSession,
    *,
    user: User,
    paper_id: UUID,
    files: Sequence[AlphaXivFileReference],
) -> tuple[list[UUID], list[AlphaXivFileReference]]:
    if len(files) > 9:
        raise CompatibilityError(422, "At most nine files may accompany the PDF")
    ids = [reference_id(item.url) for item in files]
    if paper_id in ids or len(set(ids)) != len(ids):
        raise CompatibilityError(
            422, "File references must be unique and exclude the paper"
        )
    documents = await authorize_files(db, user=user, document_ids=ids)
    normalized: list[AlphaXivFileReference] = []
    for reference, document in zip(files, documents):
        mime = document_content_type(document)
        supplied = reference.content_type.lower().split(";", 1)[0].strip()
        # Browser File.type may be empty, and Markdown commonly sniffs as text/plain.
        if supplied and supplied != mime and not ({supplied, mime} <= _TEXT_MIMES):
            raise CompatibilityError(
                422, "File contentType does not match the stored file"
            )
        normalized.append(
            AlphaXivFileReference(content_type=mime, url=file_url(document.id))
        )
    return ids, normalized


def decode_filename(encoded: str) -> str:
    if not encoded or len(encoded) > 4500 or re.search(r"%(?![0-9a-fA-F]{2})", encoded):
        raise CompatibilityError(400, "Invalid filename")
    try:
        filename = unquote(encoded, encoding="utf-8", errors="strict")
    except UnicodeError as exc:
        raise CompatibilityError(400, "Invalid filename") from exc
    if (
        not filename.strip()
        or len(filename) > 500
        or filename in {".", ".."}
        or any(char in filename for char in "/\\")
        or any(ord(char) < 32 or ord(char) == 127 for char in filename)
    ):
        raise CompatibilityError(400, "Invalid filename")
    if PurePath(filename).suffix.lower() not in {".pdf", ".txt", ".md"}:
        raise CompatibilityError(
            400, "Only PDF, plain text and Markdown uploads are supported"
        )
    return filename


async def _upload_actor(db: AsyncSession, actor_id: UUID) -> User:
    actor: User | None = (
        await db.execute(
            select(User)
            .join(Organization, User.organization_id == Organization.id)
            .where(
                User.id == actor_id,
                User.is_active.is_(True),
                User.is_deleted.is_(False),
                Organization.is_active.is_(True),
                Organization.is_deleted.is_(False),
            )
            .options(selectinload(User.organization))
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if actor is None:
        raise CompatibilityError(404, "Organization not found")
    if not actor.can_upload_documents():
        raise CompatibilityError(403, "Document upload permission is required")
    return actor


async def upload_file(
    db: AsyncSession,
    *,
    actor_id: UUID,
    encoded_filename: str,
    content_type: str,
    content_length: int | None,
    chunks: AsyncIterator[bytes],
) -> AlphaXivUploadResponse:
    filename = decode_filename(encoded_filename)
    expected = "application/pdf" if filename.lower().endswith(".pdf") else "text/plain"
    supplied = content_type.lower().split(";", 1)[0].strip()
    if supplied and supplied != expected and not ({supplied, expected} <= _TEXT_MIMES):
        raise CompatibilityError(400, "File content type does not match the filename")
    actor = await _upload_actor(db, actor_id)
    original_organization_id = actor.organization_id
    organization = actor.organization
    limit = min(MAX_UPLOAD_BYTES, organization.max_file_size_bytes)
    if content_length is not None and (content_length < 0 or content_length > limit):
        raise CompatibilityError(413, "File exceeds the maximum allowed size")
    service = FileService(db)
    spool = SpooledTemporaryFile[bytes](max_size=1024 * 1024, mode="w+b")
    upload = UploadFile(
        file=spool, filename=filename, headers=Headers({"content-type": expected})
    )
    try:
        size = 0
        async for chunk in chunks:
            size += len(chunk)
            if size > limit:
                raise CompatibilityError(413, "File exceeds the maximum allowed size")
            await upload.write(chunk)
        if not size or (content_length is not None and content_length != size):
            raise CompatibilityError(400, "Invalid file data")
        await upload.seek(0)
        upload.size = size
        # Reading a slow body is an authorization boundary: permissions and
        # organization membership may have changed while awaiting the client.
        actor = await _upload_actor(db, actor_id)
        if actor.organization_id != original_organization_id:
            raise CompatibilityError(404, "Organization not found")
        organization = actor.organization
        await db.refresh(organization)
        if size > min(MAX_UPLOAD_BYTES, organization.max_file_size_bytes):
            raise CompatibilityError(413, "File exceeds the maximum allowed size")
        validation = service.validate_file(upload, actor, organization)
        sniffed = validation["mime_type"]
        if sniffed != expected and not ({sniffed, expected} <= _TEXT_MIMES):
            raise CompatibilityError(
                400, "File signature does not match its content type"
            )
        document = await service.upload_file(
            file=upload,
            title=filename,
            user=actor,
            organization=organization,
            tags=[],
            is_public=False,
        )
        return AlphaXivUploadResponse(
            data=AlphaXivUploadedFile(
                url=file_url(document.id).lstrip("/"),
                document_id=document.id,
                processing_status=ApiDocumentStatus.from_db(document.processing_status),
            )
        )
    except FileValidationError as exc:
        logger.warning("Compatibility upload validation failed", exc_info=True)
        reason = exc.public_detail
        if reason in {
            "File exceeds the maximum allowed size",
            "Insufficient storage quota",
        }:
            raise CompatibilityError(413, reason) from exc
        raise CompatibilityError(400, "File validation failed") from exc
    except HTTPException as exc:
        # Preserve the native duplicate result, never arbitrary internal detail.
        if exc.status_code == 409:
            raise CompatibilityError(
                409, "Identical file already exists in this organization"
            ) from exc
        # The native detail may carry internal text; log it, answer generically.
        if exc.status_code >= 500:
            logger.error(
                "Compatibility upload failed in FileService (status=%s)",
                exc.status_code,
                exc_info=True,
            )
            raise CompatibilityError(503, "File upload failed") from exc
        logger.warning(
            "Compatibility upload rejected by FileService (status=%s)",
            exc.status_code,
            exc_info=True,
        )
        raise CompatibilityError(400, "File upload failed") from exc
    except FileStorageError as exc:
        logger.exception("Compatibility file storage failed")
        raise CompatibilityError(503, "File upload failed") from exc
    finally:
        await upload.close()
