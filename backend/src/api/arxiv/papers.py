"""Authenticated paper metadata, sync, recent import and source PDF routes."""

import logging
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import (
    get_current_organization,
    get_current_user,
    require_admin,
)
from src.models.organization import Organization
from src.models.user import User
from src.services.arxiv import papers as service
from src.services.expensive_work_admission import (
    metered_expensive_work,
    require_expensive_work_admission,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/arxiv/papers", tags=["arxiv-papers"])


class PaperMetadata(BaseModel):
    id: str
    title: str
    authors: list[str]
    abstract: str
    categories: list[str] = Field(default_factory=list)
    published: datetime | None = None
    updated: datetime | None = None
    comment: str | None = None
    pdf_url: str | None = None
    doi: str | None = None
    withdrawn: bool | None = Field(
        None, description="Null until this revision's withdrawal status is checked"
    )
    latest_id: str | None = None
    latest_withdrawn: bool | None = None
    source: Literal["arxiv_atom", "arxiv_abstract_page", "arxiv_atom+abstract_page"]
    checked_at: datetime


class PaperRequest(BaseModel):
    paper_id: str = Field(max_length=64)

    @field_validator("paper_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return service.normalize_paper_id(value)


Category = Annotated[
    str,
    StringConstraints(pattern="^" + service.CATEGORY_PATTERN + "$", max_length=32),
]


class RecentImportRequest(BaseModel):
    categories: list[Category] = Field(min_length=1, max_length=10)
    max_results: int = Field(20, ge=1, le=50)
    days: int = Field(1, ge=1, le=7)


class RecentImportResponse(BaseModel):
    task_id: str
    status: Literal["queued"] = "queued"


def paper_admission(user: User = Depends(get_current_user)) -> service.Admission:
    admitted = False

    async def admit() -> None:
        nonlocal admitted
        if not admitted:
            await require_expensive_work_admission(user)
            admitted = True

    return admit


async def _metadata(
    db: AsyncSession,
    organization: Organization,
    paper_id: str,
    *,
    sync: bool = False,
    admit: service.Admission | None = None,
) -> dict[str, Any]:
    try:
        if sync:
            return await service.sync_metadata(
                db, cast(UUID, organization.id), paper_id
            )
        return await service.resolve_metadata(
            db, cast(UUID, organization.id), paper_id, admit=admit
        )
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(422, "Invalid arXiv identifier") from error
    except service.PaperNotFound as error:
        raise HTTPException(404, "arXiv paper not found") from error
    except Exception as error:
        logger.warning("arXiv metadata retrieval failed", exc_info=True)
        raise HTTPException(502, "arXiv metadata is temporarily unavailable") from error


@router.get("/metadata", response_model=PaperMetadata)
async def metadata(
    paper_id: str = Query(max_length=64),
    db: AsyncSession = Depends(get_db),
    organization: Organization = Depends(get_current_organization),
    admit: service.Admission = Depends(paper_admission),
) -> dict[str, Any]:
    return await _metadata(db, organization, paper_id, admit=admit)


@router.post("/sync", response_model=PaperMetadata)
@metered_expensive_work
async def sync(
    request: PaperRequest,
    db: AsyncSession = Depends(get_db),
    organization: Organization = Depends(get_current_organization),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    return await _metadata(db, organization, request.paper_id, sync=True)


@router.get("/pdf", response_class=RedirectResponse, status_code=303)
async def pdf(
    paper_id: str = Query(max_length=64),
    db: AsyncSession = Depends(get_db),
    organization: Organization = Depends(get_current_organization),
    user: User = Depends(get_current_user),
    admit: service.Admission = Depends(paper_admission),
) -> RedirectResponse:
    try:
        document_id = await service.store_pdf(
            db, cast(UUID, organization.id), cast(UUID, user.id), paper_id, admit=admit
        )
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(422, "Invalid arXiv identifier") from error
    except service.PaperNotFound as error:
        raise HTTPException(404, "arXiv paper not found") from error
    except Exception as error:
        logger.warning("arXiv PDF storage failed", exc_info=True)
        raise HTTPException(502, "arXiv PDF is temporarily unavailable") from error
    return RedirectResponse(f"/api/v1/files/{document_id}/download", status_code=303)


@router.post("/recent", response_model=RecentImportResponse, status_code=202)
@metered_expensive_work
async def recent(
    request: RecentImportRequest,
    organization: Organization = Depends(get_current_organization),
    admin: User = Depends(require_admin),
) -> RecentImportResponse:
    from src.services.arxiv.recent import enqueue_recent

    try:
        task_id = await enqueue_recent(
            cast(UUID, organization.id),
            cast(UUID, admin.id),
            request.categories,
            request.max_results,
            request.days,
        )
    except Exception as error:
        logger.warning("arXiv recent import enqueue failed", exc_info=True)
        raise HTTPException(
            503, "Paper import queue is temporarily unavailable"
        ) from error
    return RecentImportResponse(task_id=task_id)
