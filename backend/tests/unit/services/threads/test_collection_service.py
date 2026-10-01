"""Characterize collection_service.py (Task 4.3 consolidation).

Unlike the other resource services in this task, ``ChatService``'s five
collection methods have zero production callers and two carry real bugs (see
the module docstring) — these tests prove the bugs are fixed unconditionally
(no compatibility flag): per-document organization ownership on attach, and
soft- (not hard-) delete on remove.
"""

from __future__ import annotations

from typing import Awaitable, Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.document import Document
from src.models.organization import Organization
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.schemas.chat import CollectionCreate, CollectionUpdate
from src.services.threads import collection_service

pytestmark = pytest.mark.integration


async def _make_workspace(db_session: AsyncSession, owner: User) -> Workspace:
    ws = Workspace(name="ws", owner_id=owner.id, organization_id=owner.organization_id)
    db_session.add(ws)
    await db_session.commit()
    await db_session.refresh(ws)
    db_session.info["_created"]["workspaces"].append(ws.id)
    return ws


async def _make_document(
    db_session: AsyncSession, organization_id: UUID, user: User, *, title: str = "doc"
) -> Document:
    from src.models.document import DocumentType

    doc = Document(
        title=title,
        filename="f.pdf",
        file_path="x/f.pdf",
        file_size_bytes=1,
        mime_type="application/pdf",
        document_type=DocumentType.PDF,
        organization_id=organization_id,
        uploaded_by_user_id=user.id,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)
    return doc


async def test_create_collection_only_attaches_owned_documents(
    db_session: AsyncSession,
    user_factory: Callable[..., Awaitable[User]],
    organization_factory: Callable[..., Awaitable[Organization]],
) -> None:
    owner = await user_factory()
    other_org = await organization_factory()
    other_user = await user_factory(organization=other_org)
    ws = await _make_workspace(db_session, owner)

    owned_doc = await _make_document(db_session, owner.organization_id, owner)
    foreign_doc = await _make_document(db_session, other_org.id, other_user)

    collection = await collection_service.create_collection(
        db_session,
        CollectionCreate(
            workspace_id=ws.id, name="c", document_ids=[owned_doc.id, foreign_doc.id]
        ),
        owner.id,
    )

    assert collection is not None
    attached_ids = {cd.document_id for cd in collection.documents}
    assert attached_ids == {owned_doc.id}


async def test_add_documents_to_collection_rejects_foreign_org_document(
    db_session: AsyncSession,
    user_factory: Callable[..., Awaitable[User]],
    organization_factory: Callable[..., Awaitable[Organization]],
) -> None:
    owner = await user_factory()
    other_org = await organization_factory()
    other_user = await user_factory(organization=other_org)
    ws = await _make_workspace(db_session, owner)
    collection = await collection_service.create_collection(
        db_session, CollectionCreate(workspace_id=ws.id, name="c"), owner.id
    )
    assert collection is not None
    foreign_doc = await _make_document(db_session, other_org.id, other_user)

    updated = await collection_service.add_documents_to_collection(
        db_session, collection.id, [foreign_doc.id], owner.id
    )
    assert updated is not None
    assert updated.documents == []


async def test_remove_documents_soft_deletes_not_hard_deletes(
    db_session: AsyncSession, user_factory: Callable[..., Awaitable[User]]
) -> None:
    owner = await user_factory()
    ws = await _make_workspace(db_session, owner)
    doc = await _make_document(db_session, owner.organization_id, owner)
    collection = await collection_service.create_collection(
        db_session,
        CollectionCreate(workspace_id=ws.id, name="c", document_ids=[doc.id]),
        owner.id,
    )
    assert collection is not None
    collection_doc_id = collection.documents[0].id

    await collection_service.remove_documents_from_collection(
        db_session, collection.id, [doc.id], owner.id
    )

    from sqlalchemy import select

    from src.models.collection import CollectionDocument

    row = (
        await db_session.execute(
            select(CollectionDocument).where(CollectionDocument.id == collection_doc_id)
        )
    ).scalar_one_or_none()
    assert row is not None  # still exists (soft-deleted), not hard-deleted
    assert row.is_deleted is True
    assert row.deleted_at is not None


