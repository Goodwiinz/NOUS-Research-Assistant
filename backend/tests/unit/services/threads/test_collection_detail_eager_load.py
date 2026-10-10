"""Collection detail responses must render attached documents.

The detail routes render ``cd.document.id/title/document_type`` via
``_collection_to_detail_response``. Loading only ``Collection.documents`` left
the nested ``document`` to a lazy load inside async code, which raised
``MissingGreenlet`` (HTTP 500) as soon as a collection had a document.
``add_documents_to_collection`` also re-fetched an identity-mapped collection
whose ``documents`` was already loaded, so the response omitted the new rows.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.api.threads.workspace_routes.presenters import _collection_to_detail_response
from src.models.collection import Collection, CollectionDocument
from src.models.document import Document, DocumentType
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.services.threads import collection_service, workspace_access

pytestmark = pytest.mark.unit


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        for model in (
            User,
            Workspace,
            WorkspaceMember,
            Collection,
            Document,
            CollectionDocument,
        ):
            await conn.run_sync(model.__table__.create)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(
    session_factory: async_sessionmaker[AsyncSession], *, attach: bool
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Owned workspace + empty collection + one same-org document.

    Returns ``(user_id, collection_id, document_id)``; ``attach`` also links
    the document to the collection.
    """
    user_id, organization_id = uuid.uuid4(), uuid.uuid4()
    collection_id, document_id = uuid.uuid4(), uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, 'owner@example.test', 'unused', 'Test', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
            ),
            {"id": str(user_id), "org": str(organization_id)},
        )
        workspace = Workspace(
            id=uuid.uuid4(),
            name="ws",
            owner_id=user_id,
            organization_id=organization_id,
        )
        document = Document(
            id=document_id,
            title="Attached paper",
            filename="f.pdf",
            file_path="x/f.pdf",
            file_size_bytes=1,
            mime_type="application/pdf",
            document_type=DocumentType.PDF,
            organization_id=organization_id,
            uploaded_by_user_id=user_id,
        )
        db.add_all([workspace, document])
        await db.commit()
        db.add(Collection(id=collection_id, workspace_id=workspace.id, name="c"))
        await db.commit()
        if attach:
            db.add(
                CollectionDocument(collection_id=collection_id, document_id=document_id)
            )
            await db.commit()
        return user_id, collection_id, document_id


@pytest.mark.asyncio
async def test_get_collection_detail_response_with_attached_document(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, collection_id, _ = await _seed(session_factory, attach=True)

    # Fresh session: nothing pre-loaded in the identity map, so the nested
    # ``document`` is only available if get_collection eager-loads it.
    async with session_factory() as db:
        collection = await workspace_access.get_collection(db, collection_id, user_id)
        assert collection is not None
        response = _collection_to_detail_response(collection)

    assert [(d["title"], d["document_type"]) for d in response.documents] == [
        ("Attached paper", DocumentType.PDF.value)
    ]


@pytest.mark.asyncio
async def test_add_documents_response_includes_newly_added_document(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, collection_id, document_id = await _seed(session_factory, attach=False)

    async with session_factory() as db:
        collection = await collection_service.add_documents_to_collection(
            db, collection_id, [document_id], user_id
        )
        assert collection is not None
        response = _collection_to_detail_response(collection)

    assert [d["title"] for d in response.documents] == ["Attached paper"]
    assert response.document_count == 1
