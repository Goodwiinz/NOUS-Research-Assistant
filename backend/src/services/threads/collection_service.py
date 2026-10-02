"""
Collection persistence (Task 4.3 consolidation).

Canonical owner for collection CRUD + document membership — the router-inline
copy in ``backend/src/api/threads/workspace_routes/collections.py`` (nested +
standalone) and ``ChatService``'s five collection methods both implemented
overlapping pieces of this independently.

Unlike the other resources split in this task, ``ChatService``'s collection
methods have **zero production callers** (dead code — the router never
delegated to them) and two of them carry real bugs, not just style
differences:

- ``add_documents_to_collection``: the router checks per-document
  organization ownership (``workspace_access.get_accessible_document_or_none``)
  before attaching; ``ChatService``'s version inserted whatever document id
  was given with no ownership check at all (an IDOR-shaped gap if it were
  ever wired up).
- ``remove_documents_from_collection``: the router soft-deletes
  ``CollectionDocument`` rows (consistent with every other delete in this
  schema); ``ChatService``'s version hard-deleted them.

Because nothing calls the old (buggy) ``ChatService`` behavior, there is no
"existing behavior" for a compatibility flag to protect — unlike the other
resource services in this task, these two functions consolidate onto the
router's safe behavior unconditionally. ``update_collection``/
``delete_collection`` have no ``ChatService`` equivalent at all (router-only
before this split); they move over as-is, no flag.
"""

from datetime import datetime
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.collection import Collection, CollectionDocument
from src.schemas.chat import CollectionCreate, CollectionUpdate
from src.services.threads import workspace_access


async def get_collection(
    db: AsyncSession,
    collection_id: UUID,
    user_id: UUID,
    *,
    workspace_id: Optional[UUID] = None,
) -> Optional[Collection]:
    """Fetch a collection the caller can access, or ``None``."""
    return await workspace_access.get_collection(
        db, collection_id, user_id, workspace_id=workspace_id
    )


async def create_collection(
    db: AsyncSession, data: CollectionCreate, user_id: UUID
) -> Optional[Collection]:
    """Create a collection (+ optional initial documents, org-ownership
    checked). ``None`` if the workspace isn't found/accessible; raises
    ``PermissionError`` if found but the caller lacks edit rights."""
    workspace = await workspace_access.get_workspace(
        db, data.workspace_id, user_id, load_conversations=False, load_collections=False
    )
    if not workspace:
        return None
    if not workspace.can_user_edit(str(user_id)):
        raise PermissionError("Insufficient permissions")

    collection = Collection(
        workspace_id=data.workspace_id,
        name=data.name,
        description=data.description,
        color=data.color,
        icon=data.icon,
    )
    db.add(collection)

    if data.document_ids:
        organization_id = getattr(workspace, "organization_id", None)
        for i, doc_id in enumerate(data.document_ids):
            doc = await workspace_access.get_accessible_document_or_none(
                db, doc_id, user_id, organization_id
            )
            if doc:
                db.add(
                    CollectionDocument(
                        collection=collection, document_id=doc_id, sort_order=i
                    )
                )

    await db.flush()
    # A plain db.refresh() expires + lazy-reloads relationships, which
    # MissingGreenlets under the async session the moment a caller (e.g. the
    # presenter's document_count) touches `.documents`. Re-fetch with it
    # eager-loaded instead — the same pattern get_collection already uses.
    created = await workspace_access.get_collection(db, collection.id, user_id)
    assert created is not None
    return created


