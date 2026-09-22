"""Draft generation must scope document_ids to the project's collection.

The document_ids branch previously did `select(Document).where(Document.id.in_(...))`
with no project/org filter, so a caller (who owns project P) could pass another
tenant's document UUIDs and have their title/content synthesized into a draft
stored under P — a cross-tenant document-content leak. The fetch now always joins
collection_documents on collection_id == project_id, so foreign ids are dropped.

Compiles the built query and asserts the collection/project constraint is always
present (both with and without document_ids).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit

from src.services.research.draft_generation_service import DraftGenerationService


def _sql(query):
    return str(query.compile(compile_kwargs={"literal_binds": False})).lower()


def test_docids_query_is_scoped_to_project_collection():
    pid = uuid4()
    q = DraftGenerationService._build_project_documents_query(pid, [uuid4(), uuid4()])
    sql = _sql(q)
    # must join the project's collection membership AND filter the id list
    assert "collection_documents" in sql
    assert "collection_id" in sql
    assert "documents.id in" in sql  # id-list intersection applied


def test_no_docids_query_still_scoped_to_project_collection():
    pid = uuid4()
    q = DraftGenerationService._build_project_documents_query(pid, None)
    sql = _sql(q)
    assert "collection_documents" in sql
    assert "collection_id" in sql


def test_docids_query_never_selects_by_bare_id_without_collection():
    # regression: the old branch was select(Document).where(id.in_(...)) with NO
    # collection join. Ensure the collection join is always present.
    pid = uuid4()
    for docs in (None, [uuid4()]):
        sql = _sql(DraftGenerationService._build_project_documents_query(pid, docs))
        assert "join collection_documents" in sql


def test_docids_query_excludes_soft_deleted_documents():
    # W-B1: soft-deleted docs keep their collection_documents row; the draft
    # source query must filter them or retracted papers get synthesized in.
    pid = uuid4()
    for docs in (None, [uuid4()]):
        sql = _sql(DraftGenerationService._build_project_documents_query(pid, docs))
        # column names always appear in the SELECT list; the guard must be a
        # predicate, so assert inside the WHERE clause specifically
        where_clause = sql.split("where", 1)[1]
        assert "is_deleted is false" in where_clause


def test_document_numbering_has_deterministic_order():
    sql = _sql(DraftGenerationService._build_project_documents_query(uuid4(), None))
    assert "order by documents.id" in sql
