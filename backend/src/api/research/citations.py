"""Citations API endpoints for Research Assistant feature.

Handles:
- Citation persistence from chat messages
- Citation retrieval and filtering
- Citation metadata extraction
- Bibliography export
"""

import math
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from structlog import get_logger

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models import (
    ChatMessage,
    Citation,
    Collection,
    CollectionDocument,
    Conversation,
    Document,
    Thread,
    User,
    Workspace,
)
from src.services.research.bibliography_service import BibliographyService
from src.services.research.citation_extraction_service import CitationExtractionService
from src.services.research_engine.project_access import ResearchAction, resolve_project
from src.shared.research_schemas import (
    CitationCreate,
    CitationListResponse,
    CitationResponse,
    CitationUpdate,
)

logger = get_logger()
router = APIRouter(prefix="/api/v1/citations", tags=["citations"])

_CITATION_RELATIONSHIP_TYPES = frozenset({"CITES", "CITED_BY", "RELATED_TO"})


def _normalize_citation_relationship_type(value: str) -> str:
    normalized = value.upper()
    if normalized not in _CITATION_RELATIONSHIP_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="relationship_type must be CITES, CITED_BY, or RELATED_TO",
        )
    return normalized


def _document_is_accessible(document: object, current_user: User) -> bool:
    """GOO-349: documents are organization-scoped. ``is_public`` / uploader
    identity only narrow access *inside* the caller's current organization;
    neither may bypass it (a foreign-org public document, or one the caller
    uploaded in a previous org). An org-less caller gets no document path."""
    if document is None:
        return False
    if getattr(document, "is_deleted", False) is True:
        return False
    org_id = current_user.organization_id
    if org_id is None or getattr(document, "organization_id", None) != org_id:
        return False

    return bool(
        getattr(document, "is_public", False)
        or getattr(document, "uploaded_by_user_id", None) == current_user.id
    )


def _document_access_clause(current_user: User):
    """SQL mirror of ``_document_is_accessible`` (same org fail-closed rule)."""
    if current_user.organization_id is None:
        return false()
    return and_(
        Document.is_deleted.is_(False),
        Document.organization_id == current_user.organization_id,
        or_(
            Document.is_public.is_(True),
            Document.uploaded_by_user_id == current_user.id,
        ),
    )


def _message_is_accessible(message: object, current_user: User) -> bool:
    if message is None:
        return False

    thread = getattr(message, "thread", None)
    conversation = getattr(thread, "conversation", None) if thread else None
    workspace = getattr(conversation, "workspace", None) if conversation else None
    if any(
        getattr(obj, "is_deleted", False) is True
        for obj in (message, thread, conversation, workspace)
        if obj is not None
    ):
        return False
    return getattr(workspace, "owner_id", None) == current_user.id


def _citation_is_accessible(citation: Citation, current_user: User) -> bool:
    if getattr(citation, "is_deleted", False) is True:
        return False
    return _document_is_accessible(
        getattr(citation, "document", None), current_user
    ) or _message_is_accessible(getattr(citation, "message", None), current_user)


async def _load_accessible_citation(
    citation_id: UUID, current_user: User, db: AsyncSession
) -> Citation:
    """Load a citation with the relationships ``_citation_is_accessible`` needs
    and enforce access. Raises 404 if missing or not accessible (404, not 403,
    so a caller cannot probe which citation ids exist). Eager-load shape mirrors
    ``get_citation``."""
    result = await db.execute(
        select(Citation)
        .options(
            selectinload(Citation.document),
            selectinload(Citation.message)
            .selectinload(ChatMessage.thread)
            .selectinload(Thread.conversation)
            .selectinload(Conversation.workspace),
        )
        .where(Citation.id == citation_id, Citation.is_deleted.is_(False))
    )
    citation = result.scalar_one_or_none()
    if citation is None or not _citation_is_accessible(citation, current_user):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Citation not found",
        )
    return citation


async def _ensure_project_access(
    project_id: UUID,
    current_user: User,
    db: AsyncSession,
    action: ResearchAction = ResearchAction.VIEW,
) -> None:
    """Apply the canonical project authorization and lifecycle boundary."""
    await resolve_project(db, project_id, current_user.id, action)


class CitationExtractRequest(BaseModel):
    """Request payload for citation extraction."""

    document_id: Optional[UUID] = None
    arxiv_id: Optional[str] = None
    doi: Optional[str] = None
    title: Optional[str] = None
    strategy: str = "auto"


