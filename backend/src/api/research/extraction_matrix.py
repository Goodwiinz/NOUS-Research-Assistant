"""
Extraction Matrix API Router
Literature review extraction matrix management and data extraction endpoints.

Security: All endpoints require authentication. Project-scoped endpoints
validate project ownership before granting access.
"""

import uuid
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from structlog import get_logger

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models import Collection, CollectionDocument, Workspace
from src.models.document import Document
from src.models.extraction_matrix import ExtractionCell, ExtractionMatrix
from src.models.user import User
from src.services.research import extraction_forms_service
from src.services.research.extraction_matrix_service import ExtractionMatrixService
from src.services.research_engine.project_access import (
    ResearchAction,
    project_documents_query,
    resolve_project,
)
from src.shared.scispace_schemas import (
    CreateMatrixRequest,
    ExtractionCellResponse,
    TriggerExtractionRequest,
    UpdateMatrixRequest,
)
from src.tasks.research_tasks import run_extraction_matrix

logger = get_logger()


def _scoped_document_query(doc_id, project_id):
    """Build a Document fetch constrained to a project's collection.

    A document is only returned when it is a member of ``project_id``'s
    collection (collection_documents). Callers must have already verified they
    own ``project_id``; this drops any client-supplied document id that is not
    actually in that project, preventing cross-tenant/cross-project reads.
    """
    from src.services.research.extraction_matrix_service import (
        _scoped_document_query as scoped_query,
    )

    return scoped_query(doc_id, project_id)


router = APIRouter(prefix="/api/v1/research", tags=["extraction-matrix"])


# ============================================================================
# Security Helper
# ============================================================================


async def _validate_project_ownership(
    project_id: UUID,
    current_user: User,
    db: AsyncSession,
    action: ResearchAction = ResearchAction.VIEW,
) -> Collection:
    """Apply the canonical project authorization and lifecycle boundary."""
    return (await resolve_project(db, project_id, current_user.id, action)).collection


# ============================================================================
# List Matrices for Project
# ============================================================================


