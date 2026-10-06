"""Superseding review version API (GOO-320): transport only; the service
commits.

A supervisor freezes the root version, then accepts one GOO-319 delta per
successor; unchanged decisions are carried by reference and changed
evidence becomes targeted GOO-301/302 work. Writes are SUPERVISE, reads
VIEW. The export is a sealed, read-only JSON attachment.
"""

import json
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.research_engine import (
    ReviewReleaseLinkCreate,
    ReviewReleaseLinkResponse,
    ReviewVersionCreate,
    ReviewVersionExport,
    ReviewVersionListResponse,
    ReviewVersionResponse,
    UpdateAccountingResponse,
)
from src.services.research_engine import review_update_service as service
from src.services.research_engine.project_access import ResearchAction, resolve_project

REVIEW_VERSIONS = "/projects/{project_id}/review-versions"
router = APIRouter(prefix="/research-engine", tags=["research-engine-review-versions"])
SUPERVISE = ResearchAction.SUPERVISE


def _uid(user: User) -> UUID:
    return cast(UUID, user.id)


@router.get(REVIEW_VERSIONS, response_model=ReviewVersionListResponse)
async def list_versions(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReviewVersionListResponse:
    """The version chain with derived work status, stale counts and the
    deltas a successor could accept."""
    context = await resolve_project(db, project_id, _uid(current_user))
    return await service.list_versions(db, context)


@router.post(
    REVIEW_VERSIONS,
    status_code=201,
    response_model=ReviewVersionResponse,
    responses={
        200: {"description": "Replayed version"},
        404: {"description": "Unknown parent version or execution"},
        409: {
            "description": (
                "Stale tip, delta changed or out of order, unresolved parent "
                "work, or a root already exists"
            )
        },
        422: {"description": "Ineligible reviewer or invalid uncertainty carry"},
    },
)
async def create_version(
    project_id: UUID,
    request: ReviewVersionCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReviewVersionResponse:
    """Freeze the root (no parent) or a successor accepting one delta."""
    context = await resolve_project(db, project_id, _uid(current_user), SUPERVISE)
    result, replayed = await service.create_version(
        db, context, _uid(current_user), request
    )
    if replayed:
        response.status_code = 200
    return result


@router.post(
    REVIEW_VERSIONS + "/{version_id:uuid}/work",
    response_model=ReviewVersionResponse,
)
async def ensure_work(
    project_id: UUID,
    version_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReviewVersionResponse:
    """Retry the targeted queues and assignments with the same keys."""
    context = await resolve_project(db, project_id, _uid(current_user), SUPERVISE)
    return await service.ensure_work(db, context, _uid(current_user), version_id)


@router.post(
    REVIEW_VERSIONS + "/{version_id:uuid}/release",
    status_code=201,
    response_model=ReviewReleaseLinkResponse,
    responses={
        200: {"description": "Replayed link"},
        409: {"description": "Not verified, too old, or wrong superseded release"},
    },
)
async def link_release(
    project_id: UUID,
    version_id: UUID,
    request: ReviewReleaseLinkCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReviewReleaseLinkResponse:
    """Link a verified GOO-315 release superseding the parent's release."""
    context = await resolve_project(db, project_id, _uid(current_user), SUPERVISE)
    result, replayed = await service.link_release(
        db, context, _uid(current_user), version_id, request
    )
    if replayed:
        response.status_code = 200
    return result


@router.get(
    REVIEW_VERSIONS + "/{version_id:uuid}/accounting",
    response_model=UpdateAccountingResponse,
    responses={409: {"description": "Update accounting does not reconcile"}},
)
async def get_accounting(
    project_id: UUID,
    version_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UpdateAccountingResponse:
    """Both PRISMA bodies plus the update boxes."""
    context = await resolve_project(db, project_id, _uid(current_user))
    return await service.accounting(db, context, version_id)


@router.get(
    REVIEW_VERSIONS + "/{version_id:uuid}/export",
    response_class=Response,
    responses={
        200: {
            "model": ReviewVersionExport,
            "description": "Sealed nous.academic.review-version.v1 export",
        },
        404: {"description": "Unknown or foreign version"},
    },
)
async def export_version(
    project_id: UUID,
    version_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Corpus, decisions with actors, work, accounting and release (read-only)."""
    context = await resolve_project(db, project_id, _uid(current_user))
    package = await service.export_version(db, context, version_id)
    return Response(
        content=json.dumps(package, sort_keys=True, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="review-version-{version_id}.json"'
            )
        },
    )