class CitationLookupRequest(BaseModel):
    """Request payload for citation lookup without persistence."""

    document_id: Optional[UUID] = None
    arxiv_id: Optional[str] = None
    doi: Optional[str] = None
    title: Optional[str] = None
    strategy: str = "auto"


class BibliographyExportRequest(BaseModel):
    """Request payload for bibliography export."""

    format: str = Field(default="bibtex")
    citation_ids: Optional[List[UUID]] = None
    project_id: Optional[UUID] = None


@router.post("", response_model=CitationResponse, status_code=status.HTTP_201_CREATED)
async def create_citation(
    citation_data: CitationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new citation from chat message context.

    Args:
        citation_data: Citation creation data
        current_user: Authenticated user
        db: Database session

    Returns:
        Created citation
    """
    try:
        # Verify the referenced document belongs to the user's organization
        if citation_data.document_id:
            doc_check = await db.execute(
                select(Document.id).where(
                    Document.id == citation_data.document_id,
                    Document.organization_id == current_user.organization_id,
                    Document.is_deleted == False,
                )
            )
            if doc_check.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Document not found or not accessible",
                )

        # Verify the referenced message belongs to the user (same ownership
        # chain _message_is_accessible walks: thread → conversation → workspace)
        if citation_data.message_id:
            msg_check = await db.execute(
                select(ChatMessage.id)
                .join(ChatMessage.thread)
                .join(Thread.conversation)
                .join(Conversation.workspace)
                .where(
                    ChatMessage.id == citation_data.message_id,
                    Workspace.owner_id == current_user.id,
                    ChatMessage.is_deleted.is_(False),
                    Thread.is_deleted.is_(False),
                    Conversation.is_deleted.is_(False),
                    Workspace.is_deleted.is_(False),
                )
            )
            if msg_check.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Message not found or not accessible",
                )

        # Create citation instance
        citation = Citation(
            message_id=citation_data.message_id,
            document_id=citation_data.document_id,
            external_reference_id=citation_data.external_reference_id,
            document_title=citation_data.document_title,
            document_type=citation_data.document_type or "paper",
            authors=citation_data.authors,
            year=citation_data.year,
            venue=citation_data.venue,
            doi=citation_data.doi,
            arxiv_id=citation_data.arxiv_id,
            abstract=citation_data.abstract,
            snippet=citation_data.snippet,
            page_number=citation_data.page_number,
            score=citation_data.score or 0.0,
            metadata_source=citation_data.metadata_source or "manual",
            needs_review=citation_data.needs_review or False,
        )

        db.add(citation)
        await db.commit()
        await db.refresh(citation)

        logger.info(
            "citation_created",
            citation_id=str(citation.id),
            document_id=str(citation.document_id) if citation.document_id else None,
            message_id=str(citation.message_id) if citation.message_id else None,
            metadata_source=citation.metadata_source,
        )

        return CitationResponse.model_validate(citation)

    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        logger.error(
            "citation_creation_failed", exc_info=True, user_id=str(current_user.id)
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create citation",
        )


@router.get("", response_model=CitationListResponse)
async def list_citations(
    message_id: Optional[UUID] = Query(None, description="Filter by message ID"),
    document_id: Optional[UUID] = Query(None, description="Filter by document ID"),
    arxiv_id: Optional[str] = Query(None, description="Filter by ArXiv ID"),
    doi: Optional[str] = Query(None, description="Filter by DOI"),
    needs_review: Optional[bool] = Query(
        None, description="Filter by needs_review flag"
    ),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(50, ge=1, le=100, description="Max records to return"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List citations with filtering.

    Args:
        message_id: Optional message ID filter
        document_id: Optional document ID filter
        arxiv_id: Optional ArXiv ID filter
        doi: Optional DOI filter
        needs_review: Optional needs_review filter
        skip: Pagination offset
        limit: Pagination limit
        current_user: Authenticated user
        db: Database session

    Returns:
        List of citations with pagination metadata
    """
    if not isinstance(skip, int):
        skip = 0
    if not isinstance(limit, int):
        limit = 50

    try:
        filters = []
        if message_id:
            filters.append(Citation.message_id == message_id)
        if document_id:
            filters.append(Citation.document_id == document_id)
        if arxiv_id:
            filters.append(Citation.arxiv_id == arxiv_id)
        if doi:
            filters.append(Citation.doi == doi)
        if needs_review is not None:
            filters.append(Citation.needs_review == needs_review)

        # SQL equivalent of _citation_is_accessible: accessible via the
        # document (caller's org AND public-or-own) OR via the message's
        # thread → conversation → workspace ownership chain. All joins are
        # many-to-one from Citation, so no row multiplication.
        access_filter = or_(
            _document_access_clause(current_user),
            and_(
                ChatMessage.is_deleted.is_(False),
                Thread.is_deleted.is_(False),
                Conversation.is_deleted.is_(False),
                Workspace.is_deleted.is_(False),
                Workspace.owner_id == current_user.id,
            ),
            # R5-L17 note: orphan citations (neither document nor message)
            # intentionally match NOTHING — the model has no creator column
            # to scope them to, and world-readable orphans would be worse
            # than invisible ones. Fail-closed until a creator column lands.
        )
        filtered = (
            select(Citation)
            .outerjoin(Citation.document)
            .outerjoin(Citation.message)
            .outerjoin(ChatMessage.thread)
            .outerjoin(Thread.conversation)
            .outerjoin(Conversation.workspace)
            .where(Citation.is_deleted.is_(False), access_filter, *filters)
        )

        count_result = await db.execute(
            select(func.count()).select_from(filtered.subquery())
        )
        total = count_result.scalar() or 0

        result = await db.execute(
            filtered.options(
                selectinload(Citation.document),
                selectinload(Citation.message)
                .selectinload(ChatMessage.thread)
                .selectinload(Thread.conversation)
                .selectinload(Conversation.workspace),
            )
            .order_by(Citation.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        citations = result.scalars().all()

        return CitationListResponse(
            citations=[CitationResponse.model_validate(c) for c in citations],
            total=total,
            skip=skip,
            limit=limit,
        )

    except Exception:
        logger.error(
            "citation_list_failed", exc_info=True, user_id=str(current_user.id)
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list citations",
        )


@router.get("/{citation_id}", response_model=CitationResponse)
async def get_citation(
    citation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get single citation detail with full snippet.

    Args:
        citation_id: Citation ID
        current_user: Authenticated user
        db: Database session

    Returns:
        Citation details
    """
    try:
        query = (
            select(Citation)
            .options(
                selectinload(Citation.document),
                selectinload(Citation.message)
                .selectinload(ChatMessage.thread)
                .selectinload(Thread.conversation)
                .selectinload(Conversation.workspace),
            )
            .where(Citation.id == citation_id, Citation.is_deleted.is_(False))
        )
        result = await db.execute(query)
        citation = result.scalar_one_or_none()

        if not citation or not _citation_is_accessible(citation, current_user):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Citation {citation_id} not found",
            )

        return CitationResponse.model_validate(citation)

    except HTTPException:
        raise
    except Exception:
        logger.error(
            "citation_get_failed",
            exc_info=True,
            citation_id=str(citation_id),
            user_id=str(current_user.id),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get citation",
        )


@router.post(
    "/extract", response_model=CitationResponse, status_code=status.HTTP_201_CREATED
)
async def extract_citation(
    request: Optional[CitationExtractRequest] = Body(None),
    document_id: Optional[UUID] = Query(
        None, description="Document ID to infer arXiv/DOI/title from metadata"
    ),
    arxiv_id: Optional[str] = Query(None, description="ArXiv ID"),
    doi: Optional[str] = Query(None, description="DOI"),
    title: Optional[str] = Query(None, description="Paper title"),
    strategy: str = Query(
        "auto",
        description="Extraction strategy (auto/arxiv/semantic_scholar/crossref/manual)",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Extract citation metadata using hybrid extraction pipeline.

    Args:
        arxiv_id: Optional ArXiv ID
        doi: Optional DOI
        title: Optional paper title
        strategy: Extraction strategy ("auto", "arxiv", "semantic_scholar", "crossref")
        current_user: Authenticated user
        db: Database session

    Returns:
        Extracted citation
    """
    request_document_id = request.document_id if request else None
    request_arxiv_id = request.arxiv_id if request else None
    request_doi = request.doi if request else None
    request_title = request.title if request else None
    request_strategy = request.strategy if request and request.strategy else None

    resolved_document_id = request_document_id or document_id
    resolved_arxiv_id = request_arxiv_id or arxiv_id
    resolved_doi = request_doi or doi
    resolved_title = request_title or title
    resolved_strategy = (request_strategy or strategy or "auto").lower()

    if (
        not resolved_document_id
        and not resolved_arxiv_id
        and not resolved_doi
        and not resolved_title
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide at least one of: document_id, arxiv_id, doi, or title",
        )

    try:
        extraction_service = CitationExtractionService(db)

        if resolved_document_id:
            # Tenant scope: verify the client-supplied document_id belongs to the
            # caller's organization before the extraction service reads its
            # metadata / PDF. Without this, another org's document title / DOI /
            # arXiv id (and PDF contents) leak. Mirrors the create_citation check;
            # 404 (not 403) so document ids can't be probed for existence.
            doc_check = await db.execute(
                select(Document.id).where(
                    Document.id == resolved_document_id,
                    Document.organization_id == current_user.organization_id,
                    Document.is_deleted == False,  # noqa: E712
                )
            )
            if doc_check.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Document not found or not accessible",
                )

            citation_data, source = await extraction_service.extract_for_document(
                document_id=resolved_document_id,
                strategy=resolved_strategy,
            )
        else:
            # Extract using hybrid strategy
            citation_data, source = await extraction_service.extract_hybrid(
                arxiv_id=resolved_arxiv_id,
                doi=resolved_doi,
                title=resolved_title,
                strategy=resolved_strategy,
            )

        if not citation_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Could not extract citation metadata from any source",
            )

        # Create citation in database
        citation = Citation(
            document_id=resolved_document_id or citation_data.document_id,
            document_title=citation_data.document_title,
            authors=citation_data.authors,
            year=citation_data.year,
            venue=citation_data.venue,
            doi=citation_data.doi,
            arxiv_id=citation_data.arxiv_id,
            abstract=citation_data.abstract,
            metadata_source=source,
            needs_review=bool(citation_data.needs_review),
        )

        db.add(citation)
        await db.commit()
        await db.refresh(citation)

        logger.info(
            "citation_extracted",
            citation_id=str(citation.id),
            source=source,
            strategy=resolved_strategy,
            document_id=str(resolved_document_id) if resolved_document_id else None,
            arxiv_id=resolved_arxiv_id,
            doi=resolved_doi,
        )

        return CitationResponse.model_validate(citation)

    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        logger.error("citation_extraction_failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to extract citation",
        )


@router.post("/lookup", response_model=CitationResponse)
async def lookup_citation(
    request: Optional[CitationLookupRequest] = Body(None),
    document_id: Optional[UUID] = Query(
        None, description="Document ID to infer arXiv/DOI/title from metadata"
    ),
    arxiv_id: Optional[str] = Query(None, description="ArXiv ID"),
    doi: Optional[str] = Query(None, description="DOI"),
    title: Optional[str] = Query(None, description="Paper title"),
    strategy: str = Query(
        "auto",
        description="Lookup strategy (auto/arxiv/semantic_scholar/crossref/manual)",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lookup citation by ArXiv ID, DOI, or title (no database persistence).

    Args:
        arxiv_id: Optional ArXiv ID
        doi: Optional DOI
        title: Optional paper title
        current_user: Authenticated user
        db: Database session

    Returns:
        Citation metadata (not persisted)
    """
    request_document_id = request.document_id if request else None
    request_arxiv_id = request.arxiv_id if request else None
    request_doi = request.doi if request else None
    request_title = request.title if request else None
    request_strategy = request.strategy if request and request.strategy else None

    resolved_document_id = request_document_id or document_id
    resolved_arxiv_id = request_arxiv_id or arxiv_id
    resolved_doi = request_doi or doi
    resolved_title = request_title or title
    resolved_strategy = (request_strategy or strategy or "auto").lower()

    if (
        not resolved_document_id
        and not resolved_arxiv_id
        and not resolved_doi
        and not resolved_title
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide at least one of: document_id, arxiv_id, doi, or title",
        )

    try:
        extraction_service = CitationExtractionService(db)

        if resolved_document_id:
            # Tenant scope: verify the client-supplied document_id belongs to the
            # caller's organization before the extraction service reads its
            # metadata / PDF. Without this, another org's document title / DOI /
            # arXiv id (and PDF contents) leak. Mirrors the create_citation check;
            # 404 (not 403) so document ids can't be probed for existence.
            doc_check = await db.execute(
                select(Document.id).where(
                    Document.id == resolved_document_id,
                    Document.organization_id == current_user.organization_id,
                    Document.is_deleted == False,  # noqa: E712
                )
            )
            if doc_check.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Document not found or not accessible",
                )

            citation_data, source = await extraction_service.extract_for_document(
                document_id=resolved_document_id,
                strategy=resolved_strategy,
            )
        else:
            citation_data, source = await extraction_service.extract_hybrid(
                arxiv_id=resolved_arxiv_id,
                doi=resolved_doi,
                title=resolved_title,
                strategy=resolved_strategy,
            )

        if not citation_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Citation not found"
            )

        # Return citation without persisting
        now = datetime.now(timezone.utc)
        return CitationResponse.model_validate(
            {
                "id": UUID("00000000-0000-0000-0000-000000000000"),  # Placeholder ID
                "document_id": resolved_document_id or citation_data.document_id,
                "document_title": citation_data.document_title,
                "document_type": "paper",
                "authors": citation_data.authors or [],
                "year": citation_data.year,
                "venue": citation_data.venue,
                "doi": citation_data.doi,
                "arxiv_id": citation_data.arxiv_id,
                "abstract": citation_data.abstract,
                "score": 0.0,
                "metadata_source": source,
                "needs_review": bool(citation_data.needs_review),
                "created_at": now,
                "updated_at": now,
            }
        )

    except HTTPException:
        raise
    except Exception:
        logger.error("citation_lookup_failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to lookup citation",
        )


@router.post("/export")
async def export_bibliography(
    request: Optional[BibliographyExportRequest] = Body(None),
    format: str = Query("bibtex"),
    citation_ids: Optional[List[UUID]] = Query(None),
    project_id: Optional[UUID] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export bibliography in specified format.

    Args:
        format: Bibliography format ("bibtex", "ieee", "apa", "mla")
        citation_ids: Optional list of citation IDs to export
        project_id: Optional project ID to export all citations from
        current_user: Authenticated user
        db: Database session

    Returns:
        Formatted bibliography as plain text
    """
    if not isinstance(request, BibliographyExportRequest):
        request = None

    request_format = request.format if request and request.format else None
    request_citation_ids = request.citation_ids if request else None
    request_project_id = request.project_id if request else None

    resolved_format = (request_format or format or "bibtex").lower()
    resolved_citation_ids = (
        request_citation_ids if request_citation_ids is not None else citation_ids
    )
    resolved_project_id = request_project_id or project_id

    if not resolved_citation_ids and not resolved_project_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide either citation_ids or project_id",
        )

    try:
        if resolved_project_id:
            await _ensure_project_access(resolved_project_id, current_user, db)

        # Fetch citations
        query = (
            select(Citation)
            .options(
                selectinload(Citation.document),
                selectinload(Citation.message)
                .selectinload(ChatMessage.thread)
                .selectinload(Thread.conversation)
                .selectinload(Conversation.workspace),
            )
            .where(Citation.is_deleted.is_(False))
        )

        document_ids: list = []
        if resolved_citation_ids:
            query = query.where(Citation.id.in_(resolved_citation_ids))
        elif resolved_project_id:
            # Get all document IDs in this project via CollectionDocument junction table
            doc_query = select(CollectionDocument.document_id).where(
                CollectionDocument.collection_id == resolved_project_id,
                CollectionDocument.is_deleted.is_(False),
            )
            doc_result = await db.execute(doc_query)
            document_ids = [row[0] for row in doc_result.all()]

            if not document_ids:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No documents found in project",
                )

            # Filter citations by documents in the project
            query = query.where(Citation.document_id.in_(document_ids))

        result = await db.execute(query)
        citations = [
            citation
            for citation in result.scalars().all()
            if _citation_is_accessible(citation, current_user)
        ]

        # Fallback: build bibliography from Document metadata when no
        # Citation records exist (common for freshly ingested papers).
        # GOO-349: a project-document link can be stale (document moved or
        # foreign), so the fallback re-applies the org document boundary.
        if (
            not citations
            and resolved_project_id
            and current_user.organization_id is not None
        ):
            from src.services.agent.tools_impl import _citations_from_documents

            doc_stmt = select(Document).where(
                Document.id.in_(document_ids),
                Document.is_deleted == False,
                Document.organization_id == current_user.organization_id,
            )
            doc_result = await db.execute(doc_stmt)
            docs = list(doc_result.scalars().all())
            citations = _citations_from_documents(docs)  # type: ignore[assignment]

        if not citations:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No citations found"
            )

        # Format bibliography
        bibliography = BibliographyService.format_bibliography(
            citations=list(citations), format_type=resolved_format  # type: ignore[arg-type]
        )

        logger.info(
            "bibliography_exported",
            format=resolved_format,
            citation_count=len(citations),
            user_id=str(current_user.id),
        )

        # Return as plain text with appropriate content type
        from fastapi.responses import PlainTextResponse

        content_type = "text/plain"
        extension = resolved_format
        if resolved_format == "bibtex":
            content_type = "application/x-bibtex"
            extension = "bib"

        return PlainTextResponse(
            content=bibliography,
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="bibliography.{extension}"'
            },
        )

    except HTTPException:
        raise
    except ValueError:
        logger.warning("Invalid bibliography export request", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid bibliography export request",
        )
    except Exception:
        logger.error("bibliography_export_failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to export bibliography",
        )


