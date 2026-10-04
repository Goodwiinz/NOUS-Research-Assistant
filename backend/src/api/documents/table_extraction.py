"""Table Extraction API Router - extract tables and math from PDF documents."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

from src.core.database import get_db
from src.core.dependencies import get_current_organization, get_current_user
from src.models.document import Document
from src.models.organization import Organization
from src.models.user import User
from src.services.processing.table_extraction_service import TableExtractionService

logger = get_logger()
router = APIRouter(prefix="/api/v1/documents", tags=["table-extraction"])

_service = TableExtractionService()


# ============================================================================
# Request schemas
# ============================================================================


class ExtractRegionRequest(BaseModel):
    page: int = Field(..., ge=1)
    x1: float = Field(..., ge=0)
    y1: float = Field(..., ge=0)
    x2: float = Field(..., ge=0)
    y2: float = Field(..., ge=0)


# ============================================================================
# GET /api/v1/documents/{document_id}/tables
# ============================================================================


@router.get("/{document_id}/tables")
async def extract_tables(
    document_id: UUID,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """Extract all tables from a PDF document using Camelot.

    Attempts lattice mode first, falls back to stream mode. Returns an empty
    list if Camelot is not installed or no tables are found.
    """
    doc_result = await db.execute(
        select(Document).where(
            and_(
                Document.id == document_id,
                Document.organization_id == organization.id,
                Document.is_deleted == False,
            )
        )
    )
    document = doc_result.scalar_one_or_none()
    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )

    if not document.file_path or not document.file_path.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Table extraction is only supported for PDF documents",
        )

    try:
        tables = await _service.extract_tables_from_pdf(document)
    except Exception as exc:
        logger.error(
            "table_extraction_failed",
            document_id=str(document_id),
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to extract tables from document",
        )

    return {
        "document_id": str(document_id),
        "table_count": len(tables),
        "tables": tables,
    }


# ============================================================================
# POST /api/v1/documents/{document_id}/extract-region
# ============================================================================


@router.post("/{document_id}/extract-region")
async def extract_region(
    document_id: UUID,
    body: ExtractRegionRequest,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """Extract content from a specific bounding-box region of a PDF page.

    Uses PyMuPDF to crop the region, attempts text extraction, and falls back
    to GPT-4o Vision for complex layouts and math.
    """
    doc_result = await db.execute(
        select(Document).where(
            and_(
                Document.id == document_id,
                Document.organization_id == organization.id,
                Document.is_deleted == False,
            )
        )
    )
    document = doc_result.scalar_one_or_none()
    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )

    if not document.file_path or not document.file_path.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Region extraction is only supported for PDF documents",
        )

    try:
        result = await _service.extract_region(
            document=document,
            page=body.page,
            x1=body.x1,
            y1=body.y1,
            x2=body.x2,
            y2=body.y2,
        )
    except ValueError:
        logger.warning("Invalid extraction region", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid extraction region",
        )
    except Exception as exc:
        logger.error(
            "region_extraction_failed",
            document_id=str(document_id),
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to extract region from document",
        )

    return {
        "document_id": str(document_id),
        "page": body.page,
        "region": {"x1": body.x1, "y1": body.y1, "x2": body.x2, "y2": body.y2},
        **result,
    }