async def list_collections(
    db: AsyncSession,
    workspace_id: UUID,
    user_id: UUID,
    *,
    limit: int = 20,
    offset: int = 0,
) -> Optional[Tuple[List[Collection], int]]:
    """List collections in a workspace.

    Returns ``None`` if the workspace isn't found/accessible (distinct from
    a legitimately empty list).
    """
    workspace = await workspace_access.get_workspace(
        db, workspace_id, user_id, load_conversations=False, load_collections=False
    )
    if not workspace:
        return None

    base_conditions = [
        Collection.workspace_id == workspace_id,
        Collection.is_deleted == False,  # noqa: E712
    ]
    total = (
        await db.execute(select(func.count(Collection.id)).where(*base_conditions))
    ).scalar() or 0

    stmt = (
        select(Collection, func.count(CollectionDocument.id))
        .outerjoin(
            CollectionDocument,
            and_(
                CollectionDocument.collection_id == Collection.id,
                CollectionDocument.is_deleted == False,  # noqa: E712
            ),
        )
        .where(*base_conditions)
        .group_by(Collection.id)
        .order_by(Collection.name)
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    collections = []
    for collection, document_count in rows:
        setattr(collection, "_document_count", document_count)
        collections.append(collection)
    return collections, total


async def update_collection(
    db: AsyncSession,
    collection_id: UUID,
    data: CollectionUpdate,
    user_id: UUID,
    *,
    workspace_id: Optional[UUID] = None,
) -> Optional[Collection]:
    """Update a collection. ``None`` if not found/accessible; raises
    ``PermissionError`` if found but the caller lacks edit rights.

    ``workspace_id``, when given, scopes the lookup to that parent (the
    nested-route chain check); standalone callers pass ``None``.
    """
    collection = await workspace_access.get_collection(
        db, collection_id, user_id, workspace_id=workspace_id
    )
    if not collection:
        return None
    if not collection.workspace.can_user_edit(str(user_id)):
        raise PermissionError("Insufficient permissions")

    if data.name is not None:
        collection.name = data.name
    if data.description is not None:
        collection.description = data.description
    if data.color is not None:
        collection.color = data.color
    if data.icon is not None:
        collection.icon = data.icon

    collection.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(collection)
    return collection


async def delete_collection(
    db: AsyncSession,
    collection_id: UUID,
    user_id: UUID,
    *,
    workspace_id: Optional[UUID] = None,
) -> Optional[bool]:
    """Soft-delete a collection. ``None`` if not found/accessible; raises
    ``PermissionError`` if found but the caller lacks edit rights.

    ``workspace_id``, when given, scopes the lookup to that parent (the
    nested-route chain check); standalone callers pass ``None``.
    """
    collection = await workspace_access.get_collection(
        db, collection_id, user_id, workspace_id=workspace_id
    )
    if not collection:
        return None
    if not collection.workspace.can_user_edit(str(user_id)):
        raise PermissionError("Insufficient permissions")

    collection.is_deleted = True
    collection.deleted_at = datetime.utcnow()
    await db.flush()
    return True


async def add_documents_to_collection(
    db: AsyncSession,
    collection_id: UUID,
    document_ids: List[UUID],
    user_id: UUID,
    *,
    workspace_id: Optional[UUID] = None,
) -> Optional[Collection]:
    """Add documents to a collection. Only documents the caller's
    organization owns are attached (silently skipped otherwise — mirrors
    ``ChatService._filter_owned_document_ids``'s convention for message
    attachments). ``None`` if not found/accessible; raises
    ``PermissionError`` if found but the caller lacks edit rights.

    ``workspace_id``, when given, scopes the lookup to that parent (the
    nested-route chain check); standalone callers pass ``None``.
    """
    collection = await workspace_access.get_collection(
        db, collection_id, user_id, workspace_id=workspace_id
    )
    if not collection:
        return None
    if not collection.workspace.can_user_edit(str(user_id)):
        raise PermissionError("Insufficient permissions")

    max_order = (
        await db.execute(
            select(func.max(CollectionDocument.sort_order)).where(
                CollectionDocument.collection_id == collection_id
            )
        )
    ).scalar() or 0

    organization_id = getattr(collection.workspace, "organization_id", None)
    for i, doc_id in enumerate(document_ids):
        doc = await workspace_access.get_accessible_document_or_none(
            db, doc_id, user_id, organization_id
        )
        if not doc:
            continue

        existing = (
            (
                await db.execute(
                    select(CollectionDocument).where(
                        CollectionDocument.collection_id == collection_id,
                        CollectionDocument.document_id == doc_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if not existing:
            db.add(
                CollectionDocument(
                    collection_id=collection_id,
                    document_id=doc_id,
                    sort_order=max_order + i + 1,
                )
            )
        elif existing.is_deleted:
            # Revive the soft-deleted row; a second insert would violate
            # uq_collection_documents (R6-M7).
            existing.restore()
            existing.sort_order = max_order + i + 1

    await db.flush()
    # The rows above were added by FK only, and `collection.documents` is
    # already loaded (identity-mapped), so the re-fetch below would return it
    # stale. Expire just that attribute (AsyncSession.expire is sync) so the
    # eager load repopulates it. See create_collection for why not db.refresh().
    db.expire(collection, ["documents"])
    updated = await workspace_access.get_collection(db, collection_id, user_id)
    assert updated is not None
    return updated


async def remove_documents_from_collection(
    db: AsyncSession,
    collection_id: UUID,
    document_ids: List[UUID],
    user_id: UUID,
    *,
    workspace_id: Optional[UUID] = None,
) -> Optional[Collection]:
    """Remove (soft-delete) documents from a collection. ``None`` if not
    found/accessible; raises ``PermissionError`` if found but the caller
    lacks edit rights.

    ``workspace_id``, when given, scopes the lookup to that parent (the
    nested-route chain check); standalone callers pass ``None``.
    """
    collection = await workspace_access.get_collection(
        db, collection_id, user_id, workspace_id=workspace_id
    )
    if not collection:
        return None
    if not collection.workspace.can_user_edit(str(user_id)):
        raise PermissionError("Insufficient permissions")

    for doc_id in document_ids:
        collection_doc = (
            (
                await db.execute(
                    select(CollectionDocument).where(
                        CollectionDocument.collection_id == collection_id,
                        CollectionDocument.document_id == doc_id,
                        CollectionDocument.is_deleted == False,  # noqa: E712
                    )
                )
            )
            .scalars()
            .first()
        )
        if collection_doc:
            collection_doc.is_deleted = True
            collection_doc.deleted_at = datetime.utcnow()

    await db.flush()
    # See create_collection: re-fetch with `.documents` eager-loaded rather
    # than db.refresh(), which would expire the relationship and
    # MissingGreenlet on next access under the async session.
    updated = await workspace_access.get_collection(db, collection_id, user_id)
    assert updated is not None
    return updated