# =========================================================================
# Citation Graph Endpoints (T053-T054)
# =========================================================================


@router.get("/relationships")
async def list_citation_relationships(
    source_id: Optional[UUID] = Query(None, description="Filter by source citation ID"),
    target_id: Optional[UUID] = Query(None, description="Filter by target citation ID"),
    relationship_type: Optional[str] = Query(
        None, description="Filter by relationship type"
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List citation relationships.

    Args:
        source_id: Optional source citation filter
        target_id: Optional target citation filter
        relationship_type: Optional relationship type filter
        current_user: Authenticated user
        db: Database session

    Returns:
        List of citation relationships
    """
    from src.models import CitationRelationship

    if relationship_type:
        relationship_type = _normalize_citation_relationship_type(relationship_type)

    # Require an anchor citation and verify the caller can access it — otherwise
    # this endpoint enumerated every tenant's citation relationships (including
    # citation_context text). Relationships hang off the anchor the user owns.
    if not source_id and not target_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="source_id or target_id is required",
        )
    for anchor_id in (source_id, target_id):
        if anchor_id:
            await _load_accessible_citation(anchor_id, current_user, db)

    try:
        query = select(CitationRelationship)

        filters = []
        if source_id:
            filters.append(CitationRelationship.source_citation_id == source_id)
        if target_id:
            filters.append(CitationRelationship.target_citation_id == target_id)
        if relationship_type:
            filters.append(CitationRelationship.relationship_type == relationship_type)

        if filters:
            query = query.where(and_(*filters))

        result = await db.execute(query)
        relationships = result.scalars().all()

        return {
            "relationships": [
                {
                    "id": str(r.id),
                    "source_citation_id": str(r.source_citation_id),
                    "target_citation_id": str(r.target_citation_id),
                    "relationship_type": r.relationship_type,
                    "citation_context": r.citation_context,
                    "confidence": r.confidence,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in relationships
            ],
            "total": len(relationships),
        }

    except Exception:
        logger.error("list_relationships_failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list relationships",
        )


@router.post("/relationships", status_code=status.HTTP_201_CREATED)
async def create_citation_relationship(
    source_citation_id: UUID,
    target_citation_id: UUID,
    relationship_type: str = "CITES",
    citation_context: Optional[str] = None,
    confidence: float = 1.0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a citation relationship.

    Args:
        source_citation_id: The citing paper
        target_citation_id: The cited paper
        relationship_type: Type of relationship (CITES, EXTENDS, CONTRADICTS)
        citation_context: Optional text context
        confidence: Confidence score
        current_user: Authenticated user
        db: Database session

    Returns:
        Created relationship
    """
    from src.models import CitationRelationship
    from src.services.research.citation_graph_service import get_citation_graph_service

    relationship_type = _normalize_citation_relationship_type(relationship_type)

    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="confidence must be a finite number between 0 and 1",
        )

    if source_citation_id == target_citation_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source and target citations cannot be the same",
        )

    try:
        # Verify both citations exist AND are accessible to the caller —
        # existence-only let a user link/pollute arbitrary cross-tenant
        # citations by id.
        await _load_accessible_citation(source_citation_id, current_user, db)
        await _load_accessible_citation(target_citation_id, current_user, db)

        # Create relationship in PostgreSQL
        relationship = CitationRelationship(
            source_citation_id=source_citation_id,
            target_citation_id=target_citation_id,
            relationship_type=relationship_type,
            citation_context=citation_context,
            confidence=confidence,
        )

        db.add(relationship)
        await db.commit()
        await db.refresh(relationship)

        # Sync to Neo4j graph
        try:
            graph_service = await get_citation_graph_service()
            await graph_service.create_cites_relationship(
                source_citation_id=source_citation_id,
                target_citation_id=target_citation_id,
                relationship_type=relationship_type,
                citation_context=citation_context,
                confidence=confidence,
            )
        except Exception as graph_error:
            logger.warning(
                "neo4j_sync_failed",
                error=str(graph_error),
                relationship_id=str(relationship.id),
            )

        logger.info(
            "citation_relationship_created",
            relationship_id=str(relationship.id),
            source=str(source_citation_id),
            target=str(target_citation_id),
            type=relationship_type,
        )

        return {
            "id": str(relationship.id),
            "source_citation_id": str(relationship.source_citation_id),
            "target_citation_id": str(relationship.target_citation_id),
            "relationship_type": relationship.relationship_type,
            "citation_context": relationship.citation_context,
            "confidence": relationship.confidence,
            "created_at": (
                relationship.created_at.isoformat() if relationship.created_at else None
            ),
        }

    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        logger.error("create_relationship_failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create relationship",
        )


