"""GOO-349: citation reads + bibliography export follow the org document boundary.

Before: ``_document_is_accessible`` accepted ``is_public`` or uploader identity
with no organization check (a foreign-org public document, or a document the
caller uploaded in a previous org, exposed its citations); the list SQL
mirrored that; and the bibliography fallback loaded project document ids with
no organization guard (stale project-document links to foreign documents).

Mutation check: drop the organization comparison in
``_document_is_accessible`` / ``_document_access_clause`` / the bibliography
fallback and ``pytest -q backend/tests/unit/api/test_citation_document_org_scope.py``
fails.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from src.api.research import citations as cit

pytestmark = pytest.mark.unit

ORG = uuid4()
OTHER_ORG = uuid4()


def _user(org: Optional[UUID] = ORG) -> Any:
    return SimpleNamespace(id=uuid4(), organization_id=org)


def _doc(
    *,
    org: Optional[UUID],
    uploader: Optional[UUID] = None,
    is_public: bool = False,
    is_deleted: bool = False,
) -> Any:
    return SimpleNamespace(
        organization_id=org,
        uploaded_by_user_id=uploader,
        is_public=is_public,
        is_deleted=is_deleted,
    )


def _citation(document: Any) -> Any:
    return SimpleNamespace(is_deleted=False, document=document, message=None)


def test_foreign_org_public_document_does_not_grant_citation_access() -> None:
    user = _user()
    assert not cit._citation_is_accessible(
        _citation(_doc(org=OTHER_ORG, is_public=True)), user
    )


def test_old_org_uploader_does_not_grant_citation_access() -> None:
    user = _user()
    assert not cit._citation_is_accessible(
        _citation(_doc(org=OTHER_ORG, uploader=user.id)), user
    )


def test_orgless_caller_gets_no_document_path_access() -> None:
    user = _user(org=None)
    assert not cit._citation_is_accessible(
        _citation(_doc(org=None, uploader=user.id, is_public=True)), user
    )


@pytest.mark.parametrize("public,own", [(True, False), (False, True)])
def test_same_org_public_or_own_document_still_accessible(
    public: bool, own: bool
) -> None:
    user = _user()
    doc = _doc(org=ORG, is_public=public, uploader=user.id if own else uuid4())
    assert cit._citation_is_accessible(_citation(doc), user)


def _sql(stmt: Any) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


@pytest.mark.asyncio
async def test_list_citations_sql_filters_document_organization() -> None:
    user = _user()
    db = AsyncMock()
    result = MagicMock()
    result.scalar = Mock(return_value=0)
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)

    await cit.list_citations(
        message_id=None,
        document_id=None,
        arxiv_id=None,
        doi=None,
        needs_review=None,
        skip=0,
        limit=50,
        current_user=user,
        db=db,
    )
    count_sql, list_sql = (_sql(c.args[0]) for c in db.execute.call_args_list)
    for sql in (count_sql, list_sql):
        assert f"documents.organization_id = '{ORG.hex}'" in sql.replace("-", "")


@pytest.mark.asyncio
async def test_list_citations_orgless_caller_document_branch_matches_nothing() -> None:
    user = _user(org=None)
    db = AsyncMock()
    result = MagicMock()
    result.scalar = Mock(return_value=0)
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)

    await cit.list_citations(
        message_id=None,
        document_id=None,
        arxiv_id=None,
        doi=None,
        needs_review=None,
        skip=0,
        limit=50,
        current_user=user,
        db=db,
    )
    sql = _sql(db.execute.call_args_list[0].args[0])
    assert "documents.organization_id IS NULL" not in sql
    assert "documents.is_public" not in sql


@pytest.mark.asyncio
async def test_bibliography_fallback_scopes_project_documents_to_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user()
    monkeypatch.setattr(cit, "resolve_project", AsyncMock())
    statements: list[Any] = []

    doc_ids = MagicMock()
    doc_ids.all.return_value = [(uuid4(),)]
    no_citations = MagicMock()
    no_citations.scalars.return_value.all.return_value = []
    no_docs = MagicMock()
    no_docs.scalars.return_value.all.return_value = []
    results = iter([doc_ids, no_citations, no_docs])

    async def execute(stmt: Any) -> Any:
        statements.append(stmt)
        return next(results)

    db = AsyncMock()
    db.execute = execute

    with pytest.raises(HTTPException) as exc:
        await cit.export_bibliography(
            request=None,
            format="bibtex",
            citation_ids=None,
            project_id=uuid4(),
            current_user=user,
            db=db,
        )
    assert exc.value.status_code == 404
    fallback_sql = _sql(statements[-1]).replace("-", "")
    assert f"documents.organization_id = '{ORG.hex}'" in fallback_sql
