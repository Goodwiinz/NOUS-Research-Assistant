"""Regression tests for research project and citation API bugs."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.api.research import citations as citations_api
from src.api.research import drafts as drafts_api
from src.api.research import projects as projects_api


def _result(*, scalar=None, scalars=None, rows=None):
    """Create a lightweight SQLAlchemy-like result mock."""
    result = Mock()
    scalar_items = list(scalars or [])
    result.scalar_one_or_none = Mock(return_value=scalar)
    result.scalar = Mock(return_value=scalar)
    result.all = Mock(return_value=list(rows or []))
    result.scalars = Mock(
        return_value=SimpleNamespace(
            all=lambda: scalar_items,
            first=lambda: scalar_items[0] if scalar_items else None,
        )
    )
    return result


def _make_user():
    return SimpleNamespace(
        id=uuid4(), organization_id=uuid4(), email="owner@example.com"
    )


@pytest.mark.asyncio
async def test_draft_review_access_rejects_deleted_workspace() -> None:
    db = AsyncMock()
    project_id = uuid4()
    user = _make_user()
    with patch.object(
        drafts_api,
        "resolve_project",
        AsyncMock(
            side_effect=HTTPException(status_code=404, detail="Project not found")
        ),
    ) as resolve:
        with pytest.raises(HTTPException) as exc:
            await drafts_api.list_draft_reviews(
                project_id=project_id, current_user=user, db=db, limit=20
            )

    assert exc.value.status_code == 404
    resolve.assert_awaited_once_with(
        db, project_id, user.id, drafts_api.ResearchAction.VIEW
    )
    db.execute.assert_not_awaited()


def _make_document(**overrides):
    now = datetime.now(timezone.utc)
    defaults = {
        "id": uuid4(),
        "title": "Test Document",
        "filename": "test.pdf",
        "document_type": "pdf",
        "processing_status": "completed",
        "created_at": now,
        "uploaded_by_user_id": uuid4(),
        "is_public": False,
        "content_text": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_citation(**overrides):
    now = datetime.now(timezone.utc)
    defaults = {
        "id": uuid4(),
        "message_id": None,
        "document_id": uuid4(),
        "external_reference_id": None,
        "document_title": "Test Paper",
        "document_type": "paper",
        "chunk_index": None,
        "chunk_id": None,
        "snippet": "Snippet",
        "page_number": 1,
        "score": 0.9,
        "rerank_score": None,
        "authors": ["Author One"],
        "year": 2024,
        "venue": "Journal",
        "doi": None,
        "arxiv_id": None,
        "abstract": None,
        "metadata_source": "manual",
        "needs_review": False,
        "created_at": now,
        "updated_at": now,
        "document": None,
        "message": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_project(**overrides):
    now = datetime.now(timezone.utc)
    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "name": "Research Project",
        "description": "Project description",
        "project_type": "research",
        "research_status": "active",
        "research_goals": None,
        "deadline": None,
        "tags": [],
        "is_private": True,
        "created_at": now,
        "updated_at": now,
        "documents": [],
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_note(project_id, user_id, **overrides):
    now = datetime.now(timezone.utc)
    defaults = {
        "id": uuid4(),
        "project_id": project_id,
        "user_id": user_id,
        "title": "Note title",
        "content": "Full note content",
        "content_preview": "Full note content",
        "linked_document_ids": [],
        "tags": [],
        "is_pinned": False,
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "doc_overrides",
    [
        {"organization_id": uuid4()},  # foreign organization, even if public
        {"organization_id": uuid4(), "is_public": True},
        {"is_deleted": True},  # caller's organization, but soft-deleted
    ],
)
async def test_get_citation_rejects_foreign_org_or_deleted_document(
    doc_overrides: dict,
) -> None:
    current_user = _make_user()
    doc = _make_document(
        **{"organization_id": current_user.organization_id, **doc_overrides}
    )
    citation = _make_citation(document_id=doc.id, document=doc)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(scalar=citation))

    with pytest.raises(HTTPException) as exc_info:
        await citations_api.get_citation(
            citation_id=citation.id,
            current_user=current_user,
            db=db,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_citation_allows_same_org_colleague_private_document() -> None:
    """GOO-410: documents are organization-shared, so a colleague's private
    document's citation is readable like the document itself."""
    current_user = _make_user()
    private_doc = _make_document(
        organization_id=current_user.organization_id,
        uploaded_by_user_id=uuid4(),
        is_public=False,
    )
    citation = _make_citation(document_id=private_doc.id, document=private_doc)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(scalar=citation))

    response = await citations_api.get_citation(
        citation_id=citation.id,
        current_user=current_user,
        db=db,
    )

    assert response.id == citation.id


