"""
File upload and management API endpoints
"""

import logging
import os
import uuid
from datetime import datetime
from typing import AsyncIterator, List, Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.core.database import get_db
from src.core.dependencies import (
    can_upload_documents,
    get_current_organization,
    get_current_user,
    has_storage_quota,
)
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.organization import Organization
from src.models.processing import JobStatus, ProcessingJob
from src.models.user import User, UserRole
from src.services.documents.file_service import (
    FileService,
    FileStorageError,
    FileValidationError,
    get_file_service,
)
from src.shared.enums import ApiDocumentStatus
from src.shared.pagination import Page, PaginationParams

router = APIRouter(prefix="/files", tags=["files"])


# Module-level logger: the except handlers in list_files / get_file_statistics /
# cancel_upload reference `logger`; before this it existed only as a local inside
# upload_file, so those error paths raised NameError and masked the real error.
logger = logging.getLogger(__name__)


def _escape_like(value: str) -> str:
    """Escape SQL LIKE special characters."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _list_files_filters(
    organization: Organization,
    document_type: Optional[DocumentType],
    processing_status: Optional[str],
    search: Optional[str],
) -> List[ColumnElement]:
    """Build the WHERE clause for the file list endpoint.

    Repo rule (audit C10 / count-filter-drift): the count query and the result
    query MUST apply the *same* filters. Both route through this single helper
    so a predicate added to one can never silently drift from the other — a
    drifted count over-reports ``total`` and manufactures phantom "next" pages.

    Raises ``HTTPException(400)`` for an unknown ``processing_status`` (kept
    here so count + results reject identically).
    """
    conditions: List[ColumnElement] = [
        Document.organization_id == organization.id,
        Document.is_deleted == False,  # noqa: E712
    ]

    if document_type:
        conditions.append(Document.document_type == document_type)

    if processing_status:
        # This router emits public status names ('queued'/'indexed') in its
        # upload response, so a client filtering by what it received sends those
        # back. Comparing them raw against the ProcessingStatus enum column
        # raised LookupError -> 500. Translate via the shared vocabulary (single
        # source of truth), keeping the validated-enum injection-prevention
        # pattern — only known values reach the query.
        try:
            mapped = ApiDocumentStatus.to_db(processing_status)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid processing_status: {processing_status}",
            )
        conditions.append(Document.processing_status == mapped)

    if search:
        escaped_search = _escape_like(search)
        conditions.append(
            or_(
                Document.title.ilike(f"%{escaped_search}%"),
                Document.filename.ilike(f"%{escaped_search}%"),
            )
        )

    return conditions


# Request/Response Models
class FileUploadResponse(BaseModel):
    document_id: str
    upload_id: str  # Same as document_id for v1 API compatibility
    id: str  # Deprecated, use document_id
    title: str
    filename: str
    document_type: str
    file_size_bytes: int
    file_size_mb: float
    mime_type: str
    processing_status: ApiDocumentStatus
    upload_timestamp: str
    created_at: str
    message: str
    upload_progress: int = 100  # v1 API doesn't support progress tracking


class FileListResponse(BaseModel):
    # Wire-compatible shape (key ``files``, not ``items``): kept stable for
    # existing consumers. ``has_more`` is additive and derived from ``total`` +
    # the returned slice via ``Page.create`` — never hand-set, so it can't claim
    # a page that doesn't exist.
    files: List[dict]
    total: int
    page: int
    size: int
    has_more: bool


class FileStatsResponse(BaseModel):
    files_by_type: List[dict]
    processing_stats: List[dict]


@router.post("/upload", response_model=FileUploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    title: str = Form(...),
    description: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    is_public: bool = Form(False),
    processing_priority: Optional[str] = Form("normal"),
    enable_quality_check: bool = Form(True),
    custom_metadata: Optional[str] = Form(None),
    current_user: User = Depends(
        get_current_user
    ),  # Temporarily reduced permission check
    db: AsyncSession = Depends(get_db),
    file_service: FileService = Depends(get_file_service),
):
    """Upload a file to the system"""

    # Debug logging
    logger.info(f"📤 Upload Request Debug:")
    logger.info(f"  - User ID: {current_user.id}")
    logger.info(f"  - User Email: {current_user.email}")
    logger.info(
        f"  - User Role: {current_user.role.value if current_user.role else 'None'}"
    )
    logger.info(f"  - User Active: {current_user.is_active}")
    logger.info(f"  - Can Upload: {current_user.can_upload_documents()}")
    logger.info(f"  - Title: {title}")
    logger.info(f"  - Description: {description}")
    logger.info(f"  - Tags: {tags}")
    logger.info(f"  - Processing Priority: {processing_priority}")
    logger.info(f"  - Enable Quality Check: {enable_quality_check}")

    try:
        # Get user's organization
        from src.core.dependencies import get_current_organization

        organization = get_current_organization(current_user)
        logger.info(
            f"  - Organization ID: {organization.id if organization else 'None'}"
        )
        logger.info(
            f"  - Organization Name: {organization.name if organization else 'None'}"
        )

        # Manual permission check for debugging
        if not current_user.can_upload_documents():
            logger.error(
                f"❌ Permission check failed! User {current_user.email} cannot upload documents"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. User role: {current_user.role.value if current_user.role else 'None'}, Required: USER or higher",
            )

        logger.info(f"✅ Permission check passed!")

        # Get file size for quota check
        if hasattr(file, "size") and file.size:
            file_size = file.size
        else:
            # Read content to get size if not available
            file.file.seek(0, 2)  # Seek to end
            file_size = file.file.tell()
            file.file.seek(0)  # Reset position

        # Check storage quota
        if not organization.can_upload_file(file_size):
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Insufficient storage quota. Available: {organization.storage_available_gb:.2f}GB",
            )

        # Parse tags from comma-separated string
        tag_list = []
        if tags:
            tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()]

        # Upload file
        document = await file_service.upload_file(
            file=file,
            title=title,
            user=current_user,
            organization=organization,
            tags=tag_list,
            is_public=is_public,
        )

        # Map backend status to the public API vocabulary
        frontend_status = ApiDocumentStatus.from_db(document.processing_status)

        return FileUploadResponse(
            document_id=str(document.id),
            upload_id=str(document.id),  # Same as document_id for v1 API
            id=str(document.id),  # Deprecated, maintain compatibility
            title=document.title,
            filename=document.filename,
            document_type=document.document_type.value,
            file_size_bytes=document.file_size_bytes,
            file_size_mb=document.file_size_mb,
            mime_type=document.mime_type or "application/octet-stream",
            processing_status=frontend_status,  # Use frontend-compatible lowercase status
            upload_timestamp=document.created_at.isoformat(),
            created_at=document.created_at.isoformat(),
            message="File uploaded successfully",
            upload_progress=100,
        )

    except HTTPException:
        # R2-M10: intentional 4xx (validation/quota) must not be re-wrapped.
        raise
    except FileValidationError as e:
        # FileService validation messages are intentionally safe and actionable.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except FileStorageError:
        logger.error("File upload failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File upload failed",
        )
    except Exception:
        logger.error("Unexpected file upload failure", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File upload failed",
        )


@router.get("/", response_model=FileListResponse)
async def list_files(
    pagination: PaginationParams = Depends(),
    document_type: Optional[DocumentType] = None,
    processing_status: Optional[str] = None,
    search: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """List files in the organization.

    Pagination is validated by the shared ``PaginationParams`` dependency
    (``page >= 1``, ``1 <= size <= 100`` — out-of-range fails closed with 422),
    and the count + result queries share ``_list_files_filters`` so ``total``
    and ``has_more`` can never drift (audit C10).
    """
    try:
        # Single source of truth for the WHERE clause, applied identically to
        # the count and the result query.
        conditions = _list_files_filters(
            organization, document_type, processing_status, search
        )

        # Count total results
        count_stmt = select(func.count(Document.id)).where(*conditions)
        count_result = await db.execute(count_stmt)
        total = count_result.scalar() or 0

        # Fetch documents with pagination
        stmt = (
            select(Document)
            .where(*conditions)
            .offset(pagination.offset)
            .limit(pagination.size)
        )
        result = await db.execute(stmt)
        documents = result.scalars().all()

        # Page.create derives has_more from the true total + the returned slice.
        page = Page.create(
            items=[doc.to_dict() for doc in documents],
            total=total,
            params=pagination,
        )

        return FileListResponse(
            files=page.items,
            total=page.total,
            page=page.page,
            size=page.size,
            has_more=page.has_more,
        )

    except HTTPException:
        # e.g. the 400 for an invalid processing_status — don't mask it as 500.
        raise
    except Exception as e:
        logger.error(f"Error in list_files: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


@router.get("/stats", response_model=FileStatsResponse)
async def get_file_statistics(
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    file_service: FileService = Depends(get_file_service),
):
    """Get file statistics for the organization.

    Declared BEFORE ``/{file_id}`` — FastAPI matches routes in declaration
    order, so with ``/{file_id}`` first the literal path ``/stats`` was captured
    as a file_id and failed downstream as ``invalid UUID 'stats'`` (the
    multi-tenancy middleware's document lookup raised an asyncpg DataError).
    """
    try:
        stats = await file_service.get_file_stats(str(organization.id))
        return FileStatsResponse(**stats)
    except Exception as e:
        logger.error(f"Error getting file statistics: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


@router.get("/{file_id}")
async def get_file_info(
    file_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """Get detailed information about a specific file"""
    stmt = select(Document).where(
        Document.id == file_id,
        Document.organization_id == organization.id,
        Document.is_deleted == False,
    )
    result = await db.execute(stmt)
    document = result.scalars().first()

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    return {"file": document.to_dict(include_content=True)}


# Content types safe to hand back with their stored MIME. Anything outside this
# set is served as an opaque download: the bytes are user-uploaded and now come
# from the API's own origin, so an HTML/SVG/XML document echoed back with its
# stored type would be stored XSS against this origin.
_SERVEABLE_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "audio/mpeg",
        "audio/mp4",
        "audio/wav",
        "audio/webm",
        "video/mp4",
        "video/webm",
        "video/quicktime",
    }
)

_DOWNLOAD_CHUNK_BYTES = 64 * 1024


async def _iter_signed_url(url: str) -> AsyncIterator[bytes]:
    """Yield an object's bytes from its presigned URL, a chunk at a time.

    Never materializes the whole object: uploads run to 1 GiB on the enterprise
    tier while a backend pod is capped at 4 GiB across 4 workers, so buffering
    a few concurrent downloads would OOM the pod and kill unrelated requests.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0)) as client:
        async with client.stream("GET", url) as upstream:
            upstream.raise_for_status()
            async for chunk in upstream.aiter_bytes(_DOWNLOAD_CHUNK_BYTES):
                yield chunk