async def test_update_collection_rejects_mismatched_workspace_id(
    db_session: AsyncSession, user_factory: Callable[..., Awaitable[User]]
) -> None:
    owner = await user_factory()
    ws_a = await _make_workspace(db_session, owner)
    ws_b = await _make_workspace(db_session, owner)
    collection = await collection_service.create_collection(
        db_session, CollectionCreate(workspace_id=ws_b.id, name="c"), owner.id
    )
    assert collection is not None

    assert (
        await collection_service.update_collection(
            db_session,
            collection.id,
            CollectionUpdate(name="new"),
            owner.id,
            workspace_id=ws_a.id,
        )
    ) is None

    updated = await collection_service.update_collection(
        db_session,
        collection.id,
        CollectionUpdate(name="new"),
        owner.id,
        workspace_id=ws_b.id,
    )
    assert updated is not None
    assert updated.name == "new"


async def test_delete_collection_not_found_vs_forbidden(
    db_session: AsyncSession, user_factory: Callable[..., Awaitable[User]]
) -> None:
    owner = await user_factory()
    viewer = await user_factory()
    ws = await _make_workspace(db_session, owner)
    db_session.add(
        WorkspaceMember(
            workspace_id=ws.id, user_id=viewer.id, role=WorkspaceRole.VIEWER
        )
    )
    await db_session.commit()
    collection = await collection_service.create_collection(
        db_session, CollectionCreate(workspace_id=ws.id, name="c"), owner.id
    )
    assert collection is not None

    assert (
        await collection_service.delete_collection(db_session, uuid4(), owner.id)
    ) is None

    with pytest.raises(PermissionError):
        await collection_service.delete_collection(db_session, collection.id, viewer.id)


async def _link_row(db_session: AsyncSession, collection_id: UUID, doc_id: UUID):
    from sqlalchemy import select

    from src.models.collection import CollectionDocument

    return (
        await db_session.execute(
            select(CollectionDocument)
            .where(
                CollectionDocument.collection_id == collection_id,
                CollectionDocument.document_id == doc_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_re_adding_removed_document_restores_link(
    db_session: AsyncSession, user_factory: Callable[..., Awaitable[User]]
) -> None:
    """R6-M7 / R8-B3: remove then re-add revives the soft-deleted junction row
    on both the REST path and the agent link helper, instead of raising on
    ``uq_collection_documents`` or reporting ``already_linked``."""
    from src.services.agent.tool_helpers import _link_documents_to_project

    owner = await user_factory()
    ws = await _make_workspace(db_session, owner)
    doc = await _make_document(db_session, owner.organization_id, owner)
    collection = await collection_service.create_collection(
        db_session,
        CollectionCreate(workspace_id=ws.id, name="c", document_ids=[doc.id]),
        owner.id,
    )
    assert collection is not None
    original_row_id = collection.documents[0].id

    # REST: remove → re-add.
    await collection_service.remove_documents_from_collection(
        db_session, collection.id, [doc.id], owner.id
    )
    await collection_service.add_documents_to_collection(
        db_session, collection.id, [doc.id], owner.id
    )
    row = await _link_row(db_session, collection.id, doc.id)
    assert row.id == original_row_id
    assert row.is_deleted is False
    assert row.deleted_at is None

    # Agent helper: remove → link.
    await collection_service.remove_documents_from_collection(
        db_session, collection.id, [doc.id], owner.id
    )
    result = await _link_documents_to_project(
        db_session, collection, [str(doc.id), str(doc.id)]
    )
    assert result == {"linked": 1, "already_linked": 0, "restored": [str(doc.id)]}
    row = await _link_row(db_session, collection.id, doc.id)
    assert row.id == original_row_id
    assert row.is_deleted is False
    assert row.deleted_at is None

    # An active link stays already_linked.
    again = await _link_documents_to_project(db_session, collection, [str(doc.id)])
    assert again == {"linked": 0, "already_linked": 1, "restored": []}