@pytest.mark.asyncio
async def test_list_citations_applies_access_filter_in_sql():
    """Access filtering moved from Python into the SQL statement (W-A3).

    A mock DB can't evaluate the WHERE clause, so assert the emitted
    statements carry the access predicates and SQL-level pagination; the
    behavioral exclusion cases are covered by real-DB integration tests
    (tests/integration/test_citations_api.py::TestCitationTenancy).
    """
    current_user = _make_user()
    visible_citation = _make_citation(
        document=_make_document(uploaded_by_user_id=current_user.id),
    )

    executed = []

    async def capture_execute(statement):
        executed.append(str(statement).lower())
        if len(executed) == 1:  # count query
            return _result(scalar=1)
        return _result(scalars=[visible_citation])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=capture_execute)

    response = await citations_api.list_citations(
        skip=0,
        limit=50,
        current_user=current_user,
        db=db,
    )

    assert response.total == 1
    assert len(response.citations) == 1
    count_sql, list_sql = executed
    for sql in (count_sql, list_sql):
        # GOO-410: the document branch is org + not deleted, nothing narrower.
        assert "documents.organization_id" in sql
        assert "documents.is_deleted" in sql
        assert "documents.is_public" not in sql
        assert "documents.uploaded_by_user_id" not in sql
        assert "owner_id" in sql
    assert "limit" in list_sql and "offset" in list_sql