@router.get("/projects/{project_id}/matrices")
async def list_matrices(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all extraction matrices for a project."""
    await _validate_project_ownership(project_id, current_user, db)

    query = (
        select(ExtractionMatrix)
        .where(
            ExtractionMatrix.project_id == project_id,
            ExtractionMatrix.is_deleted.is_(False),
        )
        .order_by(ExtractionMatrix.created_at.desc())
    )
    result = await db.execute(query)
    matrices = result.scalars().all()

    return {
        "matrices": [
            {
                "id": str(m.id),
                "project_id": str(m.project_id),
                "name": m.name,
                "columns": m.columns,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in matrices
        ],
        "total": len(matrices),
    }


# ============================================================================
# Create Matrix (with auto-extraction)
# ============================================================================


@router.post("/projects/{project_id}/matrices", status_code=201)
async def create_matrix(
    project_id: UUID,
    request: CreateMatrixRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new extraction matrix for a project.

    Defines column headers (e.g. Methodology, Sample Size) that will be
    used to extract structured data from project documents.
    Automatically triggers extraction for all existing project documents.
    """
    context = await resolve_project(
        db, project_id, current_user.id, ResearchAction.EDIT
    )
    columns = [col.model_dump() for col in request.columns]
    matrix = ExtractionMatrix(project_id=project_id, name=request.name, columns=columns)
    db.add(matrix)
    # GOO-304: v1 (authored) lands in the same transaction as the matrix.
    await extraction_forms_service.create_version(
        db, context, matrix, columns, current_user.id
    )

    logger.info(
        "matrix_created",
        matrix_id=str(matrix.id),
        project_id=str(project_id),
        user_id=str(current_user.id),
    )

    # Auto-extract: query all documents in the project
    extraction_task_id = None
    doc_query = project_documents_query(project_id).with_only_columns(Document.id)
    doc_result = await db.execute(doc_query)
    document_ids = [row[0] for row in doc_result.all()]

    if document_ids:
        extraction_task_id = f"auto-create-{uuid.uuid4().hex[:12]}"
        kwargs = await extraction_forms_service.extraction_task_kwargs(
            db, matrix, document_ids, current_user.id, extraction_task_id
        )
        run_extraction_matrix.apply_async(
            kwargs=kwargs,
            task_id=extraction_task_id,
            queue="low_priority",
        )
        logger.info(
            "auto_extraction_started",
            matrix_id=str(matrix.id),
            task_id=extraction_task_id,
            document_count=len(document_ids),
        )

    return {
        "id": str(matrix.id),
        "project_id": str(matrix.project_id),
        "name": matrix.name,
        "columns": matrix.columns,
        "created_at": matrix.created_at.isoformat() if matrix.created_at else None,
        "extraction_task_id": extraction_task_id,
    }


# ============================================================================
# Get Matrix with Cells
# ============================================================================


@router.get("/matrices/{matrix_id}")
async def get_matrix(
    matrix_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get an extraction matrix with all its cells.

    Returns the matrix definition and all extracted cell values,
    organized for rendering as a comparison grid.
    """
    query = (
        select(ExtractionMatrix)
        .options(selectinload(ExtractionMatrix.cells))
        .where(
            and_(
                ExtractionMatrix.id == matrix_id,
                ExtractionMatrix.is_deleted == False,
            )
        )
    )
    result = await db.execute(query)
    matrix = result.scalar_one_or_none()

    if not matrix:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Matrix not found",
        )

    # Validate ownership via project
    await _validate_project_ownership(matrix.project_id, current_user, db)

    allowed_ids = set(
        (
            await db.execute(
                project_documents_query(matrix.project_id).with_only_columns(
                    Document.id
                )
            )
        )
        .scalars()
        .all()
    )
    cells = [
        {
            "document_id": str(cell.document_id),
            "column_name": cell.column_name,
            "value": cell.value,
            "citation_snippet": cell.citation_snippet,
            "confidence": cell.confidence,
        }
        for cell in matrix.cells
        if not cell.is_deleted and cell.document_id in allowed_ids
    ]

    return {
        "id": str(matrix.id),
        "project_id": str(matrix.project_id),
        "name": matrix.name,
        "columns": matrix.columns,
        "cells": cells,
        "created_at": matrix.created_at.isoformat() if matrix.created_at else None,
        "updated_at": matrix.updated_at.isoformat() if matrix.updated_at else None,
    }


# ============================================================================
# Update Matrix (PATCH)
# ============================================================================


@router.patch("/matrices/{matrix_id}")
async def update_matrix(
    matrix_id: UUID,
    request: UpdateMatrixRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update an extraction matrix (rename, add/remove/reorder columns).

    When clear_stale_cells is true and columns changed, cells whose
    column_name no longer matches any column are deleted.
    """
    # GOO-304: a column change appends a form version (identical columns
    # create nothing) and prior values are never deleted: clear_stale_cells
    # only reports documents holding values on removed columns. The public
    # description is updated with the OpenAPI regeneration in the routes task.
    query = (
        select(ExtractionMatrix)
        .options(selectinload(ExtractionMatrix.cells))
        .where(
            and_(
                ExtractionMatrix.id == matrix_id,
                ExtractionMatrix.is_deleted == False,
            )
        )
    )
    result = await db.execute(query)
    matrix = result.scalar_one_or_none()

    if not matrix:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Matrix not found",
        )

    context = await resolve_project(
        db, matrix.project_id, current_user.id, ResearchAction.EDIT
    )

    columns_changed = False
    stale_document_ids: List[str] = []

    if request.name is not None:
        matrix.name = request.name

    if request.columns is None:
        await db.commit()
    else:
        old_col_names = {c["name"] for c in matrix.columns}
        new_col_names = {c.name for c in request.columns}
        columns_changed = old_col_names != new_col_names

        if request.clear_stale_cells and columns_changed:
            stale_document_ids = (
                await extraction_forms_service.removed_field_document_ids(
                    db, matrix, sorted(old_col_names - new_col_names)
                )
            )
        # Commits the rename too, in the same transaction as the version.
        await extraction_forms_service.create_version(
            db,
            context,
            matrix,
            [col.model_dump() for col in request.columns],
            current_user.id,
        )

    logger.info(
        "matrix_updated",
        matrix_id=str(matrix_id),
        user_id=str(current_user.id),
        columns_changed=columns_changed,
    )

    return {
        "id": str(matrix.id),
        "project_id": str(matrix.project_id),
        "name": matrix.name,
        "columns": matrix.columns,
        "columns_changed": columns_changed,
        "stale_document_ids": stale_document_ids,
        "updated_at": matrix.updated_at.isoformat() if matrix.updated_at else None,
    }


# ============================================================================
# Trigger Extraction
# ============================================================================


@router.post("/matrices/{matrix_id}/extract", status_code=202)
async def trigger_extraction(
    matrix_id: UUID,
    request: TriggerExtractionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Trigger LLM extraction for selected documents.

    Processes each document against the matrix columns and stores
    the extracted values as cells. Returns 202 Accepted since
    extraction may take time for large document sets.
    """
    query = select(ExtractionMatrix).where(
        and_(
            ExtractionMatrix.id == matrix_id,
            ExtractionMatrix.is_deleted == False,
        )
    )
    result = await db.execute(query)
    matrix = result.scalar_one_or_none()

    if not matrix:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Matrix not found",
        )

    await _validate_project_ownership(
        matrix.project_id, current_user, db, ResearchAction.EDIT
    )

    logger.info(
        "extraction_triggered",
        matrix_id=str(matrix_id),
        document_count=len(request.document_ids),
        user_id=str(current_user.id),
    )

    from src.core.config import settings

    azure_key = settings.AZURE_OPENAI_CHAT_API_KEY or settings.AZURE_OPENAI_API_KEY
    azure_endpoint = (
        settings.AZURE_OPENAI_CHAT_ENDPOINT or settings.AZURE_OPENAI_ENDPOINT
    )
    openai_key = settings.OPENAI_API_KEY

    if not ((azure_key and azure_endpoint) or openai_key):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No OpenAI or Azure OpenAI API key configured.",
        )

    # R5-M23/R5-L16: the LLM loop ran INLINE — a large matrix pinned the
    # request for tens of minutes and client retries re-paid the spend.
    # Offload to a tracked background task; 202 now tells the truth.
    if len(request.document_ids) > 10:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Maximum 10 documents per extraction batch",
        )

    task_id = str(uuid.uuid4())
    await ExtractionMatrixService.set_extraction_status(
        task_id,
        {
            "matrix_id": str(matrix_id),
            "status": "running",
            "extracted": 0,
            "total": len(request.document_ids),
        },
    )
    kwargs = await extraction_forms_service.extraction_task_kwargs(
        db, matrix, request.document_ids, current_user.id, task_id
    )
    run_extraction_matrix.apply_async(
        kwargs=kwargs,
        task_id=task_id,
        queue="low_priority",
    )

    return {
        "task_id": task_id,
        "matrix_id": str(matrix_id),
        "status": "accepted",
        "message": f"Extraction started for {len(request.document_ids)} document(s)",
    }


# ============================================================================
# Extraction Task Status
# ============================================================================


@router.get("/extraction-tasks/{task_id}")
async def get_extraction_task_status(
    task_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a task only through its authorized, retained matrix."""
    task_status = await ExtractionMatrixService.get_extraction_status(task_id)

    if not task_status:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Extraction task not found",
        )

    matrix = (
        await db.execute(
            select(ExtractionMatrix).where(
                ExtractionMatrix.id == task_status.get("matrix_id"),
                ExtractionMatrix.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if matrix is None:
        raise HTTPException(status_code=404, detail="Extraction task not found")
    await _validate_project_ownership(matrix.project_id, current_user, db)
    return task_status


# ============================================================================
# Delete Matrix
# ============================================================================


@router.delete("/matrices/{matrix_id}")
async def delete_matrix(
    matrix_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete an extraction matrix and all its cells."""
    query = select(ExtractionMatrix).where(
        and_(
            ExtractionMatrix.id == matrix_id,
            ExtractionMatrix.is_deleted == False,
        )
    )
    result = await db.execute(query)
    matrix = result.scalar_one_or_none()

    if not matrix:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Matrix not found",
        )

    await _validate_project_ownership(
        matrix.project_id, current_user, db, ResearchAction.EDIT
    )

    matrix.soft_delete()
    await db.commit()

    logger.info(
        "matrix_deleted",
        matrix_id=str(matrix_id),
        user_id=str(current_user.id),
    )

    return {"message": "Matrix deleted", "matrix_id": str(matrix_id)}
