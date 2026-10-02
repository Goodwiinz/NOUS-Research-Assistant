"""Satellite reconciler beat task (audit P2.3 / finding D1).

Contract under test:

- flag-gated: RECONCILER_ENABLED=false skips outright.
- report-only default (RECONCILER_APPLY=false): lists what it WOULD re-drive
  (doc id + org + which satellite) without calling either re-drive core or
  mutating any row.
- apply mode: re-drives via the extracted cores — Neo4j through
  ``repair_document_graph`` (flips to completed + timestamp on ok, stays
  failed otherwise) and DO KB through ``_sync_document_to_kb_blocking``
  (completed on a data-source uuid, stays failed on None), one org-level
  indexing kick per touched org.
- rate cap: at most RECONCILER_MAX_DOCS_PER_RUN documents per run.
- healthy / terminal / still-processing documents are untouched; deleted
  documents are only cleaned up (DO KB data source, Neo4j subgraph), never
  re-indexed.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

from src.models.document import Document, DocumentType, ProcessingStatus
from src.services.knowledge_graph.repair import GraphRepairOutcome
from src.shared.enums import SatelliteSyncStatus
from src.tasks import reconcile_tasks as rt

FAILED = SatelliteSyncStatus.FAILED.value
COMPLETED = SatelliteSyncStatus.COMPLETED.value


@pytest.fixture
def session_factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import src.models  # noqa: F401  register every table on Base.metadata
    from src.models.base import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield Session
    finally:
        engine.dispose()


def _seed_doc(
    factory,
    *,
    org_id=None,
    neo4j=None,
    do_kb=None,
    status=ProcessingStatus.COMPLETED,
    is_deleted=False,
    content_text="text",
    do_kb_uuid=None,
):
    doc_id = uuid.uuid4()
    with factory() as db:
        db.add(
            Document(
                id=doc_id,
                title="Doc",
                filename="doc.txt",
                file_path="local:///tmp/doc.txt",
                file_size_bytes=10,
                mime_type="text/plain",
                document_type=DocumentType.TEXT,
                organization_id=org_id or uuid.uuid4(),
                content_text=content_text,
                processing_status=status,
                neo4j_index_status=neo4j,
                do_kb_sync_status=do_kb,
                do_kb_data_source_uuid=do_kb_uuid,
                is_deleted=is_deleted,
            )
        )
        db.commit()
    return doc_id


def _get(factory, doc_id):
    with factory() as db:
        return db.query(Document).filter(Document.id == doc_id).first()


def _settings(
    *,
    enabled=True,
    apply=False,
    cap=25,
    batch=100,
    do_kb_enabled=True,
):
    return SimpleNamespace(
        RECONCILER_ENABLED=enabled,
        RECONCILER_APPLY=apply,
        RECONCILER_MAX_DOCS_PER_RUN=cap,
        RECONCILER_BATCH_SIZE=batch,
        DO_KB_ENABLED=do_kb_enabled,
    )


def _run(factory, settings, *, repair=None, kb_sync=None, kb_cleanup=None, kick=None):
    """Run the task with all external effects patched; returns (result, mocks)."""
    repair = repair if repair is not None else MagicMock()
    kb_sync = kb_sync if kb_sync is not None else MagicMock(return_value=None)
    kb_cleanup = kb_cleanup if kb_cleanup is not None else MagicMock(return_value=False)
    kick = kick if kick is not None else AsyncMock()
    with (
        patch.object(rt, "SessionLocal", factory),
        patch("src.core.config.get_settings", return_value=settings),
        patch.object(rt, "repair_document_graph", repair),
        patch.object(rt, "_sync_document_to_kb_blocking", kb_sync),
        patch.object(rt, "_cleanup_deleted_do_kb_document", kb_cleanup),
        patch.object(rt, "_kick_do_kb_indexing", kick),
        # Never load spaCy in tests: the lazy service is only consumed by the
        # (mocked) repair core anyway.
        patch.object(rt._LazyExtractionService, "get", lambda self: None),
    ):
        result = rt.reconcile_satellite_indexes()
    return result, SimpleNamespace(
        repair=repair, kb_sync=kb_sync, kb_cleanup=kb_cleanup, kick=kick
    )


def _ok_outcome(doc_id):
    return GraphRepairOutcome(
        document_id=str(doc_id), entities_found=2, entities_created=2
    )


def _bad_outcome(doc_id):
    return GraphRepairOutcome(document_id=str(doc_id), error_message="neo4j down")


# ---------------------------------------------------------------------------
# gating + report-only
# ---------------------------------------------------------------------------


def test_gated_by_reconciler_enabled(session_factory):
    _seed_doc(session_factory, neo4j=FAILED)
    result, mocks = _run(session_factory, _settings(enabled=False))
    assert result == {"skipped": "reconciler-disabled"}
    mocks.repair.assert_not_called()
    mocks.kb_sync.assert_not_called()
    mocks.kb_cleanup.assert_not_called()


def test_report_only_lists_without_acting(session_factory):
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    kg_failed = _seed_doc(session_factory, org_id=org_a, neo4j=FAILED)
    kb_failed = _seed_doc(session_factory, org_id=org_b, do_kb=FAILED)
    # A deleted doc with neo4j=failed is a pending graph cleanup (GOO-358).
    deleted = _seed_doc(session_factory, org_id=org_a, neo4j=FAILED, is_deleted=True)
    # Must all be ignored:
    healthy = _seed_doc(session_factory, org_id=org_a, neo4j=COMPLETED, do_kb=COMPLETED)
    never_attempted = _seed_doc(session_factory, org_id=org_a)
    in_flight = _seed_doc(
        session_factory, org_id=org_a, neo4j=FAILED, status=ProcessingStatus.PROCESSING
    )
    ingest_failed = _seed_doc(
        session_factory, org_id=org_a, neo4j=FAILED, status=ProcessingStatus.FAILED
    )

    result, mocks = _run(session_factory, _settings(apply=False))

    assert result["mode"] == "report-only"
    assert result["eligible"] == 3
    assert result["scanned"] == 3
    reported = {entry["document_id"] for entry in result["report"]}
    assert reported == {str(kg_failed), str(kb_failed), str(deleted)}
    # Every report entry names the org and the drifted satellite.
    by_id = {entry["document_id"]: entry for entry in result["report"]}
    assert by_id[str(kg_failed)]["organization_id"] == str(org_a)
    assert by_id[str(kg_failed)]["neo4j_index_status"] == FAILED
    assert by_id[str(kb_failed)]["do_kb_sync_status"] == FAILED
    assert by_id[str(deleted)]["kg_cleanup_pending"] is True
    assert by_id[str(kg_failed)]["kg_cleanup_pending"] is False

    # Report-only touched nothing and called no re-drive core.
    mocks.repair.assert_not_called()
    mocks.kb_sync.assert_not_called()
    mocks.kick.assert_not_called()
    assert _get(session_factory, kg_failed).neo4j_index_status == FAILED
    assert _get(session_factory, kb_failed).do_kb_sync_status == FAILED
    assert _get(session_factory, healthy).neo4j_index_status == COMPLETED
    assert _get(session_factory, never_attempted).neo4j_index_status is None
    assert _get(session_factory, deleted).neo4j_index_status == FAILED
    assert _get(session_factory, in_flight).neo4j_index_status == FAILED
    assert _get(session_factory, ingest_failed).neo4j_index_status == FAILED


# ---------------------------------------------------------------------------
# apply mode
# ---------------------------------------------------------------------------


def test_apply_redrives_neo4j_via_extracted_core(session_factory):
    org_id = uuid.uuid4()
    doc_id = _seed_doc(session_factory, org_id=org_id, neo4j=FAILED)
    repair = MagicMock(side_effect=lambda doc, **kw: _ok_outcome(doc.id))

    result, mocks = _run(session_factory, _settings(apply=True), repair=repair)

    assert result["mode"] == "apply"
    assert result["kg_repaired"] == 1
    repair.assert_called_once()
    assert str(repair.call_args.args[0].id) == str(doc_id)
    doc = _get(session_factory, doc_id)
    assert doc.neo4j_index_status == COMPLETED
    assert doc.neo4j_indexed_at is not None
    # No DO KB failure on this doc — the KB core must not run.
    mocks.kb_sync.assert_not_called()


def test_apply_keeps_failed_when_repair_core_fails(session_factory):
    doc_id = _seed_doc(session_factory, neo4j=FAILED)
    repair = MagicMock(side_effect=lambda doc, **kw: _bad_outcome(doc.id))

    result, _ = _run(session_factory, _settings(apply=True), repair=repair)

    assert result["kg_still_failed"] == 1
    doc = _get(session_factory, doc_id)
    assert doc.neo4j_index_status == FAILED
    assert doc.neo4j_indexed_at is None


def test_apply_redrives_do_kb_and_kicks_indexing_once_per_org(session_factory):
    org_id = uuid.uuid4()
    doc_a = _seed_doc(session_factory, org_id=org_id, do_kb=FAILED)
    doc_b = _seed_doc(session_factory, org_id=org_id, do_kb=FAILED)
    kb_sync = MagicMock(return_value="ds-1")
    kick = AsyncMock()

    result, _ = _run(session_factory, _settings(apply=True), kb_sync=kb_sync, kick=kick)

    assert result["do_kb_resynced"] == 2
    assert kb_sync.call_count == 2
    # Re-drives register only; ONE org-level indexing kick at the end.
    for call in kb_sync.call_args_list:
        assert call.kwargs.get("trigger_indexing") is False
    kick.assert_awaited_once()
    assert kick.await_args.args[0] == {str(org_id)}
    assert _get(session_factory, doc_a).do_kb_sync_status == COMPLETED
    assert _get(session_factory, doc_b).do_kb_sync_status == COMPLETED


def test_apply_keeps_failed_when_kb_sync_returns_none(session_factory):
    doc_id = _seed_doc(session_factory, do_kb=FAILED)
    kb_sync = MagicMock(return_value=None)
    kick = AsyncMock()

    result, _ = _run(session_factory, _settings(apply=True), kb_sync=kb_sync, kick=kick)

    assert result["do_kb_still_failed"] == 1
    assert _get(session_factory, doc_id).do_kb_sync_status == FAILED
    kick.assert_not_awaited()


def test_apply_skips_do_kb_when_feature_disabled(session_factory):
    doc_id = _seed_doc(session_factory, do_kb=FAILED)
    kb_sync = MagicMock(return_value="ds-1")

    result, mocks = _run(
        session_factory,
        _settings(apply=True, do_kb_enabled=False),
        kb_sync=kb_sync,
    )

    assert result["do_kb_skipped_disabled"] == 1
    kb_sync.assert_not_called()
    assert _get(session_factory, doc_id).do_kb_sync_status == FAILED


def test_apply_retries_deleted_document_do_kb_cleanup(session_factory):
    doc_id = _seed_doc(
        session_factory,
        is_deleted=True,
        do_kb_uuid="ds-pending-delete",
    )
    cleanup = MagicMock(return_value=True)

    result, mocks = _run(
        session_factory,
        _settings(apply=True),
        kb_cleanup=cleanup,
    )

    assert result["eligible"] == 1
    assert result["do_kb_cleanup_succeeded"] == 1
    cleanup.assert_called_once()
    assert cleanup.call_args.args[0].id == doc_id
    mocks.repair.assert_not_called()
    mocks.kb_sync.assert_not_called()


def test_deleted_document_cleanup_uses_fresh_async_session():
    document = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())
    current = SimpleNamespace(is_deleted=True, do_kb_data_source_uuid="ds-pending")
    session = MagicMock()
    session.get = AsyncMock(return_value=current)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    unsync = AsyncMock(return_value=True)

    with (
        patch("src.core.database.AsyncSessionLocal", return_value=context),
        patch("src.services.do_kb.unsync_document_from_kb", new=unsync),
    ):
        result = rt._cleanup_deleted_do_kb_document(document)

    assert result is True
    session.get.assert_awaited_once_with(Document, document.id)
    unsync.assert_awaited_once_with(session, current)


def test_failed_deleted_document_cleanup_remains_retryable(session_factory):
    doc_id = _seed_doc(
        session_factory,
        is_deleted=True,
        do_kb_uuid="ds-pending-delete",
    )

    result, _ = _run(session_factory, _settings(apply=True))

    assert result["do_kb_cleanup_failed"] == 1
    assert _get(session_factory, doc_id).do_kb_data_source_uuid == "ds-pending-delete"


def test_rate_cap_bounds_docs_per_run(session_factory):
    org_id = uuid.uuid4()
    for _ in range(5):
        _seed_doc(session_factory, org_id=org_id, neo4j=FAILED)
    repair = MagicMock(side_effect=lambda doc, **kw: _ok_outcome(doc.id))

    result, _ = _run(
        session_factory, _settings(apply=True, cap=2, batch=1), repair=repair
    )

    assert result["eligible"] == 5
    assert result["scanned"] == 2
    assert repair.call_count == 2
    with session_factory() as db:
        remaining = (
            db.query(Document).filter(Document.neo4j_index_status == FAILED).count()
        )
    assert remaining == 3


def test_doc_failed_on_both_satellites_counts_once_and_redrives_both(
    session_factory,
):
    doc_id = _seed_doc(session_factory, neo4j=FAILED, do_kb=FAILED)
    repair = MagicMock(side_effect=lambda doc, **kw: _ok_outcome(doc.id))
    kb_sync = MagicMock(return_value="ds-9")

    result, _ = _run(
        session_factory, _settings(apply=True), repair=repair, kb_sync=kb_sync
    )

    assert result["scanned"] == 1
    assert result["kg_repaired"] == 1
    assert result["do_kb_resynced"] == 1
    doc = _get(session_factory, doc_id)
    assert doc.neo4j_index_status == COMPLETED
    assert doc.do_kb_sync_status == COMPLETED


def test_beat_entry_registered_and_resolves():
    """The reconciler must be wired into beat and registered on the app."""
    import src.tasks.reconcile_tasks  # noqa: F401
    from src.tasks.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["reconcile-satellite-indexes"]
    assert entry["task"] == "src.tasks.reconcile_tasks.reconcile_satellite_indexes"
    assert entry["task"] in celery_app.tasks
    assert "src.tasks.reconcile_tasks" in celery_app.conf.include


def test_apply_retries_deleted_document_graph_cleanup(session_factory):
    """A deleted document marked neo4j=failed (a late graph write whose
    cleanup failed) gets its subgraph deleted, never re-indexed (GOO-358)."""
    org_id = uuid.uuid4()
    doc_id = _seed_doc(session_factory, org_id=org_id, neo4j=FAILED, is_deleted=True)
    kg = MagicMock()

    with patch(
        "src.services.knowledge_graph.knowledge_graph_service.KnowledgeGraphService",
        return_value=kg,
    ):
        result, mocks = _run(session_factory, _settings(apply=True))

    assert result["kg_cleanup_succeeded"] == 1
    kg.delete_document_graph.assert_called_once_with(str(doc_id), str(org_id))
    mocks.repair.assert_not_called()
    mocks.kb_sync.assert_not_called()
    doc = _get(session_factory, doc_id)
    assert doc.neo4j_index_status == COMPLETED
    assert doc.is_deleted is True


def test_failed_deleted_document_graph_cleanup_remains_retryable(session_factory):
    doc_id = _seed_doc(session_factory, neo4j=FAILED, is_deleted=True)
    kg = MagicMock()
    kg.delete_document_graph.side_effect = RuntimeError("neo4j down")

    with patch(
        "src.services.knowledge_graph.knowledge_graph_service.KnowledgeGraphService",
        return_value=kg,
    ):
        result, _ = _run(session_factory, _settings(apply=True))

    assert result["kg_cleanup_failed"] == 1
    assert _get(session_factory, doc_id).neo4j_index_status == FAILED
