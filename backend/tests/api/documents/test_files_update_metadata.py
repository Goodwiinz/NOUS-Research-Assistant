"""PUT /files/{file_id} keeps its HTTP behaviour after the write left the router.

Characterization tests for Plan 07 Slice 3, review amendment 5: they pass
against the old inline-commit handler and must keep passing once it delegates
to file_metadata_service. Status codes, details and the response body are the
public contract, and a foreign or deleted document stays indistinguishable
from a missing one.

The files router is mounted alone over a file SQLite database, with only the
session and the authenticated user overridden (``get_current_organization``
runs for real). Users are seeded with raw SQL: first_name/last_name are
encrypted columns and these tests do not initialize field encryption.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import selectinload

from src.api.documents import files as files_mod
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.document import Document, DocumentType
from src.models.organization import Organization
from src.models.user import User

pytestmark = pytest.mark.unit

ORG, FOREIGN_ORG = uuid4(), uuid4()
OWNER, COLLEAGUE, FOREIGN_USER = uuid4(), uuid4(), uuid4()
DOC, DELETED_DOC, FOREIGN_DOC = uuid4(), uuid4(), uuid4()
ORIGINAL_TITLE = "Original title"
FORBIDDEN = "Can only update your own files or require admin role"

_INSERT_USER = text(
    "INSERT INTO users (id, organization_id, email, password_hash, first_name,"
    " last_name, role, is_active, login_count, created_at, updated_at,"
    " is_deleted) VALUES (:id, :org, :email, 'unused', 'Test', 'User', 'USER',"
    " 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
)


@dataclass
class Api:
    app: FastAPI
    client: AsyncClient
    db: AsyncSession
    engine: AsyncEngine

    async def act_as(self, user_id: UUID) -> None:
        user = (
            (
                await self.db.execute(
                    select(User)
                    .options(selectinload(User.organization))
                    .where(User.id == user_id)
                )
            )
            .scalars()
            .one()
        )
        self.app.dependency_overrides[get_current_user] = lambda: user

    async def committed(self, document_id: UUID = DOC) -> Document:
        factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with factory() as observer:
            document = await observer.get(Document, document_id)
            assert document is not None
            return document


def _document(
    document_id: UUID, organization_id: UUID, uploader: UUID, *, is_deleted: bool
) -> Document:
    return Document(
        id=document_id,
        organization_id=organization_id,
        uploaded_by_user_id=uploader,
        title=ORIGINAL_TITLE,
        filename="paper.pdf",
        file_path="/unused/paper.pdf",
        file_size_bytes=100,
        mime_type="application/pdf",
        document_type=DocumentType.PDF,
        tags=["seed"],
        is_deleted=is_deleted,
    )


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'files.db'}")
    tables: list[Any] = [Organization, User, Document]
    async with engine.begin() as conn:
        for model in tables:
            await conn.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as seed:
        for org_id in (ORG, FOREIGN_ORG):
            seed.add(
                Organization(id=org_id, name=str(org_id), storage_limit_bytes=10**6)
            )
        await seed.flush()
        for user_id, org_id in (
            (OWNER, ORG),
            (COLLEAGUE, ORG),
            (FOREIGN_USER, FOREIGN_ORG),
        ):
            await seed.execute(
                _INSERT_USER,
                {"id": str(user_id), "org": str(org_id), "email": f"{user_id}@e.test"},
            )
        seed.add_all(
            [
                _document(DOC, ORG, OWNER, is_deleted=False),
                _document(DELETED_DOC, ORG, OWNER, is_deleted=True),
                _document(FOREIGN_DOC, FOREIGN_ORG, FOREIGN_USER, is_deleted=False),
            ]
        )
        await seed.commit()

    app = FastAPI()
    app.include_router(files_mod.router)
    async with factory() as db:
        app.dependency_overrides[get_db] = lambda: db
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            api = Api(app=app, client=client, db=db, engine=engine)
            await api.act_as(OWNER)
            yield api
    await engine.dispose()


async def test_owner_put_updates_title_tags_and_visibility(api: Api) -> None:
    response = await api.client.put(
        f"/files/{DOC}",
        params={"title": "Renamed", "is_public": "true"},
        json=["graphs", "survey"],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "File metadata updated successfully"
    assert body["file"]["id"] == str(DOC)
    assert (body["file"]["title"], body["file"]["tags"]) == (
        "Renamed",
        ["graphs", "survey"],
    )
    assert body["file"]["is_public"] is True
    assert "file_path" not in body["file"]
    stored = await api.committed()
    assert (stored.title, stored.tags, stored.is_public) == (
        "Renamed",
        ["graphs", "survey"],
        True,
    )


async def test_put_title_only_leaves_other_fields(api: Api) -> None:
    response = await api.client.put(f"/files/{DOC}", params={"title": "Only"})

    assert response.status_code == 200
    stored = await api.committed()
    assert (stored.title, stored.tags, stored.is_public) == ("Only", ["seed"], False)


@pytest.mark.parametrize(
    "document_id",
    [uuid4(), DELETED_DOC, FOREIGN_DOC],
    ids=["missing", "soft-deleted", "foreign-org"],
)
async def test_put_on_missing_deleted_or_foreign_document_is_404(
    api: Api, document_id: UUID
) -> None:
    response = await api.client.put(
        f"/files/{document_id}", params={"title": "Renamed"}
    )

    assert (response.status_code, response.json()) == (
        404,
        {"detail": "File not found"},
    )
    for existing in (DELETED_DOC, FOREIGN_DOC):
        assert (await api.committed(existing)).title == ORIGINAL_TITLE


async def test_put_by_a_colleague_without_admin_role_is_403(api: Api) -> None:
    await api.act_as(COLLEAGUE)

    response = await api.client.put(f"/files/{DOC}", params={"title": "Renamed"})

    assert (response.status_code, response.json()) == (403, {"detail": FORBIDDEN})
    assert (await api.committed()).title == ORIGINAL_TITLE


async def test_put_commit_failure_is_a_safe_400_and_rolls_back(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_flush = api.db.flush

    async def failing_commit() -> None:
        await real_flush()
        raise RuntimeError("driver detail: password=hunter2")

    monkeypatch.setattr(api.db, "commit", failing_commit)

    response = await api.client.put(f"/files/{DOC}", params={"title": "Lost"})

    assert (response.status_code, response.json()) == (
        400,
        {"detail": "Failed to update file"},
    )
    assert "hunter2" not in response.text
    assert not api.db.in_transaction()
    assert (await api.committed()).title == ORIGINAL_TITLE
