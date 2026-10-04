"""Deleting a document must clean up its Neo4j entity subgraph (audit D2).

Before this fix, `delete_document` / `bulk_delete_documents` soft-deleted the
document's Postgres `Entity` rows and unsynced its DO KB data source, but never
touched the mirrored `:Entity` nodes in Neo4j — so a deleted document's graph
entities orphaned there permanently (`knowledge_graph_service.delete_entity`
existed but had no per-document caller).

The fix calls `KnowledgeGraphService.delete_document_graph` (reference-count
style: the doc's relationships first, then only fully-orphaned nodes it
created — entity nodes are SHARED across documents, see the service tests)
scoped to the org, after the Postgres commit. Failure isolation is
load-bearing: a neo4j outage (or an open circuit breaker) during delete must
not raise or block the user's delete.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.documents import documents as documents_mod

# `_cleanup_document_graph` imports KnowledgeGraphService lazily from here, so
# patching this attribute intercepts the call.
KG_PATH = "src.services.knowledge_graph.knowledge_graph_service.KnowledgeGraphService"


def _result(value):
    r = MagicMock()
    r.scalars.return_value.first.return_value = value
    return r


def _file_service(*documents):
    """The delete transaction lives in FileService.soft_delete_documents (proven
    against Postgres in tests/integration/test_document_deletion_postgres.py);
    these tests cover the router's post-commit cleanup."""
    file_service = MagicMock()
    file_service.soft_delete_documents = AsyncMock(return_value=list(documents))
    return file_service


def _make_document(*, ds_uuid=None):
    document = MagicMock()
    document.id = uuid.uuid4()
    document.organization_id = uuid.uuid4()
    document.file_size_bytes = 100
    document.do_kb_data_source_uuid = ds_uuid  # None → DO KB cleanup is a no-op
    document.uploaded_by_user_id = "user-1"
    document.organization = MagicMock()
    return document


def _user():
    user = MagicMock()
    user.id = "user-1"
    user.has_permission.return_value = True
    return user


def _single_delete_db(document):
    """db.execute call order for a cascade single delete: select, entity update,
    job update, atomic quota update."""
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _result(document),  # select the document
        ]
    )
    db.commit = AsyncMock()
    return db


@pytest.mark.unit
def test_delete_document_cleans_up_graph():
    """Deleting a doc reaps its Neo4j graph, keyed by doc id + org."""
    document = _make_document()
    org = MagicMock()
    org.id = document.organization_id

    db = _single_delete_db(document)

    with patch(KG_PATH) as kg_cls:
        resp = asyncio.run(
            documents_mod.delete_document(
                document_id=str(document.id),
                cascade=True,
                current_user=_user(),
                organization=org,
                db=db,
                file_service=_file_service(document),
            )
        )

    kg_cls.return_value.delete_document_graph.assert_called_once_with(
        str(document.id), str(org.id)
    )
    assert resp["document_id"] == str(document.id)


@pytest.mark.unit
def test_delete_document_skips_graph_cleanup_when_not_cascade():
    """cascade=False leaves related entities in Postgres → also leave the graph."""
    document = _make_document()
    org = MagicMock()
    org.id = document.organization_id

    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _result(document),  # select the document
        ]
    )
    db.commit = AsyncMock()

    with patch(KG_PATH) as kg_cls:
        asyncio.run(
            documents_mod.delete_document(
                document_id=str(document.id),
                cascade=False,
                current_user=_user(),
                organization=org,
                db=db,
                file_service=_file_service(document),
            )
        )

    kg_cls.return_value.delete_document_graph.assert_not_called()


@pytest.mark.unit
def test_delete_document_graph_failure_does_not_block_delete():
    """A neo4j outage during graph cleanup must not raise out of the endpoint."""
    document = _make_document()
    org = MagicMock()
    org.id = document.organization_id

    db = _single_delete_db(document)

    with patch(KG_PATH) as kg_cls:
        kg_cls.return_value.delete_document_graph.side_effect = RuntimeError(
            "neo4j circuit breaker open"
        )
        resp = asyncio.run(
            documents_mod.delete_document(
                document_id=str(document.id),
                cascade=True,
                current_user=_user(),
                organization=org,
                db=db,
                file_service=_file_service(document),
            )
        )

    assert resp["document_id"] == str(document.id)
    kg_cls.return_value.delete_document_graph.assert_called_once()


@pytest.mark.unit
def test_bulk_delete_defers_graph_cleanup_to_background_task():
    """Bulk delete must NOT run graph cleanup inline (up to 100 neo4j round-trips
    would block the response); it registers ONE background task covering every
    deleted doc, and that task — run after the response — deletes each subgraph
    keyed by doc id + the shared org."""
    from starlette.background import BackgroundTasks

    doc_a = _make_document()
    doc_b = _make_document()
    org = MagicMock()
    org.id = uuid.uuid4()

    request = MagicMock()
    request.document_ids = [str(doc_a.id), str(doc_b.id)]

    db = MagicMock()
    select_result = MagicMock()
    select_result.scalars.return_value.all.return_value = [doc_a, doc_b]
    db.execute = AsyncMock(
        side_effect=[
            select_result,  # select all docs
        ]
    )
    db.commit = AsyncMock()

    background_tasks = BackgroundTasks()
    with patch(KG_PATH) as kg_cls:
        resp = asyncio.run(
            documents_mod.bulk_delete_documents(
                request=request,
                cascade=True,
                background_tasks=background_tasks,
                current_user=_user(),
                organization=org,
                db=db,
                file_service=_file_service(doc_a, doc_b),
            )
        )

        # Response returned WITHOUT any inline graph work...
        assert resp.success_count == 2
        kg_cls.return_value.delete_document_graph.assert_not_called()
        # ...but ONE cleanup task is registered for both docs (no DO KB task here
        # since neither doc has a data source), scoped to the shared org.
        assert len(background_tasks.tasks) == 1
        assert background_tasks.tasks[0].args == (
            [str(doc_a.id), str(doc_b.id)],
            str(org.id),
        )

        # Now run the background task the way Starlette would (post-response).
        asyncio.run(background_tasks())

    assert kg_cls.return_value.delete_document_graph.call_count == 2
    kg_cls.return_value.delete_document_graph.assert_any_call(
        str(doc_a.id), str(org.id)
    )
    kg_cls.return_value.delete_document_graph.assert_any_call(
        str(doc_b.id), str(org.id)
    )


@pytest.mark.unit
def test_bulk_delete_graph_cleanup_swallows_failure():
    """A neo4j outage inside the background cleanup must not raise (it would kill
    the remaining docs' cleanup) — each doc is failure-isolated + logged."""
    doc_a = _make_document()
    doc_b = _make_document()
    org = MagicMock()
    org.id = uuid.uuid4()

    with patch(KG_PATH) as kg_cls:
        kg_cls.return_value.delete_document_graph.side_effect = RuntimeError(
            "neo4j down"
        )
        # Must not raise, and must attempt BOTH docs despite the first failing.
        asyncio.run(
            documents_mod._cleanup_document_graphs_background(
                [str(doc_a.id), str(doc_b.id)], str(org.id)
            )
        )

    assert kg_cls.return_value.delete_document_graph.call_count == 2