@pytest.mark.asyncio
async def test_export_bibliography_rejects_inaccessible_project():
    current_user = _make_user()
    project_id = uuid4()
    doc_id = uuid4()
    citation = _make_citation(document_id=doc_id)

    db = AsyncMock()

    async def execute_side_effect(statement):
        sql = str(statement).lower()
        if "workspaces.owner_id" in sql or "join workspaces" in sql:
            return _result(scalar=None)
        if "collection_documents.document_id" in sql:
            return _result(rows=[(doc_id,)])
        return _result(scalars=[citation])

    db.execute = AsyncMock(side_effect=execute_side_effect)

    with (
        patch.object(
            citations_api, "resolve_project", AsyncMock(side_effect=HTTPException(404))
        ) as resolve,
        patch.object(
            citations_api.BibliographyService,
            "format_bibliography",
            return_value="@article{test}",
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await citations_api.export_bibliography(
                request=None,
                format="bibtex",
                citation_ids=None,
                project_id=project_id,
                current_user=current_user,
                db=db,
            )

    assert exc_info.value.status_code == 404
    resolve.assert_awaited_once_with(
        db, project_id, current_user.id, citations_api.ResearchAction.VIEW
    )


@pytest.mark.asyncio
async def test_add_document_to_project_accepts_colleague_private_document() -> None:
    """GOO-410: the org-scoped document lookup is the whole check, so a
    colleague's private document passes it. The junction lookup then finds a
    live row (409), which proves the document check let it through."""
    current_user = _make_user()
    project_id = uuid4()
    private_doc = _make_document(
        organization_id=current_user.organization_id,
        uploaded_by_user_id=uuid4(),
        is_public=False,
    )

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _result(scalar=private_doc),
            _result(scalar=SimpleNamespace(is_deleted=False)),
        ]
    )
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = Mock()

    with patch.object(
        projects_api,
        "_get_project_with_auth",
        AsyncMock(return_value=SimpleNamespace(id=project_id)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await projects_api.add_document_to_project(
                project_id=project_id,
                document_id=private_doc.id,
                current_user=current_user,
                db=db,
            )

    assert exc_info.value.status_code == 409
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_add_document_to_project_scopes_document_lookup_to_org():
    current_user = _make_user()
    project_id = uuid4()
    statements = []

    async def capture_execute(statement):
        statements.append(str(statement).lower())
        return _result(scalar=None)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=capture_execute)

    with patch.object(
        projects_api,
        "_get_project_with_auth",
        AsyncMock(return_value=SimpleNamespace(id=project_id)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await projects_api.add_document_to_project(
                project_id=project_id,
                document_id=uuid4(),
                current_user=current_user,
                db=db,
            )

    assert exc_info.value.status_code == 404
    assert "documents.organization_id" in statements[0]
    assert "documents.is_deleted" in statements[0]


@pytest.mark.asyncio
async def test_project_bibliography_scopes_citations_and_fallback_to_org() -> None:
    """GOO-410: a project can link a document from another organization (a
    cross-org workspace member added it), so the bibliography's citation read
    and its document-metadata fallback both apply the org + not-deleted
    boundary, like list_project_documents and the citations export."""
    current_user = _make_user()
    project = _make_project()
    statements = []
    results = iter(
        [
            _result(rows=[(uuid4(),)]),  # project document ids
            _result(scalars=[]),  # citations for those documents
            _result(scalars=[]),  # document-metadata fallback
        ]
    )

    async def capture_execute(statement: object) -> object:
        statements.append(str(statement).lower())
        return next(results)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=capture_execute)

    with patch.object(
        projects_api, "_get_project_with_auth", AsyncMock(return_value=project)
    ):
        response = await projects_api.get_project_bibliography(
            project_id=project.id,
            format="bibtex",
            current_user=current_user,
            db=db,
        )

    assert response["citation_count"] == 0
    citation_sql, fallback_sql = statements[1], statements[2]
    for sql in (citation_sql, fallback_sql):
        assert "documents.organization_id" in sql
        assert "documents.is_deleted" in sql


@pytest.mark.asyncio
async def test_list_project_documents_scopes_joined_documents_to_org():
    current_user = _make_user()
    project_id = uuid4()
    statements = []

    async def capture_execute(statement):
        statements.append(str(statement).lower())
        return _result(scalars=[])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=capture_execute)

    with patch.object(
        projects_api,
        "_get_project_with_auth",
        AsyncMock(return_value=SimpleNamespace(id=project_id)),
    ):
        response = await projects_api.list_project_documents(
            project_id=project_id,
            current_user=current_user,
            db=db,
        )

    assert response["documents"] == []
    assert "join documents" in statements[0]
    assert "documents.organization_id" in statements[0]
    assert "collection_documents.is_deleted" in statements[0]


@pytest.mark.asyncio
async def test_get_project_response_includes_notes_field():
    current_user = _make_user()
    project = _make_project()
    note = _make_note(project_id=project.id, user_id=current_user.id)
    collection_doc = SimpleNamespace(
        document=SimpleNamespace(
            id=uuid4(),
            filename="paper.pdf",
            document_type="pdf",
            processing_status="completed",
            created_at=datetime.now(timezone.utc),
        ),
        sort_order=0,
    )

    statements = []
    results = iter(
        [
            _result(scalars=[collection_doc]),
            _result(scalars=[note]),
        ]
    )

    async def capture_execute(statement):
        statements.append(str(statement).lower())
        return next(results)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=capture_execute)

    with patch.object(projects_api, "ProjectService") as mock_service_cls:
        mock_service = mock_service_cls.return_value
        mock_service.get_project_for_user = AsyncMock(return_value=project)

        response = await projects_api.get_project(
            project_id=project.id,
            current_user=current_user,
            db=db,
        )

    assert hasattr(response, "notes")
    assert len(response.notes) == 1
    assert response.notes[0].title == "Note title"
    assert "documents.organization_id" in statements[0]
    assert "collection_documents.is_deleted" in statements[0]
