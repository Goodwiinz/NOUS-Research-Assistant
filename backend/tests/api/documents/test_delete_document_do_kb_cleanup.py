"""Deleting a document must clean up its DO KB data source (audit B4).

Before this fix, `delete_document` / `bulk_delete_documents` soft-deleted the
Document but never removed its DO KB data source. Because
`resolve_and_filter_chunks` resolves chunks by `storage_path` + org and does
NOT filter `is_deleted`, the soft-deleted doc's chunks stayed live in the KB
index and kept surfacing in retrieval — plus the data source leaked storage on
DO's side.

The fix wires the existing `unsync_document_from_kb` helper into both delete
paths (after the commit, best-effort). Failure isolation is load-bearing: a KB
outage during delete must not raise or block the user's delete.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.documents import documents as documents_mod


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


def _make_document(*, ds_uuid: str | None = "ds-123"):
    document = MagicMock()
    document.id = uuid.uuid4()
    document.organization_id = uuid.uuid4()
    document.file_size_bytes = 100
    document.do_kb_data_source_uuid = ds_uuid
    # owner check: delete_document compares uploaded_by_user_id to current_user.id
    document.uploaded_by_user_id = "user-1"
    document.organization = MagicMock()
    return document


@pytest.mark.unit
def test_delete_document_unsyncs_do_kb_data_source():
    """A doc with a do_kb_data_source_uuid triggers unsync on delete."""
    document = _make_document(ds_uuid="ds-123")

    user = MagicMock()
    user.id = "user-1"
    user.has_permission.return_value = True
    org = MagicMock()
    org.id = document.organization_id

    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _result(document),
        ]
    )
    db.commit = AsyncMock()

    file_service = _file_service(document)

    unsync = AsyncMock(return_value=True)
    with patch("src.services.do_kb.unsync_document_from_kb", new=unsync):
        resp = asyncio.run(
            documents_mod.delete_document(
                document_id=str(document.id),
                cascade=True,
                current_user=user,
                organization=org,
                db=db,
                file_service=file_service,
            )
        )

    unsync.assert_awaited_once_with(db, document)
    assert resp["document_id"] == str(document.id)


@pytest.mark.unit
def test_delete_document_skips_unsync_when_no_data_source():
    """No do_kb_data_source_uuid → no unsync call (nothing to clean up)."""
    document = _make_document(ds_uuid=None)

    user = MagicMock()
    user.id = "user-1"
    user.has_permission.return_value = True
    org = MagicMock()
    org.id = document.organization_id

    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _result(document),
        ]
    )
    db.commit = AsyncMock()

    unsync = AsyncMock(return_value=True)
    with patch("src.services.do_kb.unsync_document_from_kb", new=unsync):
        asyncio.run(
            documents_mod.delete_document(
                document_id=str(document.id),
                cascade=True,
                current_user=user,
                organization=org,
                db=db,
                file_service=_file_service(document),
            )
        )

    unsync.assert_not_awaited()


@pytest.mark.unit
def test_delete_document_do_kb_failure_does_not_block_delete():
    """A KB outage during unsync must not raise out of the delete endpoint."""
    document = _make_document(ds_uuid="ds-123")

    user = MagicMock()
    user.id = "user-1"
    user.has_permission.return_value = True
    org = MagicMock()
    org.id = document.organization_id

    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _result(document),
        ]
    )
    db.commit = AsyncMock()

    # unsync_document_from_kb is documented never-raises, but defend the wire-in:
    # even if it blows up, the user's delete must still succeed.
    boom = AsyncMock(side_effect=RuntimeError("DO KB down"))
    with patch("src.services.do_kb.unsync_document_from_kb", new=boom):
        resp = asyncio.run(
            documents_mod.delete_document(
                document_id=str(document.id),
                cascade=True,
                current_user=user,
                organization=org,
                db=db,
                file_service=_file_service(document),
            )
        )

    assert resp["document_id"] == str(document.id)
    boom.assert_awaited_once()


def _session_ctx(db):
    """Async context manager stub standing in for AsyncSessionLocal()."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


