"""Unit tests for the project-report endpoint authorization.

Guards the fix for the ``Collection.owner_id`` bug: the report endpoint now
scopes the project lookup to the caller's workspaces (ownership lives on
Workspace, not Collection) and returns a single 404 for both missing and
inaccessible projects (no existence disclosure). Mocked DB, no real Postgres.
"""

from datetime import datetime, timezone
from typing import Any, Sequence
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Collection, User, UserRole


@pytest.fixture
def mock_user() -> MagicMock:
    user = MagicMock(spec=User)
    user.id = uuid4()
    user.email = "owner@example.com"
    user.role = UserRole.USER
    user.is_active = True
    return user


@pytest.fixture
def mock_project() -> MagicMock:
    project = MagicMock(spec=Collection)
    project.id = uuid4()
    project.name = "Test Research Project"
    project.description = "A test project"
    project.workspace_id = uuid4()
    project.created_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
    return project


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock(spec=AsyncSession)
    db.execute = AsyncMock()
    return db


def _scalar_one_or_none(value: Any) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _scalars_all(values: Sequence[Any]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = values
    return result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_report_renders_for_owner(
    mock_user: MagicMock, mock_project: MagicMock, mock_db: AsyncMock
) -> None:
    """Owner gets a rendered HTML report (200)."""
    from src.api.research.project_report import render_project_report

    # 1) owner-scoped project lookup, 2) docs query, 3) notes query
    mock_db.execute.side_effect = [
        _scalar_one_or_none(mock_project),
        _scalars_all([]),
        _scalars_all([]),
    ]

    with patch("src.api.research.project_report._scan_recent_threads", return_value=[]):
        response = await render_project_report(
            mock_project.id, current_user=mock_user, db=mock_db
        )

    assert isinstance(response, HTMLResponse)
    assert response.status_code == 200
    assert b"Test Research Project" in response.body


@pytest.mark.unit
@pytest.mark.asyncio
async def test_report_404_when_not_owned_or_missing(
    mock_user: MagicMock, mock_db: AsyncMock
) -> None:
    """A project in another user's workspace (or absent) yields 404 — the
    scoped query returns nothing, so missing and forbidden are indistinguishable.
    """
    from src.api.research.project_report import render_project_report

    mock_db.execute.side_effect = [_scalar_one_or_none(None)]

    with pytest.raises(HTTPException) as exc_info:
        await render_project_report(uuid4(), current_user=mock_user, db=mock_db)

    assert exc_info.value.status_code == 404
    assert "not found or access denied" in exc_info.value.detail
    # Lookup must short-circuit: no docs/notes queries after a denied project.
    assert mock_db.execute.await_count == 1


# ---------------------------------------------------------------------------
# Soft-deleted / unlinked documents must not appear in the report.
#
# Deleting a document only stamps ``Document.is_deleted``; removing it from a
# project stamps ``CollectionDocument.is_deleted``. Neither cascades, so the
# documents query has to filter both — exactly like ``list_project_documents``
# in ``src/api/research/projects.py``. Real (throwaway) SQLite here, because the
# predicate lives in SQL, not Python; a mocked ``db.execute`` cannot observe it.
# ---------------------------------------------------------------------------


@pytest.fixture
async def sqlite_session_factory():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.models import (
        CollectionDocument,
        Document,
        Organization,
        ProjectNote,
        Workspace,
    )
    from src.models.base import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[
                    Organization.__table__,
                    Workspace.__table__,
                    Collection.__table__,
                    Document.__table__,
                    CollectionDocument.__table__,
                    ProjectNote.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _document(org_id: Any, title: str, *, deleted: bool = False) -> Any:
    from src.models import Document, DocumentType

    return Document(
        id=uuid4(),
        title=title,
        filename=f"{title}.pdf",
        file_path=f"/tmp/{title}.pdf",
        file_size_bytes=1,
        mime_type="application/pdf",
        document_type=DocumentType.TEXT,
        organization_id=org_id,
        is_deleted=deleted,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_report_excludes_deleted_and_unlinked_documents(
    mock_user: MagicMock, sqlite_session_factory: Any
) -> None:
    """A soft-deleted document, a document unlinked from the project, and a
    foreign-organization document must neither be listed nor counted; the live
    document still is.
    """
    from src.api.research.project_report import render_project_report
    from src.models import CollectionDocument, Organization, Workspace

    org_id = uuid4()
    foreign_org_id = uuid4()
    mock_user.organization_id = org_id

    async with sqlite_session_factory() as db:
        db.add_all(
            [
                Organization(id=org_id, name="Org", storage_limit_bytes=1),
                Organization(id=foreign_org_id, name="Other", storage_limit_bytes=1),
            ]
        )
        workspace = Workspace(id=uuid4(), name="WS", owner_id=mock_user.id)
        project = Collection(
            id=uuid4(), workspace_id=workspace.id, name="Test Research Project"
        )
        live = _document(org_id, "LIVE-DOC")
        soft_deleted = _document(org_id, "SOFT-DELETED-DOC", deleted=True)
        unlinked = _document(org_id, "UNLINKED-DOC")
        foreign = _document(foreign_org_id, "FOREIGN-ORG-DOC")
        db.add_all([workspace, project, live, soft_deleted, unlinked, foreign])
        await db.flush()
        db.add_all(
            [
                CollectionDocument(collection_id=project.id, document_id=live.id),
                CollectionDocument(
                    collection_id=project.id, document_id=soft_deleted.id
                ),
                CollectionDocument(
                    collection_id=project.id, document_id=unlinked.id, is_deleted=True
                ),
                CollectionDocument(collection_id=project.id, document_id=foreign.id),
            ]
        )
        await db.commit()

        with patch(
            "src.api.research.project_report._scan_recent_threads", return_value=[]
        ):
            response = await render_project_report(
                project.id, current_user=mock_user, db=db
            )

    body = response.body.decode()
    assert "LIVE-DOC" in body
    assert "SOFT-DELETED-DOC" not in body
    assert "UNLINKED-DOC" not in body
    assert "FOREIGN-ORG-DOC" not in body
