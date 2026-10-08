"""
Document management API endpoints
"""

import asyncio
import logging
import uuid as uuid_module
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Path,
    Query,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.database import get_db


def validate_uuid(value: str, field_name: str = "id") -> str:
    """Validate that a string is a valid UUID format."""
    try:
        uuid_module.UUID(value)
        return value
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name} format. Expected a valid UUID.",
        )


from src.core.dependencies import get_current_organization, get_current_user
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.entity import Entity, EntityType
from src.models.organization import Organization
from src.models.processing import JobStatus, ProcessingJob
from src.models.user import User, UserRole
from src.services.documents.file_service import FileService, get_file_service
from src.shared.enums import (
    ApiDocumentStatus,
    DocumentDateRange,
    DocumentSortField,
    SortOrder,
)

router = APIRouter(prefix="/documents", tags=["documents"], redirect_slashes=False)
logger = logging.getLogger(__name__)


def _escape_like(value: str) -> str:
    """Escape SQL LIKE special characters."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _cleanup_do_kb_data_source(db: AsyncSession, document: Document) -> None:
    """Best-effort removal of a deleted document's DO KB data source.

    A soft-deleted document's chunks stay live in the DO KB index and still
    resolve back to a title in ``resolve_and_filter_chunks`` (which does not
    filter ``is_deleted``), so retrieval would keep surfacing them — and the
    underlying data source leaks storage on DO's side. Unsync removes it.

    Failure-isolated: ``unsync_document_from_kb`` never raises and no-ops when
    ``DO_KB_ENABLED`` is off or the document has no ``do_kb_data_source_uuid``,
    so a KB outage during delete must not block the user's delete.
    """
    if not document.do_kb_data_source_uuid:
        return
    try:
        from src.services.do_kb import unsync_document_from_kb

        await unsync_document_from_kb(db, document)
    except Exception:  # noqa: BLE001
        logger.warning(
            "do_kb cleanup on delete failed",
            extra={"document_id": str(document.id)},
            exc_info=True,
        )


async def _cleanup_do_kb_data_sources_background(document_ids: List[str]) -> None:
    """Bulk-delete DO KB cleanup, run as a FastAPI background task.

    Runs after the response, so it must NOT touch the request's session (closed
    by then, and AsyncSession is not concurrency-safe anyway) — it opens its
    own ``AsyncSessionLocal`` per doc, same pattern as
    ``_persist_assistant_message_safe`` in agent/jobs.py. Sequential is fine
    here: nobody is waiting. Failure-isolated per doc: swallow + log so one
    bad doc (or a DO outage) never crashes the worker or skips the rest.

    Note: a crash between the delete commit and this task leaves a soft-deleted
    doc with a stale do_kb_data_source_uuid — known sub-threshold drift window,
    tracked separately (do not fix here).
    """
    from src.core.database import AsyncSessionLocal
    from src.services.do_kb import unsync_document_from_kb

    for document_id in document_ids:
        try:
            async with AsyncSessionLocal() as db:
                document = await db.get(Document, document_id)
                if document is None or not document.do_kb_data_source_uuid:
                    continue
                await unsync_document_from_kb(db, document)
        except Exception:  # noqa: BLE001
            logger.warning(
                "do_kb bulk-delete background cleanup failed",
                extra={"document_id": str(document_id)},
                exc_info=True,
            )


async def _cleanup_document_graph(document_id: str, organization_id: str) -> None:
    """Run service-owned graph cleanup in a session independent of the request.

    Deletion already committed the cascade choice and pending retry marker.
    Cleanup records failed/completed so provider outages remain recoverable.
    """
    from src.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        await FileService(session).cleanup_deleted_document_graph(
            document_id, organization_id
        )


async def _cleanup_document_graphs_background(
    document_ids: List[str], organization_id: str
) -> None:
    """Bulk-delete Neo4j graph cleanup, run as a FastAPI background task.

    Mirrors ``_cleanup_do_kb_data_sources_background``: runs after the response,
    so it must not touch the request's session. Every document in a bulk delete
    shares one org. Per-doc failure isolation comes from ``_cleanup_document_graph``
    (a neo4j outage on one doc must not skip the rest). See that helper for the
    durable retry behavior.
    """
    for document_id in document_ids:
        await _cleanup_document_graph(document_id, organization_id)


# Request/Response Models
class DocumentResponse(BaseModel):
    id: str
    title: str
    filename: str
    document_type: str
    file_size_bytes: int
    file_size_mb: float
    mime_type: str
    processing_status: ApiDocumentStatus
    tags: Optional[List[str]] = Field(default_factory=list)
    is_public: bool
    content_preview: Optional[str] = None
    content_summary: Optional[str] = None
    created_at: str
    updated_at: str
    uploaded_by_user_id: str
    organization_id: str


class DocumentDetailResponse(DocumentResponse):
    content_text: Optional[str] = None
    metadata: Optional[dict] = None
    processing_started_at: Optional[datetime] = None
    processing_completed_at: Optional[datetime] = None
    processing_error: Optional[str] = None
    processing_retry_count: int = 0


class PaginationInfo(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool
    has_prev: bool


class DocumentListResponse(BaseModel):
    documents: List[DocumentResponse]
    pagination: PaginationInfo


class DocumentEntityResponse(BaseModel):
    id: str
    name: str
    entity_type: str
    confidence_score: float
    extraction_method: str
    metadata: Optional[dict] = None


class DocumentEntitiesResponse(BaseModel):
    document_id: str
    entities: List[DocumentEntityResponse]
    total_entities: int


class DocumentSearchResponse(BaseModel):
    documents: List[DocumentResponse]
    pagination: PaginationInfo
    search_query: str
    search_filters: dict


class DocumentStatusResponse(BaseModel):
    document_id: str
    processing_status: ApiDocumentStatus
    progress_percentage: float
    current_step: Optional[str] = None
    processing_started_at: Optional[datetime] = None
    estimated_completion: Optional[datetime] = None
    error_message: Optional[str] = None


class DuplicateCheckRequest(BaseModel):
    sha256: str = Field(
        ...,
        min_length=64,
        max_length=64,
        description="SHA-256 hex digest of file content",
    )


class DuplicateCheckResponse(BaseModel):
    exists: bool
    document: Optional[DocumentResponse] = None


class BulkDocumentRequest(BaseModel):
    document_ids: List[str] = Field(..., min_length=1, max_length=100)


class BulkDocumentResponse(BaseModel):
    successful: List[str]
    failed: List[dict]
    total_processed: int
    success_count: int
    failure_count: int


async def _run_duplicate_lookup(
    db: AsyncSession,
    query,
    lookup_name: str,
):
    """Run a duplicate lookup query, tolerating schema drift between environments."""
    try:
        result = await db.execute(query)
    except SQLAlchemyError as exc:
        logger.warning(
            "Duplicate lookup failed for %s; falling back to legacy hash strategies",
            lookup_name,
            exc_info=exc,
        )
        return None

    return result.scalar_one_or_none()


@router.get(
    "",
    response_model=DocumentListResponse,
    summary="List documents with filtering and pagination",
    description="""
    Retrieve a paginated list of documents for the current user's organization.
    
    ## Filtering Options
    - **document_type**: Filter by type (pdf, txt, jpg, png, mp3, mp4)
    - **processing_status**: Filter by status (pending, processing, completed, failed)
    - **search**: Full-text search in title, filename, and content
    - **tags**: Comma-separated list of tags to filter by
    - **is_public**: Filter by public/private visibility
    - **date_from/date_to**: Date range filtering
    
    ## Sorting
    - **sort_by**: created_at, updated_at, title, filename, file_size_bytes
    - **sort_order**: asc or desc (default: desc)
    
    ## Pagination
    - **page**: 1-indexed page number
    - **size**: Items per page (1-100, default 20)
    
    ## Security
    - Requires authentication
    - Returns only documents belonging to user's organization
    - Uses validated enums for sort fields (SQL injection protected)
    """,
    responses={
        200: {
            "description": "Successfully retrieved documents",
            "content": {
                "application/json": {
                    "example": {
                        "documents": [
                            {
                                "id": "550e8400-e29b-41d4-a716-446655440001",
                                "title": "Research Paper on ML",
                                "filename": "ml-research.pdf",
                                "document_type": "pdf",
                                "file_size_bytes": 1048576,
                                "file_size_mb": 1.0,
                                "mime_type": "application/pdf",
                                "processing_status": "indexed",
                                "tags": ["research", "machine-learning"],
                                "is_public": False,
                                "content_preview": "This paper explores...",
                                "created_at": "2024-01-15T10:30:00Z",
                                "updated_at": "2024-01-15T10:35:00Z",
                            }
                        ],
                        "pagination": {
                            "page": 1,
                            "page_size": 20,
                            "total": 42,
                            "total_pages": 3,
                            "has_next": True,
                            "has_prev": False,
                        },
                    }
                }
            },
        },
        401: {"description": "Not authenticated - missing or invalid token"},
        403: {"description": "Not authorized to access this organization's documents"},
        500: {"description": "Internal server error"},
    },
)
@router.get("/", response_model=DocumentListResponse, include_in_schema=False)
async def list_documents(
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
    document_type: Optional[DocumentType] = Query(
        None, description="Filter by document type"
    ),
    processing_status: Optional[str] = Query(
        None,
        description="Filter by processing status (accepts: pending, processing, completed, failed, retrying, queued, indexed)",
    ),
    search: Optional[str] = Query(None, description="Search in title and filename"),
    tags: Optional[str] = Query(None, description="Filter by tags (comma-separated)"),
    is_public: Optional[bool] = Query(None, description="Filter by public status"),
    date_from: Optional[datetime] = Query(
        None, description="Filter documents from date"
    ),
    date_to: Optional[datetime] = Query(None, description="Filter documents to date"),
    sort_by: DocumentSortField = Query(
        DocumentSortField.CREATED_AT,
        description="Sort field (created_at, updated_at, title, filename, file_size_bytes, processing_status, document_type)",
    ),
    sort_order: SortOrder = Query(
        SortOrder.DESC, description="Sort order: asc or desc"
    ),
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """
    List documents with pagination and comprehensive filtering
    """
    try:
        from sqlalchemy import or_

        # Build base conditions
        conditions = [
            Document.organization_id == organization.id,
            Document.is_deleted == False,
        ]

        # Apply filters
        if document_type:
            conditions.append(Document.document_type == document_type)

        if processing_status:
            # Translate the public/raw status filter to the backend enum via the
            # shared vocabulary (single source of truth). Keeps the validated-enum
            # injection-prevention pattern — only known values reach the query.
            try:
                mapped = ApiDocumentStatus.to_db(processing_status)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid processing_status: {processing_status}",
                )
            conditions.append(Document.processing_status == mapped)

        if search:
            search_pattern = f"%{_escape_like(search)}%"
            # Use ILIKE for title/filename and TSVECTOR for content search
            conditions.append(
                or_(
                    Document.title.ilike(search_pattern),
                    Document.filename.ilike(search_pattern),
                    Document.search_vector.match(search),
                )
            )

        if tags:
            tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()]
            for tag in tag_list:
                conditions.append(Document.tags.contains([tag]))

        if is_public is not None:
            conditions.append(Document.is_public == is_public)

        if date_from:
            conditions.append(Document.created_at >= date_from)

        if date_to:
            conditions.append(Document.created_at <= date_to)

        # Apply sorting - sort_by is now a validated enum, preventing SQL injection
        sort_column = getattr(Document, sort_by.value, Document.created_at)
        order_clause = (
            sort_column.desc() if sort_order == SortOrder.DESC else sort_column.asc()
        )

        # Count total results
        count_stmt = select(func.count(Document.id)).where(*conditions)
        count_result = await db.execute(count_stmt)
        total = count_result.scalar() or 0

        # Fetch documents with pagination
        offset = (page - 1) * size
        stmt = (
            select(Document)
            .options(
                selectinload(Document.uploaded_by_user),
                selectinload(Document.organization),
            )
            .where(*conditions)
            .order_by(order_clause)
            .offset(offset)
            .limit(size)
        )
        result = await db.execute(stmt)
        documents = result.scalars().all()

        # Convert to response format
        document_responses = []
        for doc in documents:
            document_responses.append(
                DocumentResponse(
                    id=str(doc.id),
                    title=doc.title,
                    filename=doc.filename,
                    document_type=doc.document_type.value,
                    file_size_bytes=doc.file_size_bytes,
                    file_size_mb=doc.file_size_mb,
                    mime_type=doc.mime_type,
                    processing_status=doc.get_mapped_status(),
                    tags=doc.tags,
                    is_public=doc.is_public,
                    content_preview=doc.get_content_preview(200),
                    content_summary=doc.content_summary,
                    created_at=doc.created_at.isoformat(),
                    updated_at=doc.updated_at.isoformat(),
                    uploaded_by_user_id=str(doc.uploaded_by_user_id),
                    organization_id=str(doc.organization_id),
                )
            )

        total_pages = (total + size - 1) // size
        has_next = page < total_pages
        has_prev = page > 1

        return DocumentListResponse(
            documents=document_responses,
            pagination=PaginationInfo(
                page=page,
                page_size=size,
                total=total,
                total_pages=total_pages,
                has_next=has_next,
                has_prev=has_prev,
            ),
        )

    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to list documents", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list documents",
        )


@router.get("/{document_id}", response_model=DocumentDetailResponse)
async def get_document(
    document_id: str = Path(..., description="Document UUID"),
    include_content: bool = Query(False, description="Include full text content"),
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """
    Get detailed information about a specific document
    """
    # Validate UUID format to prevent database errors
    validate_uuid(document_id, "document_id")

    stmt = select(Document).where(
        Document.id == document_id,
        Document.organization_id == organization.id,
        Document.is_deleted == False,
    )
    result = await db.execute(stmt)
    document = result.scalars().first()

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

    # Check permissions
    if not document.is_public and not current_user.has_permission(UserRole.USER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this document",
        )

    return DocumentDetailResponse(
        id=str(document.id),
        title=document.title,
        filename=document.filename,
        document_type=document.document_type.value,
        file_size_bytes=document.file_size_bytes,
        file_size_mb=document.file_size_mb,
        mime_type=document.mime_type,
        processing_status=document.to_dict()["processing_status"],
        tags=document.tags,
        is_public=document.is_public,
        content_preview=document.get_content_preview(500),
        content_summary=document.content_summary,
        content_text=document.content_text if include_content else None,
        metadata=document.get_metadata(),
        created_at=document.created_at.isoformat(),
        updated_at=document.updated_at.isoformat(),
        uploaded_by_user_id=str(document.uploaded_by_user_id),
        organization_id=str(document.organization_id),
        processing_started_at=document.processing_started_at,
        processing_completed_at=document.processing_completed_at,
        processing_error=document.processing_error,
        processing_retry_count=document.processing_retry_count,
    )


@router.delete("/{document_id}")
async def delete_document(
    document_id: str = Path(..., description="Document UUID"),
    cascade: bool = Query(True, description="Cascade delete related entities and jobs"),
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
    file_service: FileService = Depends(get_file_service),
):
    """
    Delete a document with optional cascade deletion
    """
    validate_uuid(document_id, "document_id")

    stmt = select(Document).where(
        Document.id == document_id,
        Document.organization_id == organization.id,
        Document.is_deleted == False,
    )
    result = await db.execute(stmt)
    document = result.scalars().first()

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

    # Check permissions (owner or admin)
    from src.models.user import UserRole

    if (
        document.uploaded_by_user_id != current_user.id
        and not current_user.has_permission(UserRole.ADMIN)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only delete your own documents or require admin role",
        )

    try:
        # Locks, cancels unfinished jobs, soft-deletes and releases quota once.
        deleted = await file_service.soft_delete_documents(
            organization.id, [document.id], cascade=cascade, user=current_user
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to delete document %s", document_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete document",
        )
    if not deleted:
        # A concurrent delete won and already released the quota.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

    # Best-effort physical delete AFTER the commit — deleting first meant a
    # commit failure rolled back the row while the object was already gone
    # (live row pointing at a missing file). A failure here leaves at worst
    # a sweepable orphan object.
    try:
        file_service.delete_physical_file(document)
    except Exception:
        logger.warning(
            "Soft-deleted document %s but failed to remove its storage object",
            document.id,
            exc_info=True,
        )

    # Remove the DO KB data source so the deleted doc stops surfacing in
    # retrieval and no longer leaks storage (best-effort, never blocks).
    await _cleanup_do_kb_data_source(db, document)

    # Reap this document's graph from Neo4j (its relationships, then its
    # now-orphaned entity nodes — nodes are shared across docs, so no blind
    # DETACH DELETE) so the graph doesn't drift forever (audit D2). Gated on
    # ``cascade`` to match the Postgres Entity soft-delete above;
    # best-effort, runs after the commit like the DO KB cleanup and never
    # blocks the delete.
    if cascade:
        await _cleanup_document_graph(document_id, str(organization.id))

    return {
        "message": "Document deleted successfully",
        "document_id": str(document.id),
        "cascade_deleted": cascade,
        "file_size_freed": document.file_size_bytes,
    }


@router.get("/{document_id}/entities", response_model=DocumentEntitiesResponse)
async def get_document_entities(
    document_id: str = Path(..., description="Document UUID"),
    entity_type: Optional[str] = Query(None, description="Filter by entity type"),
    min_confidence: Optional[float] = Query(
        0.0, ge=0.0, le=1.0, description="Minimum confidence score"
    ),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """
    Get entities extracted from a specific document
    """
    validate_uuid(document_id, "document_id")

    # Verify document exists and user has access
    stmt = select(Document).where(
        Document.id == document_id,
        Document.organization_id == organization.id,
        Document.is_deleted == False,
    )
    result = await db.execute(stmt)
    document = result.scalars().first()

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

    # Check permissions
    if not document.is_public and not current_user.has_permission(UserRole.USER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this document",
        )

    try:
        # Build entities conditions
        conditions = [Entity.document_id == document_id, Entity.is_deleted == False]

        if entity_type:
            # Coerce to the EntityType enum — comparing the Enum column to a
            # raw string raises LookupError at execute for unknown values.
            try:
                entity_type_member = EntityType(entity_type.lower())
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unknown entity_type '{entity_type}'",
                )
            conditions.append(Entity.entity_type == entity_type_member)

        if min_confidence > 0:
            # Column is Entity.confidence — Entity.confidence_score does not
            # exist and raised AttributeError at statement-build time.
            conditions.append(Entity.confidence >= min_confidence)

        # Count total entities
        count_stmt = select(func.count(Entity.id)).where(*conditions)
        count_result = await db.execute(count_stmt)
        total = count_result.scalar() or 0

        # Apply pagination
        offset = (page - 1) * size
        stmt = select(Entity).where(*conditions).offset(offset).limit(size)
        result = await db.execute(stmt)
        entities = result.scalars().all()

        # Convert to response format
        entity_responses = []
        for entity in entities:
            entity_responses.append(
                DocumentEntityResponse(
                    id=str(entity.id),
                    name=entity.name,
                    entity_type=entity.entity_type.value,
                    # ORM attrs are `confidence` and `properties` —
                    # `entity.confidence_score` raised AttributeError, and
                    # `entity.metadata` resolves to SQLAlchemy's declarative
                    # MetaData registry, not row data.
                    confidence_score=entity.confidence,
                    extraction_method=entity.extraction_method.value,
                    metadata=entity.properties,
                )
            )

        return DocumentEntitiesResponse(
            document_id=document_id, entities=entity_responses, total_entities=total
        )

    except HTTPException:
        # Deliberate 4xx (e.g. unknown entity_type) — don't rewrap as a 500.
        raise
    except Exception:
        logger.error("Failed to get document entities", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get document entities",
        )


@router.get("/{document_id}/status", response_model=DocumentStatusResponse)
async def get_document_status(
    document_id: str = Path(..., description="Document UUID"),
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """
    Get real-time processing status of a document
    """
    validate_uuid(document_id, "document_id")

    # Verify document exists and user has access
    stmt = select(Document).where(
        Document.id == document_id,
        Document.organization_id == organization.id,
        Document.is_deleted == False,
    )
    result = await db.execute(stmt)
    document = result.scalars().first()

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

    # Check permissions
    if not document.is_public and not current_user.has_permission(UserRole.USER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this document",
        )

    # Get latest processing job
    job_stmt = (
        select(ProcessingJob)
        .where(
            ProcessingJob.document_id == document_id, ProcessingJob.is_deleted == False
        )
        .order_by(ProcessingJob.created_at.desc())
    )
    job_result = await db.execute(job_stmt)
    processing_job = job_result.scalars().first()

    # Calculate estimated completion
    estimated_completion = None
    if (
        processing_job
        and processing_job.started_at
        and processing_job.progress_percentage < 100
    ):
        # Simple estimation based on current progress
        elapsed_time = (
            datetime.now(timezone.utc) - processing_job.started_at
        ).total_seconds()
        if processing_job.progress_percentage > 0:
            estimated_total_time = elapsed_time / (
                processing_job.progress_percentage / 100
            )
            estimated_completion = processing_job.started_at + timedelta(
                seconds=estimated_total_time
            )

    return DocumentStatusResponse(
        document_id=document_id,
        processing_status=document.to_dict()["processing_status"],
        progress_percentage=(
            processing_job.progress_percentage if processing_job else 0.0
        ),
        current_step=processing_job.current_step if processing_job else None,
        processing_started_at=document.processing_started_at,
        estimated_completion=estimated_completion,
        error_message=document.processing_error
        or (processing_job.error_message if processing_job else None),
    )


@router.post(
    "/check-duplicate",
    response_model=DuplicateCheckResponse,
    summary="Check if a document with the given content hash already exists",
)
async def check_duplicate(
    body: DuplicateCheckRequest,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
) -> DuplicateCheckResponse:
    """Pre-upload duplicate check by SHA-256 content hash."""
    query = (
        select(Document)
        .where(
            Document.checksum_sha256 == body.sha256,
            Document.organization_id == organization.id,
            Document.is_deleted.isnot(True),
        )
        .limit(1)
    )
    existing = await _run_duplicate_lookup(db, query, "checksum_sha256")

    if existing is None:
        # Also check metadata-stored hash for older documents
        from sqlalchemy import String, cast

        query = (
            select(Document)
            .where(
                cast(Document.document_metadata["file_hash"], String) == body.sha256,
                Document.organization_id == organization.id,
                Document.is_deleted.isnot(True),
            )
            .limit(1)
        )
        existing = await _run_duplicate_lookup(
            db,
            query,
            "document_metadata.file_hash",
        )

    if existing is None:
        return DuplicateCheckResponse(exists=False)

    return DuplicateCheckResponse(
        exists=True,
        document=DocumentResponse(
            id=str(existing.id),
            title=existing.title or "",
            filename=existing.filename or "",
            document_type=existing.document_type or "unknown",
            file_size_bytes=existing.file_size_bytes or 0,
            file_size_mb=round((existing.file_size_bytes or 0) / (1024 * 1024), 2),
            mime_type=existing.mime_type or "",
            processing_status=ApiDocumentStatus.from_db(existing.processing_status),
            tags=existing.tags or [],
            is_public=existing.is_public or False,
            created_at=str(existing.created_at) if existing.created_at else "",
            updated_at=str(existing.updated_at) if existing.updated_at else "",
            uploaded_by_user_id=(
                str(existing.uploaded_by_user_id)
                if existing.uploaded_by_user_id
                else ""
            ),
            organization_id=(
                str(existing.organization_id) if existing.organization_id else ""
            ),
        ),
    )


@router.post(
    "/search",
    response_model=DocumentSearchResponse,
    summary="Search documents with hybrid search",
    description="""
    Perform full-text search across document titles, filenames, and content.
    
    ## Search Behavior
    - Searches title, filename, content_text, and content_summary fields
    - Results are ordered by relevance (title matches prioritized)
    - Returns matching documents with highlighted preview
    
    ## Filtering Options
    - **document_types**: List of document types to include
    - **tags**: List of tags to filter by (AND logic)
    - **date_range**: Quick date filters (last_week, last_month, last_year)
    
    ## Performance Notes
    - Uses eager loading to prevent N+1 query issues
    - Optimized for organizations with up to 100k documents
    """,
    responses={
        200: {
            "description": "Search results",
            "content": {
                "application/json": {
                    "example": {
                        "documents": [
                            {
                                "id": "550e8400-e29b-41d4-a716-446655440001",
                                "title": "Machine Learning Guide",
                                "filename": "ml-guide.pdf",
                                "document_type": "pdf",
                                "processing_status": "indexed",
                                "content_preview": "...comprehensive guide to machine learning...",
                            }
                        ],
                        "pagination": {"page": 1, "total": 15, "has_next": False},
                        "search_query": "machine learning",
                        "search_filters": {
                            "document_types": ["pdf"],
                            "date_range": "last_month",
                        },
                    }
                }
            },
        },
        400: {"description": "Invalid query parameters"},
        401: {"description": "Not authenticated"},
    },
)
async def search_documents(
    query: str = Query(..., description="Search query"),
    document_types: Optional[List[DocumentType]] = Query(
        None, description="Filter by document types"
    ),
    tags: Optional[List[str]] = Query(None, description="Filter by tags"),
    date_range: Optional[DocumentDateRange] = Query(
        None, description="Date range filter"
    ),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """
    Advanced document search with multiple filters
    """
    try:
        from sqlalchemy import or_

        # Build base conditions
        conditions = [
            Document.organization_id == organization.id,
            Document.is_deleted == False,
        ]

        # Apply text search — use ILIKE for title/filename and TSVECTOR for content
        search_pattern = f"%{_escape_like(query)}%"
        conditions.append(
            or_(
                Document.title.ilike(search_pattern),
                Document.filename.ilike(search_pattern),
                Document.search_vector.match(query),
            )
        )

        # Apply document type filter
        if document_types:
            conditions.append(Document.document_type.in_(document_types))

        # Apply tags filter
        if tags:
            for tag in tags:
                conditions.append(Document.tags.contains([tag]))

        # Apply date range filter
        if date_range:
            now = datetime.utcnow()
            if date_range == "last_week":
                date_from = now - timedelta(days=7)
                conditions.append(Document.created_at >= date_from)
            elif date_range == "last_month":
                date_from = now - timedelta(days=30)
                conditions.append(Document.created_at >= date_from)
            elif date_range == "last_year":
                date_from = now - timedelta(days=365)
                conditions.append(Document.created_at >= date_from)

        # Count total results
        count_stmt = select(func.count(Document.id)).where(*conditions)
        count_result = await db.execute(count_stmt)
        total = count_result.scalar() or 0

        # Fetch documents with pagination
        offset = (page - 1) * size
        stmt = (
            select(Document)
            .options(
                selectinload(Document.uploaded_by_user),
                selectinload(Document.organization),
            )
            .where(*conditions)
            .order_by(
                Document.title.ilike(search_pattern).desc(), Document.created_at.desc()
            )
            .offset(offset)
            .limit(size)
        )
        result = await db.execute(stmt)
        documents = result.scalars().all()

        # Convert to response format
        document_responses = []
        for doc in documents:
            document_responses.append(
                DocumentResponse(
                    id=str(doc.id),
                    title=doc.title,
                    filename=doc.filename,
                    document_type=doc.document_type.value,
                    file_size_bytes=doc.file_size_bytes,
                    file_size_mb=doc.file_size_mb,
                    mime_type=doc.mime_type,
                    processing_status=doc.get_mapped_status(),
                    tags=doc.tags,
                    is_public=doc.is_public,
                    content_preview=doc.get_content_preview(200),
                    content_summary=doc.content_summary,
                    created_at=doc.created_at.isoformat(),
                    updated_at=doc.updated_at.isoformat(),
                    uploaded_by_user_id=str(doc.uploaded_by_user_id),
                    organization_id=str(doc.organization_id),
                )
            )

        total_pages = (total + size - 1) // size
        has_next = page < total_pages
        has_prev = page > 1

        return DocumentSearchResponse(
            documents=document_responses,
            pagination=PaginationInfo(
                page=page,
                page_size=size,
                total=total,
                total_pages=total_pages,
                has_next=has_next,
                has_prev=has_prev,
            ),
            search_query=query,
            search_filters={
                "document_types": (
                    [dt.value for dt in document_types] if document_types else None
                ),
                "tags": tags,
                "date_range": date_range,
            },
        )

    except Exception:
        logger.error("Document search failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Document search failed",
        )


@router.post("/bulk-delete", response_model=BulkDocumentResponse)
async def bulk_delete_documents(
    request: BulkDocumentRequest,
    cascade: bool = Query(True, description="Cascade delete related data"),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
    file_service: FileService = Depends(get_file_service),
):
    """
    Bulk delete multiple documents
    """
    if not current_user.has_permission(UserRole.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bulk delete requires admin privileges",
        )

    # Dedupe: a caller sending the same id twice must not double-count it in
    # `successful` (or `successful_count` would over-report vs. the number of
    # documents actually deleted).
    requested_ids = list(dict.fromkeys(request.document_ids))
    try:
        # Set-based, one transaction: locks, cancels unfinished jobs,
        # soft-deletes and releases quota once per document.
        deleted_docs = await file_service.soft_delete_documents(
            organization.id, requested_ids, cascade=cascade
        )
    except Exception:
        logger.exception("Failed to bulk delete documents")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to bulk delete documents",
        )
    found_ids = {str(d.id) for d in deleted_docs}
    successful = [i for i in requested_ids if i in found_ids]
    failed = [
        {"document_id": i, "error": "Document not found"}
        for i in requested_ids
        if i not in found_ids
    ]

    # Best-effort physical deletes AFTER the commit (see delete_document): only
    # documents whose soft-delete actually committed lose their objects, and a
    # storage failure leaves a sweepable orphan instead of a live row with a
    # missing file.
    for document in deleted_docs:
        try:
            file_service.delete_physical_file(document)
        except Exception:
            logger.warning(
                "Soft-deleted document %s but failed to remove its storage object",
                document.id,
                exc_info=True,
            )

    # Defer DO KB cleanup off the request path: up to 100 docs × one DO HTTP
    # call each (with retries/backoff) could block this response for minutes
    # when DO KB is degraded. Not asyncio.gather here — the request's single
    # AsyncSession is not concurrency-safe (and unsync commits on it). The
    # background task opens its own session; sequential there is fine.
    kb_cleanup_ids = [str(d.id) for d in deleted_docs if d.do_kb_data_source_uuid]
    if kb_cleanup_ids:
        background_tasks.add_task(
            _cleanup_do_kb_data_sources_background, kb_cleanup_ids
        )

    # Best-effort Neo4j graph cleanup, deferred like DO KB above: reap each
    # deleted document's relationships + now-orphaned entity nodes so the graph
    # doesn't drift (audit D2). Gated on ``cascade`` to match the Postgres
    # Entity soft-delete; per-doc failure isolation lives in the background
    # helper.
    if cascade and deleted_docs:
        background_tasks.add_task(
            _cleanup_document_graphs_background,
            [str(d.id) for d in deleted_docs],
            str(organization.id),
        )

    return BulkDocumentResponse(
        successful=successful,
        failed=failed,
        total_processed=len(requested_ids),
        success_count=len(successful),
        failure_count=len(failed),
    )


@router.post("/{document_id}/reprocess")
async def reprocess_document(
    document_id: str = Path(..., description="Document UUID"),
    force_reprocess: bool = Query(
        False, description="Force reprocess even if already completed"
    ),
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """
    Trigger reprocessing of a document
    """
    validate_uuid(document_id, "document_id")

    from src.services.documents.file_service import FileService

    document = await FileService(db).lock_document_for_reprocessing(
        document_id, organization.id, current_user
    )

    # Check if reprocessing is needed
    if document.processing_status == ProcessingStatus.COMPLETED and not force_reprocess:
        return {
            "message": "Document already processed successfully. Use force_reprocess=true to override.",
            "processing_status": document.processing_status.value,
        }

    try:
        # Reset processing status
        document.processing_status = ProcessingStatus.PENDING
        document.processing_error = None
        document.processing_retry_count = 0
        document.processing_started_at = None
        document.processing_completed_at = None

        # Flush (not commit): keep the status reset and the new job in one
        # transaction that is only committed AFTER the Celery enqueue succeeds.
        # Committing here would strand the document in PENDING (losing its prior
        # status) if the enqueue below fails — the except's rollback can't undo a
        # commit.
        await db.flush()

        # Create new processing job
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
                "force_reprocess": force_reprocess,
            },
            config={"max_retries": 3, "timeout_seconds": 300},
            total_steps=5,
            queue_name="document_processing",
        )

        db.add(processing_job)
        # Flush to populate processing_job.id for the enqueue registration.
        await db.flush()

        # Queue the job for processing — post-commit. enqueue_after_commit
        # dispatches the ingestion task only *after* the commit below succeeds
        # (closing the worker-reads-before-commit race) and drops it entirely if
        # the transaction rolls back (no orphan job). A broker outage in the
        # post-commit window leaves the job PENDING/celery_task_id=NULL, which
        # the lost-job reconciler (src.tasks.reconcile_jobs) re-enqueues —
        # durability without the pre-commit race.
        from src.tasks.enqueue import enqueue_after_commit
        from src.tasks.processing_tasks import process_document_ingestion

        enqueue_after_commit(db, process_document_ingestion, str(processing_job.id))

        await db.commit()

        return {
            "message": "Document queued for reprocessing",
            "document_id": str(document.id),
            "processing_status": document.processing_status.value,
            "job_id": str(processing_job.id),
        }

    except Exception:
        await db.rollback()
        logger.error("Failed to queue document for reprocessing", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to queue document for reprocessing",
        )