@pytest.mark.unit
def test_bulk_delete_defers_do_kb_cleanup_to_background_task():
    """Bulk delete must NOT await DO KB cleanup inline (up to 100 DO HTTP calls
    with retries would block the response); it registers ONE background task
    covering only the docs that had a data source, and that task — run after
    the response with its own session — unsyncs each of them."""
    from starlette.background import BackgroundTasks

    doc_a = _make_document(ds_uuid="ds-a")
    doc_b = _make_document(ds_uuid=None)  # no DS → excluded from cleanup

    user = MagicMock()
    user.id = "user-1"
    user.has_permission.return_value = True  # ADMIN gate
    org = MagicMock()

    request = MagicMock()
    request.document_ids = [str(doc_a.id), str(doc_b.id)]

    db = MagicMock()
    # Set-based bulk delete: one select returning all docs, then batch entity
    # update, batch job update, one atomic quota update.
    select_result = MagicMock()
    select_result.scalars.return_value.all.return_value = [doc_a, doc_b]
    db.execute = AsyncMock(
        side_effect=[
            select_result,  # select all docs
        ]
    )
    db.commit = AsyncMock()

    background_tasks = BackgroundTasks()
    unsync = AsyncMock(return_value=True)
    with patch("src.services.do_kb.unsync_document_from_kb", new=unsync):
        resp = asyncio.run(
            documents_mod.bulk_delete_documents(
                request=request,
                cascade=True,
                background_tasks=background_tasks,
                current_user=user,
                organization=org,
                db=db,
                file_service=_file_service(doc_a, doc_b),
            )
        )

        # Response returned WITHOUT touching DO KB inline...
        assert resp.success_count == 2
        unsync.assert_not_awaited()
        # ...but cleanup for the doc with a data source is registered. (A
        # sibling Neo4j graph-cleanup task is also registered now — see
        # test_delete_document_neo4j_cleanup — so locate the DO KB task by its
        # callable rather than by position/count.)
        kb_tasks = [
            t
            for t in background_tasks.tasks
            if t.func is documents_mod._cleanup_do_kb_data_sources_background
        ]
        assert len(kb_tasks) == 1
        assert kb_tasks[0].args == ([str(doc_a.id)],)

        # Now run the background task the way Starlette would (post-response),
        # with a fresh-session stub in place of AsyncSessionLocal. Stub the KG
        # service so the sibling graph-cleanup task is an inert no-op here.
        bg_db = MagicMock()
        bg_db.get = AsyncMock(return_value=doc_a)
        with (
            patch(
                "src.core.database.AsyncSessionLocal",
                return_value=_session_ctx(bg_db),
            ),
            patch(
                "src.services.knowledge_graph.knowledge_graph_service."
                "KnowledgeGraphService"
            ),
        ):
            asyncio.run(background_tasks())

    # doc_a had a data source → unsynced on the BACKGROUND session, not `db`.
    unsync.assert_awaited_once_with(bg_db, doc_a)


@pytest.mark.unit
def test_bulk_delete_background_cleanup_swallows_do_failure():
    """A DO outage inside the background cleanup must not raise (it would kill
    the remaining docs' cleanup and log a worker error) — swallow + log."""
    doc = _make_document(ds_uuid="ds-123")

    bg_db = MagicMock()
    bg_db.get = AsyncMock(return_value=doc)

    boom = AsyncMock(side_effect=RuntimeError("DO KB down"))
    with (
        patch("src.services.do_kb.unsync_document_from_kb", new=boom),
        patch(
            "src.core.database.AsyncSessionLocal",
            return_value=_session_ctx(bg_db),
        ),
    ):
        # Must not raise.
        asyncio.run(documents_mod._cleanup_do_kb_data_sources_background([str(doc.id)]))

    boom.assert_awaited_once()


@pytest.mark.unit
def test_unsync_helper_retains_uuid_and_swallows_do_failure():
    """A DO outage keeps the data-source UUID available for a later retry."""
    from src.services.do_kb import ingest as ingest_mod

    document = MagicMock()
    document.id = uuid.uuid4()
    document.organization_id = uuid.uuid4()
    document.do_kb_data_source_uuid = "ds-123"
    document.do_kb_indexed_at = "2026-01-01"
    document.do_kb_index_status = "indexed"

    session = MagicMock()
    session.commit = AsyncMock()

    # DO client whose delete_data_source blows up (KB outage).
    client = MagicMock()
    client.delete_data_source = AsyncMock(side_effect=RuntimeError("DO KB down"))

    fake_settings = MagicMock()
    fake_settings.DO_KB_ENABLED = True

    with (
        patch.object(ingest_mod, "settings", fake_settings),
        patch.object(
            ingest_mod, "ensure_kb_for_org", new=AsyncMock(return_value="kb-uuid")
        ),
    ):
        result = asyncio.run(
            ingest_mod.unsync_document_from_kb(session, document, client=client)
        )

    assert document.do_kb_data_source_uuid == "ds-123"
    assert document.do_kb_indexed_at == "2026-01-01"
    assert document.do_kb_index_status == "indexed"
    session.commit.assert_not_awaited()
    assert result is False
