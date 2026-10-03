"""
Collection endpoints, nested and standalone (Task 4.2 split of the former
monolithic ``backend/src/api/threads/workspaces.py``; Task 4.3 moved the
persistence logic into ``src/services/threads/collection_service.py``; PR 3
Task 3.2 moved the single request commit UP to these handlers — the service
flushes, each mutating handler ends with one ``await db.commit()`` (the
middleware-owned session rolls back on the error path). This module is
transport + transaction-boundary only.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.chat import (
    CollectionCreate,
    CollectionDetailResponse,
    CollectionDocumentAdd,
    CollectionDocumentRemove,
    CollectionListResponse,
    CollectionResponse,
    CollectionUpdate,
)
from src.services.threads import collection_service, workspace_access

from .dependencies import _get_collection_or_404
from .presenters import _collection_to_detail_response, _collection_to_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/workspaces", tags=["workspaces"])

# Standalone router for flat API paths (used by frontend)
standalone_router = APIRouter(prefix="/api/v2", tags=["workspaces-flat"])

# ============================================================================
# Collection Endpoints
# ============================================================================


@router.post(
    "/{workspace_id}/collections",
    response_model=CollectionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_collection(
    workspace_id: UUID,
    request: CollectionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionResponse:
    """Create a new collection in a workspace"""
    request.workspace_id = workspace_id
    try:
        collection = await collection_service.create_collection(
            db, request, current_user.id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not collection:
        raise HTTPException(status_code=404, detail="Workspace not found")

    await db.commit()
    return _collection_to_response(collection)


@router.get("/{workspace_id}/collections", response_model=CollectionListResponse)
async def list_collections(
    workspace_id: UUID,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionListResponse:
    """List collections in a workspace"""
    offset = (page - 1) * limit
    result = await collection_service.list_collections(
        db, workspace_id, current_user.id, limit=limit, offset=offset
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    collections, total = result

    return CollectionListResponse(
        collections=[_collection_to_response(c) for c in collections],
        total=total,
        page=page,
        limit=limit,
        has_more=(offset + len(collections)) < total,
    )


@router.get(
    "/{workspace_id}/collections/{collection_id}",
    response_model=CollectionDetailResponse,
)
async def get_collection(
    workspace_id: UUID,
    collection_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionDetailResponse:
    """Get collection details with documents"""
    collection = await _get_collection_or_404(
        db, workspace_id, collection_id, current_user
    )

    return _collection_to_detail_response(collection)


@router.patch(
    "/{workspace_id}/collections/{collection_id}", response_model=CollectionResponse
)
async def update_collection(
    workspace_id: UUID,
    collection_id: UUID,
    request: CollectionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionResponse:
    """Update collection details"""
    try:
        collection = await collection_service.update_collection(
            db, collection_id, request, current_user.id, workspace_id=workspace_id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    await db.commit()
    return _collection_to_response(collection)


@router.delete(
    "/{workspace_id}/collections/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_collection(
    workspace_id: UUID,
    collection_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Soft-delete a collection"""
    try:
        deleted = await collection_service.delete_collection(
            db, collection_id, current_user.id, workspace_id=workspace_id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not deleted:
        raise HTTPException(status_code=404, detail="Collection not found")

    await db.commit()


@router.post(
    "/{workspace_id}/collections/{collection_id}/documents",
    response_model=CollectionDetailResponse,
)
async def add_documents_to_collection(
    workspace_id: UUID,
    collection_id: UUID,
    request: CollectionDocumentAdd,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionDetailResponse:
    """Add documents to a collection"""
    try:
        collection = await collection_service.add_documents_to_collection(
            db,
            collection_id,
            request.document_ids,
            current_user.id,
            workspace_id=workspace_id,
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    await db.commit()
    return _collection_to_detail_response(collection)


@router.delete(
    "/{workspace_id}/collections/{collection_id}/documents",
    response_model=CollectionDetailResponse,
)
async def remove_documents_from_collection(
    workspace_id: UUID,
    collection_id: UUID,
    request: CollectionDocumentRemove,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionDetailResponse:
    """Remove documents from a collection"""
    try:
        collection = await collection_service.remove_documents_from_collection(
            db,
            collection_id,
            request.document_ids,
            current_user.id,
            workspace_id=workspace_id,
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    await db.commit()
    return _collection_to_detail_response(collection)


# ============================================================================
# Standalone (Flat) Collection Routes
# These routes allow direct access without full path hierarchy
# ============================================================================


@standalone_router.post(
    "/collections",
    response_model=CollectionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_collection_standalone(
    request: CollectionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionResponse:
    """Create a new collection (standalone route - uses workspace_id from request body)"""
    try:
        collection = await collection_service.create_collection(
            db, request, current_user.id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not collection:
        raise HTTPException(status_code=404, detail="Workspace not found")

    await db.commit()
    return _collection_to_response(collection)


@standalone_router.get(
    "/collections/{collection_id}", response_model=CollectionDetailResponse
)
async def get_collection_standalone(
    collection_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionDetailResponse:
    """Get collection details with documents (standalone route)"""
    collection = await workspace_access.get_collection(
        db, collection_id, current_user.id
    )
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    return _collection_to_detail_response(collection)


@standalone_router.patch(
    "/collections/{collection_id}", response_model=CollectionResponse
)
async def update_collection_standalone(
    collection_id: UUID,
    request: CollectionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionResponse:
    """Update collection details (standalone route)"""
    try:
        collection = await collection_service.update_collection(
            db, collection_id, request, current_user.id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    await db.commit()
    return _collection_to_response(collection)


@standalone_router.delete(
    "/collections/{collection_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_collection_standalone(
    collection_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Soft-delete a collection (standalone route)"""
    try:
        deleted = await collection_service.delete_collection(
            db, collection_id, current_user.id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not deleted:
        raise HTTPException(status_code=404, detail="Collection not found")

    await db.commit()


@standalone_router.post(
    "/collections/{collection_id}/documents", response_model=CollectionDetailResponse
)
async def add_documents_to_collection_standalone(
    collection_id: UUID,
    request: CollectionDocumentAdd,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionDetailResponse:
    """Add documents to a collection (standalone route)"""
    try:
        collection = await collection_service.add_documents_to_collection(
            db, collection_id, request.document_ids, current_user.id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    await db.commit()
    return _collection_to_detail_response(collection)


@standalone_router.delete(
    "/collections/{collection_id}/documents", response_model=CollectionDetailResponse
)
async def remove_documents_from_collection_standalone(
    collection_id: UUID,
    request: CollectionDocumentRemove,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionDetailResponse:
    """Remove documents from a collection (standalone route)"""
    try:
        collection = await collection_service.remove_documents_from_collection(
            db, collection_id, request.document_ids, current_user.id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    await db.commit()
    return _collection_to_detail_response(collection)