@router.get("/graph")
async def get_citation_graph(
    project_id: Optional[UUID] = Query(None, description="Filter by project ID"),
    document_id: Optional[UUID] = Query(None, description="Filter by document ID"),
    depth: int = Query(2, ge=1, le=5, description="Graph traversal depth"),
    include_external: bool = Query(True, description="Include external papers"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get citation graph data for visualization.

    Args:
        project_id: Optional project filter
        document_id: Optional document filter
        depth: How many levels of citations to traverse
        include_external: Include non-uploaded papers
        current_user: Authenticated user

    Returns:
        Graph data with nodes, edges, and metadata
    """
    from src.services.research.citation_graph_service import get_citation_graph_service

    # Require an anchor and verify ownership — an unscoped graph traversed every
    # tenant's citations. project_id → workspace owner; document_id → org.
    if not project_id and not document_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="project_id or document_id is required",
        )
    if project_id:
        await _ensure_project_access(project_id, current_user, db)
    if document_id:
        doc_check = await db.execute(
            select(Document.id).where(
                Document.id == document_id,
                Document.organization_id == current_user.organization_id,
                Document.is_deleted == False,
            )
        )
        if doc_check.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Document not found",
            )

    try:
        graph_service = await get_citation_graph_service()

        graph_data = await graph_service.get_citation_graph(
            project_id=project_id,
            document_id=document_id,
            depth=depth,
            include_external=include_external,
            organization_id=str(current_user.organization_id),
        )

        logger.info(
            "citation_graph_retrieved",
            project_id=str(project_id) if project_id else None,
            document_id=str(document_id) if document_id else None,
            node_count=len(graph_data.get("nodes", [])),
            edge_count=len(graph_data.get("edges", [])),
        )

        return graph_data

    except HTTPException:
        # Preserve authz 400/404s — never let them collapse into a 500 if a
        # future edit moves a guard inside this try.
        raise
    except Exception:
        logger.error("get_graph_failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get citation graph",
        )


@router.get("/graph/node/{citation_id}")
async def get_graph_node_details(
    citation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get detailed information about a graph node.

    Args:
        citation_id: The citation ID
        current_user: Authenticated user

    Returns:
        Node details with citation counts and influence score
    """
    from src.services.research.citation_graph_service import get_citation_graph_service

    # Verify the caller can access this citation before returning node details.
    await _load_accessible_citation(citation_id, current_user, db)

    try:
        graph_service = await get_citation_graph_service()

        node_details = await graph_service.get_node_details(citation_id)

        if not node_details:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Node {citation_id} not found in graph",
            )

        return node_details

    except HTTPException:
        raise
    except Exception:
        logger.error(
            "get_node_details_failed", exc_info=True, citation_id=str(citation_id)
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get node details",
        )
