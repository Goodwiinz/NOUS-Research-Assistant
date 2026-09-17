"""
Export API endpoints for thread/conversation export.

Provides endpoints for:
- Single thread export (Markdown, PDF, JSON, HTML)
- Batch thread export with ZIP packaging
- Export status and metadata
"""

import io
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.services.research.export_service import (
    ExportService,
    PDFExportConversionError,
    PDFExportUnavailableError,
    get_export_service,
)
from src.shared.export_schemas import (
    BatchExportRequest,
    ExportError,
    ExportFormat,
    ExportOptions,
    ExportRequest,
    ExportResponse,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/export", tags=["export"])


@router.post(
    "/thread/{thread_id}",
    response_class=Response,
    responses={
        200: {
            "description": "Exported thread file",
            "content": {
                "text/markdown": {},
                "text/html": {},
                "application/json": {},
                "application/pdf": {},
            },
        },
        404: {"model": ExportError, "description": "Thread not found"},
        503: {"model": ExportError, "description": "PDF renderer unavailable"},
        500: {"model": ExportError, "description": "Export failed"},
    },
    summary="Export single thread",
    description="Export a thread to Markdown, PDF, JSON, or HTML format.",
)
async def export_thread(
    thread_id: str,
    format: ExportFormat = Query(
        default=ExportFormat.MARKDOWN, description="Export format"
    ),
    include_system_messages: bool = Query(
        default=False, description="Include system messages"
    ),
    include_citations: bool = Query(default=True, description="Include citations"),
    include_metadata: bool = Query(default=True, description="Include metadata"),
    include_feedback: bool = Query(default=False, description="Include feedback"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Export a single thread to the specified format."""
    options = ExportOptions(
        include_system_messages=include_system_messages,
        include_citations=include_citations,
        include_metadata=include_metadata,
        include_feedback=include_feedback,
    )

    try:
        export_service = get_export_service(db)
        content, filename, content_type = await export_service.export_thread(
            thread_id=thread_id,
            user_id=str(current_user.id),
            format=format,
            options=options,
        )

        logger.info(
            "Thread exported via API",
            thread_id=thread_id,
            user_id=str(current_user.id),
            format=format.value,
            size_bytes=len(content),
        )

        return Response(
            content=content,
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Export-Format": format.value,
                "X-Content-Length": str(len(content)),
            },
        )

    except PDFExportUnavailableError as exc:
        logger.warning(
            "PDF renderer unavailable",
            thread_id=thread_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail=str(exc))
    except PDFExportConversionError as exc:
        logger.error(
            "PDF conversion failed",
            thread_id=thread_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail=str(exc))
    except ValueError as e:
        logger.warning(
            "Export failed - thread not found", thread_id=thread_id, error=str(e)
        )
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Export failed", thread_id=thread_id, error=str(e))
        raise HTTPException(status_code=500, detail="Export failed")


@router.post(
    "/thread/{thread_id}/stream",
    response_class=StreamingResponse,
    responses={
        404: {"model": ExportError, "description": "Thread not found"},
        503: {"model": ExportError, "description": "PDF renderer unavailable"},
        500: {"model": ExportError, "description": "Export failed"},
    },
    summary="Export thread with streaming",
    description="Stream export for large threads to avoid timeout.",
)
async def export_thread_stream(
    thread_id: str,
    format: ExportFormat = Query(default=ExportFormat.MARKDOWN),
    include_citations: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stream export for large threads."""
    options = ExportOptions(include_citations=include_citations)

    try:
        export_service = get_export_service(db)
        content, filename, content_type = await export_service.export_thread(
            thread_id=thread_id,
            user_id=str(current_user.id),
            format=format,
            options=options,
        )

        def content_stream():
            chunk_size = 8192
            buffer = io.BytesIO(content)
            while chunk := buffer.read(chunk_size):
                yield chunk

        return StreamingResponse(
            content_stream(),
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Export-Format": format.value,
            },
        )

    except PDFExportUnavailableError as exc:
        logger.warning(
            "PDF renderer unavailable",
            thread_id=thread_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail=str(exc))
    except PDFExportConversionError as exc:
        logger.error(
            "PDF conversion failed",
            thread_id=thread_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail=str(exc))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Streaming export failed", thread_id=thread_id, error=str(e))
        raise HTTPException(status_code=500, detail="Export failed")


@router.post(
    "/batch",
    response_class=Response,
    responses={
        200: {
            "description": "ZIP file containing exported threads",
            "content": {"application/zip": {}},
        },
        400: {"model": ExportError, "description": "Invalid request"},
        503: {"model": ExportError, "description": "PDF renderer unavailable"},
        500: {"model": ExportError, "description": "Export failed"},
    },
    summary="Batch export threads",
    description="Export multiple threads as a ZIP archive.",
)
async def export_batch(
    request: BatchExportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Export multiple threads as a ZIP file."""
    if len(request.thread_ids) > 100:
        raise HTTPException(
            status_code=400, detail="Maximum 100 threads per batch export"
        )

    try:
        export_service = get_export_service(db)
        content, filename, content_type = await export_service.export_batch(
            thread_ids=request.thread_ids,
            user_id=str(current_user.id),
            format=request.format,
            options=request.options,
            as_zip=request.as_zip,
        )

        logger.info(
            "Batch export completed via API",
            thread_count=len(request.thread_ids),
            user_id=str(current_user.id),
            format=request.format.value,
            size_bytes=len(content),
        )

        return Response(
            content=content,
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Thread-Count": str(len(request.thread_ids)),
                "X-Export-Format": request.format.value,
            },
        )

    except PDFExportUnavailableError as exc:
        logger.warning(
            "PDF renderer unavailable",
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail=str(exc))
    except PDFExportConversionError as exc:
        logger.error(
            "PDF conversion failed",
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail=str(exc))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Batch export failed", error=str(e))
        raise HTTPException(status_code=500, detail="Batch export failed")


@router.get(
    "/formats",
    response_model=dict,
    summary="List available export formats",
    description="Get list of supported export formats with metadata.",
)
async def list_export_formats():
    """List available export formats."""
    return {
        "formats": [
            {
                "id": ExportFormat.MARKDOWN.value,
                "name": "Markdown",
                "extension": "md",
                "content_type": "text/markdown",
                "description": "Human-readable format with citations",
            },
            {
                "id": ExportFormat.HTML.value,
                "name": "HTML",
                "extension": "html",
                "content_type": "text/html",
                "description": "Self-contained viewable web page",
            },
            {
                "id": ExportFormat.JSON.value,
                "name": "JSON",
                "extension": "json",
                "content_type": "application/json",
                "description": "Complete data for re-import or analysis",
            },
            {
                "id": ExportFormat.PDF.value,
                "name": "PDF",
                "extension": "pdf",
                "content_type": "application/pdf",
                "description": "Formatted document (requires WeasyPrint)",
            },
        ],
        "options": {
            "include_system_messages": "Include system messages in export",
            "include_citations": "Include citation references and snippets",
            "include_attachments": "Include attachment metadata",
            "include_metadata": "Include message metadata (timestamps, model info)",
            "include_feedback": "Include user feedback ratings",
        },
        "limits": {
            "max_batch_size": 100,
            "max_thread_messages": 10000,
        },
    }


@router.post(
    "/preview/{thread_id}",
    response_model=dict,
    summary="Preview export metadata",
    description="Get export preview without generating the full file.",
)
async def preview_export(
    thread_id: str,
    format: ExportFormat = Query(default=ExportFormat.MARKDOWN),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Preview export metadata before downloading."""
    options = ExportOptions()

    try:
        export_service = get_export_service(db)
        thread = await export_service._load_thread(
            thread_id=thread_id, user_id=str(current_user.id), options=options
        )

        if not thread:
            raise HTTPException(status_code=404, detail="Thread not found")

        # Calculate estimated sizes.
        # These count the ThreadExport's message LIST, which ``_load_thread``
        # has already stripped of edit-superseded turns — deliberately NOT
        # ``thread.message_count`` (the denormalised DB counter), which would
        # promise the download more turns than it contains.
        message_count = len(thread.messages)
        citation_count = sum(len(m.citations) for m in thread.messages)

        return {
            "thread_id": thread_id,
            "title": thread.title,
            "format": format.value,
            "message_count": message_count,
            "citation_count": citation_count,
            "estimated_size_bytes": message_count * 500
            + citation_count * 200,  # Rough estimate
            "exportable": True,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Export preview failed", thread_id=thread_id, error=str(e))
        raise HTTPException(status_code=500, detail="Preview failed")