def _object_response(signed_url: str, document: Document) -> StreamingResponse:
    """Proxy object-storage bytes through this origin.

    The bytes cannot be served straight from the bucket: a 302 to a presigned
    URL is cross-origin, the bucket sends no Access-Control-Allow-Origin, and
    the inline viewer reads the file with fetch().blob() — so the browser
    blocks the redirect target. Proxying keeps it same-origin.

    That origin move costs the isolation the bucket used to provide, so the
    type is allowlisted, the disposition is `attachment`, and sniffing and
    embedding are disabled. The viewer renders an object URL built from the
    blob, so `attachment` costs it nothing.
    """
    filename = document.filename or "download"
    mime_type = (document.mime_type or "").split(";")[0].strip().lower()
    if mime_type not in _SERVEABLE_MIME_TYPES:
        mime_type = "application/octet-stream"
    return StreamingResponse(
        _iter_signed_url(signed_url),
        media_type=mime_type,
        headers={
            # RFC 5987 form so non-ASCII filenames survive the header; quote()
            # also keeps CR/LF in a stored filename out of the response headers.
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.get("/{file_id}/download")
async def download_file(
    file_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """Download a file"""
    stmt = select(Document).where(
        Document.id == file_id,
        Document.organization_id == organization.id,
        Document.is_deleted == False,
    )
    result = await db.execute(stmt)
    document = result.scalars().first()

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    # Branch on storage backend. Every branch serves the bytes from this
    # origin: a 302 to a presigned object-storage URL works for navigations
    # (<img>/<iframe>/new tab) but fails the CORS check for the inline
    # viewer's fetch().blob(), which is how the app actually reads it — the
    # bucket sends no Access-Control-Allow-Origin, so the browser blocks the
    # redirect target and the viewer reports a generic load failure.
    if document.storage_backend == "supabase" and document.storage_path:
        from src.core.supabase_client import StorageHelper, parse_storage_key

        bucket, key = parse_storage_key(document.storage_path)
        helper = StorageHelper()
        # Explicit existence check so a missing object is a clean 404 rather
        # than a 500 out of the storage client's raised error.
        if not await run_in_threadpool(helper.object_exists, bucket, key):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found in storage",
            )
        signed_url = await run_in_threadpool(
            helper.create_signed_url, bucket, key, 3600
        )
        return _object_response(signed_url, document)

    # S3 / DO Spaces (the deployed default). Without this branch the code fell
    # through to the os.path.exists() check below against an 's3://...' path,
    # which never exists on disk -> every S3-backed document 404'd.
    if document.storage_backend == "s3" and document.storage_path:
        from src.core.s3_client import S3StorageHelper

        s3_helper = S3StorageHelper()
        if not await run_in_threadpool(s3_helper.object_exists, document.storage_path):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found in storage",
            )
        signed_url = await run_in_threadpool(
            s3_helper.create_signed_url, document.storage_path, 3600
        )
        return _object_response(signed_url, document)

    # Local file path
    if not os.path.exists(document.file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found on disk"
        )

    return FileResponse(
        path=document.file_path,
        filename=document.filename,
        media_type=document.mime_type,
    )


@router.put("/{file_id}")
async def update_file_metadata(
    file_id: uuid.UUID,
    title: Optional[str] = None,
    tags: Optional[List[str]] = None,
    is_public: Optional[bool] = None,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """Update file metadata"""
    stmt = select(Document).where(
        Document.id == file_id,
        Document.organization_id == organization.id,
        Document.is_deleted == False,
    )
    result = await db.execute(stmt)
    document = result.scalars().first()

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    # Check permissions (owner or admin)
    if (
        document.uploaded_by_user_id != current_user.id
        and not current_user.has_permission(UserRole.ADMIN)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only update your own files or require admin role",
        )

    try:
        # Update fields
        if title is not None:
            document.title = title

        if tags is not None:
            document.tags = tags

        if is_public is not None:
            document.is_public = is_public

        await db.commit()
        await db.refresh(document)

        return {
            "message": "File metadata updated successfully",
            "file": document.to_dict(),
        }

    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete("/{file_id}")
async def delete_file(
    file_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
    file_service: FileService = Depends(get_file_service),
):
    """Delete a file"""
    stmt = select(Document).where(
        Document.id == file_id,
        Document.organization_id == organization.id,
        Document.is_deleted == False,
    )
    result = await db.execute(stmt)
    document = result.scalars().first()

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    try:
        success = await file_service.delete_file(document, current_user)

        if success:
            return {"message": "File deleted successfully"}
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete file",
            )

    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/{file_id}/content")
async def get_file_content(
    file_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """Get extracted text content of a file"""
    stmt = select(Document).where(
        Document.id == file_id,
        Document.organization_id == organization.id,
        Document.is_deleted == False,
    )
    result = await db.execute(stmt)
    document = result.scalars().first()

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    return {
        "content": document.content_text,
        "summary": document.content_summary,
        "content_preview": document.get_content_preview(500),
    }


@router.get("/{file_id}/metadata")
async def get_file_metadata(
    file_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """Get file metadata"""
    stmt = select(Document).where(
        Document.id == file_id,
        Document.organization_id == organization.id,
        Document.is_deleted == False,
    )
    result = await db.execute(stmt)
    document = result.scalars().first()

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    return {
        # `document.metadata` is SQLAlchemy's reserved declarative MetaData
        # registry, not the document's JSON metadata — returning it makes
        # jsonable_encoder raise and the endpoint 500 on every call. The JSON
        # column is `document_metadata`, exposed via get_metadata().
        "metadata": document.get_metadata(),
        "file_info": {
            "id": str(document.id),
            "title": document.title,
            "filename": document.filename,
            "file_size_mb": document.file_size_mb,
            "mime_type": document.mime_type,
            "document_type": document.document_type.value,
            "processing_status": document.processing_status.value,
            "tags": document.tags,
            "is_public": document.is_public,
            "created_at": document.created_at,
            "updated_at": document.updated_at,
        },
    }


@router.delete("/cancel/{upload_id}")
async def cancel_upload(
    upload_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    file_service: FileService = Depends(get_file_service),
):
    """Cancel an ongoing upload or delete a recently uploaded document"""
    try:
        # Validate upload_id format
        import uuid

        try:
            uuid.UUID(upload_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid upload ID format. Must be a valid UUID.",
            )

        # First, check if upload_id exists in processing jobs

        job_stmt = select(ProcessingJob).where(
            ProcessingJob.celery_task_id == upload_id, ProcessingJob.is_deleted == False
        )
        job_result = await db.execute(job_stmt)
        processing_job = job_result.scalars().first()

        if processing_job:
            # Handle processing job cancellation
            # Check if user owns this job
            if processing_job.created_by_user_id != current_user.id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only cancel your own uploads",
                )

            # Check if job can be cancelled. JobStatus is a plain PyEnum, so the
            # old `status not in ["pending", "running"]` compared enum members
            # to strings — always True, so every cancel early-returned and the
            # block below was dead (and would have written the raw string
            # "cancelled" into the enum column). Compare against enum members.
            if processing_job.status not in (
                JobStatus.PENDING,
                JobStatus.QUEUED,
                JobStatus.RUNNING,
                JobStatus.RETRYING,
            ):
                return {
                    "message": f"Cannot cancel job in {processing_job.status.value} state",
                    "upload_id": upload_id,
                    "job_status": processing_job.status.value,
                }

            # cancel_job() sets status=CANCELLED + completed_at + duration.
            processing_job.cancel_job()
            processing_job.error_message = "Upload cancelled by user"
            await db.commit()

            # Also revoke the Celery task if one was dispatched, mirroring
            # processing.py's cancel path — otherwise the worker keeps
            # running the job after the DB row says cancelled. Best-effort:
            # the cancel is already committed, so a broker hiccup here must
            # not turn the response into a 500 (and roll nothing back).
            if processing_job.celery_task_id:
                try:
                    from src.tasks.processing_tasks import current_app

                    current_app.control.revoke(
                        processing_job.celery_task_id, terminate=True
                    )
                except Exception as revoke_exc:
                    logger.warning(
                        "Failed to revoke Celery task %s for cancelled job %s: %s",
                        processing_job.celery_task_id,
                        processing_job.id,
                        revoke_exc,
                    )

            return {
                "message": "Upload cancelled successfully",
                "upload_id": upload_id,
                "job_id": processing_job.id,
            }

        # If no processing job found, check if it's a document ID
        doc_stmt = select(Document).where(
            Document.id == upload_id,
            Document.uploaded_by_user_id == current_user.id,
            Document.is_deleted == False,
        )
        doc_result = await db.execute(doc_stmt)
        document = doc_result.scalars().first()

        if document:
            # Handle document deletion (for recently uploaded documents)
            # Allow cancellation/deletion of documents that are still in processing state
            if document.processing_status.value in ["pending", "processing"]:
                # Route through file_service.delete_file (not a bare
                # soft_delete): the object was already uploaded to storage at
                # PENDING time, so a plain soft-delete orphans it AND never
                # decrements org storage usage — an upload-then-cancel loop
                # leaks both. delete_file removes the physical object, reverts
                # the quota, soft-deletes, and commits.
                await file_service.delete_file(document, current_user)

                return {
                    "message": "Document upload cancelled successfully",
                    "upload_id": upload_id,
                    "document_id": document.id,
                }
            else:
                return {
                    "message": f"Cannot cancel document in {document.processing_status.value} state",
                    "upload_id": upload_id,
                    "document_status": document.processing_status.value,
                }

        # If neither processing job nor document found
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Upload job or document not found",
        )

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to cancel upload: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


@router.post("/{file_id}/reprocess")
async def reprocess_file(
    file_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """Trigger reprocessing of a file"""
    stmt = select(Document).where(
        Document.id == file_id,
        Document.organization_id == organization.id,
        Document.is_deleted == False,
    )
    result = await db.execute(stmt)
    document = result.scalars().first()

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    # Check permissions
    if (
        document.uploaded_by_user_id != current_user.id
        and not current_user.has_permission(UserRole.ADMIN)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only reprocess your own files or require admin role",
        )

    try:
        # Reset processing status
        document.processing_status = ProcessingStatus.PENDING
        document.processing_error = None
        document.processing_retry_count = 0
        document.processing_started_at = None
        document.processing_completed_at = None

        # Create + enqueue an actual ProcessingJob. The old code only reset the
        # status and committed — it created no job and dispatched no task, so
        # nothing ever picked the document up (there is no PENDING sweeper); it
        # sat PENDING forever while the API falsely reported "queued". Mirror
        # documents.py reprocess_document: flush -> register post-commit enqueue
        # -> commit, so the dispatch fires only after the row is durable (no
        # worker-reads-before-commit race) and is dropped on rollback.
        from src.models.processing import JobPriority, JobType

        processing_job = ProcessingJob(
            job_type=JobType.DOCUMENT_INGESTION,
            status=JobStatus.PENDING,
            priority=JobPriority.NORMAL,
            document_id=document.id,
            organization_id=organization.id,
            created_by_user_id=current_user.id,
            parameters={
                "document_id": str(document.id),
                "file_path": document.file_path,
                "document_type": document.document_type.value,
                "mime_type": document.mime_type,
            },
            config={"max_retries": 3, "timeout_seconds": 300},
            total_steps=5,
            queue_name="document_processing",
        )
        db.add(processing_job)
        await db.flush()

        from src.tasks.enqueue import enqueue_after_commit
        from src.tasks.processing_tasks import process_document_ingestion

        enqueue_after_commit(db, process_document_ingestion, str(processing_job.id))

        await db.commit()

        return {
            "message": "File queued for reprocessing",
            "processing_status": document.processing_status.value,
            "job_id": str(processing_job.id),
        }

    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
