"""Search import, citation chase and corpus export API (GOO-300).

Transport only: authorization through ``resolve_project`` (EDIT for imports
and chases, VIEW for reads and exports), persistence in ``corpus_service``.
Services never commit, so each mutating route ends its transaction here with
one ``commit``. Exports and coverage are read-only and never commit.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.research_engine import (
    CitationChaseRequest,
    CoverageRequest,
    CoverageResponse,
    ImportDeclaration,
    ImportReceiptDetail,
    ImportReceiptResponse,
)
from src.services.research_engine import search_import
from src.services.research_engine.corpus_export import build_package, coverage, render
from src.services.research_engine.corpus_service import (
    chase_citations,
    get_receipt,
    import_file,
    list_receipts,
)
from src.services.research_engine.project_access import ResearchAction, resolve_project

router = APIRouter(prefix="/research-engine", tags=["research-engine-corpus"])

_CREATED_OR_REPLAYED: dict[int | str, dict[str, Any]] = {
    200: {"model": ImportReceiptResponse, "description": "Replayed receipt"}
}


@router.post(
    "/projects/{project_id}/imports",
    response_model=ImportReceiptResponse,
    status_code=201,
    responses=_CREATED_OR_REPLAYED,
)
async def import_search_results_route(
    project_id: UUID,
    response: Response,
    file: UploadFile = File(...),
    import_format: str = Form(..., alias="format"),
    declaration: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ImportReceiptResponse:
    """Import one exported result file (RIS, CSV, NBIB) as an immutable receipt."""
    try:
        declared = ImportDeclaration.model_validate_json(declaration)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_declaration",
                "message": "The declaration is not a valid import declaration.",
            },
        ) from exc
    # Never read an unbounded upload: one byte past the cap is enough to refuse.
    data = await file.read(search_import.MAX_BYTES + 1)
    if len(data) > search_import.MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "file_too_large",
                "message": "The file exceeds the 5 MiB import limit.",
            },
        )
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.EDIT)
    receipt, created = await import_file(
        db,
        context,
        user_id,
        declared,
        fmt=import_format,
        filename=file.filename or "",
        data=data,
    )
    await db.commit()
    response.status_code = 201 if created else 200
    return receipt


@router.get(
    "/projects/{project_id}/imports", response_model=list[ImportReceiptResponse]
)
async def list_imports_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ImportReceiptResponse]:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.VIEW
    )
    return await list_receipts(db, collection_id=cast(UUID, context.collection.id))


@router.get(
    "/projects/{project_id}/imports/{receipt_id}",
    response_model=ImportReceiptDetail,
)
async def get_import_route(
    project_id: UUID,
    receipt_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ImportReceiptDetail:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.VIEW
    )
    return await get_receipt(
        db, collection_id=cast(UUID, context.collection.id), receipt_id=receipt_id
    )


@router.post(
    "/projects/{project_id}/citation-chases",
    response_model=ImportReceiptResponse,
    status_code=201,
    responses=_CREATED_OR_REPLAYED,
)
async def chase_citations_route(
    project_id: UUID,
    body: CitationChaseRequest,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ImportReceiptResponse:
    """OpenAlex citation chase; the service checks EDIT before and after I/O."""
    receipt, created = await chase_citations(
        db, project_id=project_id, user_id=cast(UUID, current_user.id), data=body
    )
    await db.commit()
    response.status_code = 201 if created else 200
    return receipt


@router.get(
    "/projects/{project_id}/corpus/export",
    response_class=Response,
    responses={
        200: {
            "content": {"application/json": {}, "application/zip": {}},
            "description": "Versioned corpus package download",
        }
    },
)
async def export_corpus_route(
    project_id: UUID,
    export_format: Literal["json", "zip"] = Query("json", alias="format"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.VIEW
    )
    # Serializing a large package must not block the event loop.
    content, media_type, filename = await asyncio.to_thread(
        render, await build_package(db, context), export_format
    )
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/projects/{project_id}/corpus/coverage", response_model=CoverageResponse)
async def corpus_coverage_route(
    project_id: UUID,
    body: CoverageRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CoverageResponse:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.VIEW
    )
    return await coverage(db, context, body.known)
