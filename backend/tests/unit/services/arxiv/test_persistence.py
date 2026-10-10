"""Regression coverage for shared arXiv persistence and deduplication."""

import asyncio
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from src.core.config import settings
from src.models.document import Document, DocumentType, ProcessingStatus
from src.services.arxiv.persistence import persist_arxiv_documents
from tests.mocks.services import MockAsyncSession, MockResult

pytestmark = pytest.mark.unit


def _source(arxiv_id: str = "2401.00001v1") -> SimpleNamespace:
    return SimpleNamespace(
        title="Placeholder",
        content_text="paper content",
        content_summary=None,
        document_metadata={
            "arxiv_id": arxiv_id,
            "title": "  Durable\nPaper  ",
            "publication_date": datetime(2024, 1, 1, tzinfo=timezone.utc),
        },
    )


def _storage_fields(checksum: str = "abc123") -> dict[str, object]:
    return {
        "filename": "2401.00001v1.pdf",
        "file_size_bytes": 12,
        "mime_type": "application/pdf",
        "document_type": "PDF",
        "checksum_sha256": checksum,
        "file_path": "s3://bucket/key",
        "storage_backend": "s3",
        "storage_path": "key",
    }


def _existing_document(
    arxiv_id: str = "2401.00001v1", *, searchable: bool = True
) -> Document:
    return Document(
        id=uuid4(),
        arxiv_id=arxiv_id,
        title="Existing paper",
        filename=f"{arxiv_id}.pdf",
        file_path="s3://bucket/key",
        file_size_bytes=12,
        mime_type="application/pdf",
        document_type=DocumentType.PDF,
        storage_backend="s3",
        storage_path="key",
        checksum_sha256="abc123",
        content_text="paper content",
        document_metadata={"arxiv_id": arxiv_id},
        search_vector="paper" if searchable else None,
        processing_status=ProcessingStatus.COMPLETED,
        organization_id=uuid4(),
        is_public=False,
        do_kb_data_source_uuid="data-source-1",
    )


async def test_exact_arxiv_replay_reuses_before_storage() -> None:
    existing = _existing_document()
    db = MockAsyncSession().set_scalars_result([existing])

    with (
        patch("src.core.database.AsyncSessionLocal", return_value=db),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch("src.services.arxiv.persistence.store_arxiv_pdf") as store,
    ):
        result = await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=existing.organization_id
        )

    assert result.document_ids == [str(existing.id)]
    assert result.reused_document_ids == {str(existing.id)}
    assert db._added_items == []
    store.assert_not_called()


async def test_checksum_conflict_is_reported_and_deletes_promoted_object() -> None:
    existing = _existing_document("other-paper-v1")
    lookup_db = MockAsyncSession()
    write_db = MockAsyncSession()
    write_db.execute = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            MockResult(scalars_result=[]),
            MockResult(scalar_result=existing),
        ]
    )
    sessions = iter([lookup_db, write_db])
    storage_fields = _storage_fields()

    with (
        patch(
            "src.core.database.AsyncSessionLocal", side_effect=lambda: next(sessions)
        ),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch(
            "src.services.arxiv.persistence.store_arxiv_pdf",
            return_value=storage_fields,
        ),
        patch("src.services.arxiv.persistence.delete_arxiv_storage") as delete,
    ):
        result = await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=existing.organization_id
        )

    assert result.document_ids == []
    assert result.reused_document_ids == set()
    assert result.failed_papers == {
        "2401.00001v1": "content checksum conflicts with another arXiv revision"
    }
    assert write_db._added_items == []
    delete.assert_called_once_with(storage_fields)


async def test_checksum_reuse_without_identity_is_canonicalized() -> None:
    existing = _existing_document("other-paper-v1")
    existing.arxiv_id = None
    existing.document_metadata = {}
    lookup_db = MockAsyncSession()
    write_db = MockAsyncSession()
    write_db.execute = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            MockResult(scalars_result=[]),
            MockResult(scalar_result=existing),
        ]
    )
    sessions = iter([lookup_db, write_db])
    storage_fields = _storage_fields()

    with (
        patch(
            "src.core.database.AsyncSessionLocal", side_effect=lambda: next(sessions)
        ),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch(
            "src.services.arxiv.persistence.store_arxiv_pdf",
            return_value=storage_fields,
        ),
        patch("src.services.arxiv.persistence.delete_arxiv_storage") as delete,
    ):
        result = await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=existing.organization_id
        )

    assert result.document_ids == [str(existing.id)]
    assert result.reused_document_ids == {str(existing.id)}
    assert existing.arxiv_id == "2401.00001v1"
    assert existing.document_metadata["arxiv_id"] == "2401.00001v1"
    delete.assert_called_once_with(storage_fields)


async def test_incomplete_checksum_winner_is_repaired_before_reuse() -> None:
    winner = _existing_document("other-paper-v1", searchable=False)
    winner.arxiv_id = None
    winner.document_metadata = {}
    winner.file_size_bytes = 0
    winner.storage_path = None
    lookup_db = MockAsyncSession()
    write_db = MockAsyncSession()
    write_db.execute = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            MockResult(scalars_result=[]),
            MockResult(scalar_result=winner),
        ]
    )
    sessions = iter([lookup_db, write_db])
    storage_fields = _storage_fields()
    update_vectors = AsyncMock()

    with (
        patch(
            "src.core.database.AsyncSessionLocal", side_effect=lambda: next(sessions)
        ),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch(
            "src.services.arxiv.persistence.store_arxiv_pdf",
            return_value=storage_fields,
        ),
        patch(
            "src.services.search.fulltext_search_service.fulltext_search_service.async_update_document_search_vectors",
            update_vectors,
        ),
        patch("src.services.arxiv.persistence.delete_arxiv_storage") as delete,
    ):
        result = await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=winner.organization_id
        )

    assert result.document_ids == [str(winner.id)]
    assert winner.arxiv_id == "2401.00001v1"
    assert winner.file_size_bytes == 12
    assert winner.storage_path == "key"
    update_vectors.assert_awaited_once_with([str(winner.id)], write_db)
    delete.assert_not_called()


async def test_new_document_is_durable_searchable_and_json_safe() -> None:
    db = MockAsyncSession()
    storage_fields = _storage_fields()
    update_vectors = AsyncMock()

    with (
        patch("src.core.database.AsyncSessionLocal", return_value=db),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch(
            "src.services.arxiv.persistence.store_arxiv_pdf",
            return_value=storage_fields,
        ),
        patch(
            "src.services.search.fulltext_search_service.fulltext_search_service.async_update_document_search_vectors",
            update_vectors,
        ),
    ):
        result = await persist_arxiv_documents(
            [_source()], user_id="user-1", organization_id="org-A"
        )

    assert len(result.document_ids) == 1
    document = db._added_items[0]
    assert document.organization_id == "org-A"
    assert document.uploaded_by_user_id == "user-1"
    assert document.title == "Durable Paper"
    assert document.storage_backend == "s3"
    assert document.document_metadata["publication_date"] == "2024-01-01T00:00:00+00:00"
    update_vectors.assert_awaited_once_with([str(document.id)], db)


async def test_storage_promotion_runs_between_short_database_sessions() -> None:
    lookup_db = MockAsyncSession()
    write_db = MockAsyncSession()
    sessions = iter([lookup_db, write_db])

    def _store(*_args: object) -> dict[str, object]:
        assert lookup_db._closed is True
        assert write_db.execute_calls == []
        return _storage_fields()

    with (
        patch(
            "src.core.database.AsyncSessionLocal", side_effect=lambda: next(sessions)
        ),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch("src.services.arxiv.persistence.store_arxiv_pdf", side_effect=_store),
        patch(
            "src.services.search.fulltext_search_service.fulltext_search_service.async_update_document_search_vectors",
            new_callable=AsyncMock,
        ),
    ):
        await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=uuid4()
        )


async def test_incomplete_legacy_row_is_repaired_in_place() -> None:
    legacy = _existing_document()
    legacy.arxiv_id = None
    legacy.file_path = "s3://bucket/old-key"
    legacy.file_size_bytes = 0
    legacy.storage_backend = "s3"
    legacy.storage_path = "old-key"
    legacy.checksum_sha256 = None
    legacy.search_vector = None
    lookup_db = MockAsyncSession().set_scalars_result([legacy])
    write_db = MockAsyncSession().set_scalar_result(legacy)
    sessions = iter([lookup_db, write_db])
    storage_fields = _storage_fields()
    update_vectors = AsyncMock()

    with (
        patch(
            "src.core.database.AsyncSessionLocal", side_effect=lambda: next(sessions)
        ),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch(
            "src.services.arxiv.persistence.store_arxiv_pdf",
            return_value=storage_fields,
        ) as store,
        patch(
            "src.services.search.fulltext_search_service.fulltext_search_service.async_update_document_search_vectors",
            update_vectors,
        ),
        patch("src.services.arxiv.persistence.delete_arxiv_storage") as delete,
    ):
        result = await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=legacy.organization_id
        )

    assert result.document_ids == [str(legacy.id)]
    assert result.reused_document_ids == {str(legacy.id)}
    assert legacy.arxiv_id == "2401.00001v1"
    assert legacy.storage_backend == "s3"
    assert legacy.storage_path == "key"
    assert legacy.checksum_sha256 == "abc123"
    assert write_db._added_items == []
    assert store.call_args.args[2] != legacy.id
    update_vectors.assert_awaited_once_with([str(legacy.id)], write_db)
    delete.assert_called_once_with(
        {
            "storage_backend": "s3",
            "storage_path": "old-key",
            "file_path": "s3://bucket/old-key",
        }
    )


async def test_missing_search_vector_is_rebuilt_without_storage_upload() -> None:
    existing = _existing_document(searchable=False)
    lookup_db = MockAsyncSession().set_scalars_result([existing])
    write_db = MockAsyncSession().set_scalar_result(existing)
    sessions = iter([lookup_db, write_db])
    update_vectors = AsyncMock()

    with (
        patch(
            "src.core.database.AsyncSessionLocal", side_effect=lambda: next(sessions)
        ),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch("src.services.arxiv.persistence.store_arxiv_pdf") as store,
        patch(
            "src.services.search.fulltext_search_service.fulltext_search_service.async_update_document_search_vectors",
            update_vectors,
        ),
    ):
        result = await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=existing.organization_id
        )

    assert result.document_ids == [str(existing.id)]
    store.assert_not_called()
    update_vectors.assert_awaited_once_with([str(existing.id)], write_db)


async def test_healthy_legacy_row_is_canonicalized_without_reupload() -> None:
    existing = _existing_document()
    existing.arxiv_id = None
    lookup_db = MockAsyncSession().set_scalars_result([existing])
    write_db = MockAsyncSession().set_scalar_result(existing)
    sessions = iter([lookup_db, write_db])

    with (
        patch(
            "src.core.database.AsyncSessionLocal", side_effect=lambda: next(sessions)
        ),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch("src.services.arxiv.persistence.store_arxiv_pdf") as store,
        patch(
            "src.services.search.fulltext_search_service.fulltext_search_service.async_update_document_search_vectors",
            new_callable=AsyncMock,
        ) as update_vectors,
    ):
        result = await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=existing.organization_id
        )

    assert result.document_ids == [str(existing.id)]
    assert existing.arxiv_id == "2401.00001v1"
    store.assert_not_called()
    update_vectors.assert_not_awaited()


async def test_disappeared_repair_target_does_not_abort_batch() -> None:
    existing = _existing_document(searchable=False)
    lookup_db = MockAsyncSession().set_scalars_result([existing])
    write_db = MockAsyncSession()
    write_db.execute = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            MockResult(scalar_result=None),
            MockResult(scalars_result=[]),
        ]
    )
    sessions = iter([lookup_db, write_db])

    with (
        patch(
            "src.core.database.AsyncSessionLocal", side_effect=lambda: next(sessions)
        ),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch("src.services.arxiv.persistence.store_arxiv_pdf") as store,
    ):
        result = await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=existing.organization_id
        )

    assert result.document_ids == []
    assert result.reused_document_ids == set()
    assert result.failed_papers == {
        "2401.00001v1": "document was removed before repair"
    }
    store.assert_not_called()


async def test_one_storage_failure_does_not_lose_other_papers() -> None:
    sources = [_source("good-v1"), _source("bad-v1"), _source("good-v2")]
    lookup_db = MockAsyncSession()
    write_db = MockAsyncSession()
    sessions = iter([lookup_db, write_db])

    def _store(source: SimpleNamespace, *_args: object) -> dict[str, object]:
        arxiv_id = source.document_metadata["arxiv_id"]
        if arxiv_id == "bad-v1":
            raise FileNotFoundError("cached PDF vanished")
        return _storage_fields(checksum=f"checksum-{arxiv_id}")

    with (
        patch(
            "src.core.database.AsyncSessionLocal", side_effect=lambda: next(sessions)
        ),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch("src.services.arxiv.persistence.store_arxiv_pdf", side_effect=_store),
        patch("src.services.arxiv.persistence.delete_arxiv_storage") as delete,
        patch(
            "src.services.search.fulltext_search_service.fulltext_search_service.async_update_document_search_vectors",
            new_callable=AsyncMock,
        ),
    ):
        result = await persist_arxiv_documents(
            sources, user_id=uuid4(), organization_id=uuid4()
        )

    assert len(result.document_ids) == 2
    assert result.failed_papers == {"bad-v1": "durable storage promotion failed"}
    assert [doc.arxiv_id for doc in write_db._added_items] == ["good-v1", "good-v2"]
    delete.assert_not_called()


async def test_all_storage_failures_skip_the_write_session() -> None:
    lookup_db = MockAsyncSession()
    session_factory = MagicMock(return_value=lookup_db)

    with (
        patch("src.core.database.AsyncSessionLocal", session_factory),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch(
            "src.services.arxiv.persistence.store_arxiv_pdf",
            side_effect=FileNotFoundError("no source"),
        ),
    ):
        result = await persist_arxiv_documents(
            [_source("bad-v1"), _source("bad-v2")],
            user_id=uuid4(),
            organization_id=uuid4(),
        )

    assert result.document_ids == []
    assert result.failed_papers == {
        "bad-v1": "durable storage promotion failed",
        "bad-v2": "durable storage promotion failed",
    }
    assert session_factory.call_count == 1


async def test_partial_promotion_then_db_failure_cleans_only_successes() -> None:
    sources = [_source("good-v1"), _source("bad-v1"), _source("good-v2")]
    db = MockAsyncSession()
    db.flush = AsyncMock(side_effect=RuntimeError("database unavailable"))  # type: ignore[method-assign]
    good_storage = {
        "good-v1": _storage_fields("checksum-good-v1"),
        "good-v2": _storage_fields("checksum-good-v2"),
    }

    def _store(source: SimpleNamespace, *_args: object) -> dict[str, object]:
        arxiv_id = source.document_metadata["arxiv_id"]
        if arxiv_id == "bad-v1":
            raise FileNotFoundError("cached PDF vanished")
        return good_storage[arxiv_id]

    with (
        patch("src.core.database.AsyncSessionLocal", return_value=db),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch("src.services.arxiv.persistence.store_arxiv_pdf", side_effect=_store),
        patch("src.services.arxiv.persistence.delete_arxiv_storage") as delete,
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        await persist_arxiv_documents(sources, user_id=uuid4(), organization_id=uuid4())

    assert {call.args[0]["checksum_sha256"] for call in delete.call_args_list} == {
        "checksum-good-v1",
        "checksum-good-v2",
    }


async def test_transaction_failure_compensates_promoted_storage() -> None:
    db = MockAsyncSession()
    db.flush = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("database unavailable")
    )
    storage_fields = _storage_fields()

    with (
        patch("src.core.database.AsyncSessionLocal", return_value=db),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch(
            "src.services.arxiv.persistence.store_arxiv_pdf",
            return_value=storage_fields,
        ),
        patch("src.services.arxiv.persistence.delete_arxiv_storage") as delete,
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=uuid4()
        )

    delete.assert_called_once_with(storage_fields)


async def test_cancellation_during_storage_promotion_compensates_completed_upload() -> (
    None
):
    lookup_db = MockAsyncSession().set_scalars_result([])
    sessions = iter([lookup_db])
    started = Event()
    release = Event()
    storage_fields = _storage_fields()

    def _store(*_args: object) -> dict[str, object]:
        started.set()
        release.wait(timeout=1)
        return storage_fields

    with (
        patch(
            "src.core.database.AsyncSessionLocal", side_effect=lambda: next(sessions)
        ),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch("src.services.arxiv.persistence.store_arxiv_pdf", side_effect=_store),
        patch("src.services.arxiv.persistence.delete_arxiv_storage") as delete,
    ):
        task = asyncio.create_task(
            persist_arxiv_documents(
                [_source()], user_id=uuid4(), organization_id=uuid4()
            )
        )
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    delete.assert_called_once_with(storage_fields)


async def test_search_vector_failure_rolls_back_and_compensates_storage() -> None:
    db = MockAsyncSession()
    storage_fields = _storage_fields()

    with (
        patch("src.core.database.AsyncSessionLocal", return_value=db),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch(
            "src.services.arxiv.persistence.store_arxiv_pdf",
            return_value=storage_fields,
        ),
        patch("src.services.arxiv.persistence.delete_arxiv_storage") as delete,
        patch(
            "src.services.search.fulltext_search_service.fulltext_search_service.async_update_document_search_vectors",
            new_callable=AsyncMock,
            side_effect=RuntimeError("search unavailable"),
        ),
        pytest.raises(RuntimeError, match="search unavailable"),
    ):
        await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=uuid4()
        )

    delete.assert_called_once_with(storage_fields)


async def test_concurrent_checksum_loser_reuses_winner_and_cleans_storage() -> None:
    winner = _existing_document("other-paper-v1")
    winner.arxiv_id = None
    winner.document_metadata = {}
    db = MockAsyncSession()
    db.execute = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            MockResult(scalars_result=[]),
            MockResult(scalars_result=[]),
            MockResult(scalar_result=None),
            MockResult(scalar_result=None),
            MockResult(scalar_result=winner),
        ]
    )
    db.flush = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            IntegrityError(
                "INSERT INTO documents",
                {},
                Exception("uq_documents_org_checksum_live"),
            ),
            None,
        ]
    )
    storage_fields = _storage_fields()

    with (
        patch("src.core.database.AsyncSessionLocal", return_value=db),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch(
            "src.services.arxiv.persistence.store_arxiv_pdf",
            return_value=storage_fields,
        ),
        patch("src.services.arxiv.persistence.delete_arxiv_storage") as delete,
    ):
        result = await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=winner.organization_id
        )

    assert result.document_ids == [str(winner.id)]
    assert result.reused_document_ids == {str(winner.id)}
    assert winner.arxiv_id == "2401.00001v1"
    delete.assert_called_once_with(storage_fields)


async def test_concurrent_exact_revision_loser_reuses_unique_winner() -> None:
    winner = _existing_document()
    db = MockAsyncSession()
    db.execute = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            MockResult(scalars_result=[]),
            MockResult(scalars_result=[]),
            MockResult(scalar_result=None),
            MockResult(scalar_result=winner),
        ]
    )
    db.flush = AsyncMock(  # type: ignore[method-assign]
        side_effect=IntegrityError(
            "INSERT INTO documents",
            {},
            Exception("uq_documents_org_arxiv_id_live"),
        )
    )
    storage_fields = _storage_fields(checksum="different-bytes")

    with (
        patch("src.core.database.AsyncSessionLocal", return_value=db),
        patch.object(settings, "DO_KB_ENABLED", False),
        patch(
            "src.services.arxiv.persistence.store_arxiv_pdf",
            return_value=storage_fields,
        ),
        patch("src.services.arxiv.persistence.delete_arxiv_storage") as delete,
    ):
        result = await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=winner.organization_id
        )

    assert result.document_ids == [str(winner.id)]
    assert result.reused_document_ids == {str(winner.id)}
    delete.assert_called_once_with(storage_fields)


@pytest.mark.parametrize(
    ("sync_result", "expected_status", "expected_failed"),
    [("data-source-1", "completed", False), (None, "failed", True)],
)
async def test_kb_sync_outcome_is_persisted(
    sync_result: str | None, expected_status: str, expected_failed: bool
) -> None:
    db = MockAsyncSession()
    kb_db = MagicMock()
    kb_db.__aenter__ = AsyncMock(return_value=kb_db)
    kb_db.__aexit__ = AsyncMock(return_value=False)
    kb_db.execute = AsyncMock(
        side_effect=lambda *_args: MockResult(scalars_result=list(db._added_items))
    )
    kb_db.commit = AsyncMock()
    sessions = iter([db, db, kb_db])

    with (
        patch(
            "src.core.database.AsyncSessionLocal", side_effect=lambda: next(sessions)
        ),
        patch.object(settings, "DO_KB_ENABLED", True),
        patch(
            "src.services.arxiv.persistence.store_arxiv_pdf",
            return_value=_storage_fields(),
        ),
        patch(
            "src.services.search.fulltext_search_service.fulltext_search_service.async_update_document_search_vectors",
            new_callable=AsyncMock,
        ),
        patch(
            "src.services.do_kb.sync_documents_to_kb",
            new_callable=AsyncMock,
            return_value=[sync_result],
        ),
    ):
        result = await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=uuid4()
        )

    assert db._added_items[0].do_kb_sync_status == expected_status
    assert result.kb_sync_failed is expected_failed
    assert kb_db.commit.await_count == 2


async def test_kb_exception_marks_document_reconcilable() -> None:
    db = MockAsyncSession()
    kb_db = MagicMock()
    kb_db.__aenter__ = AsyncMock(return_value=kb_db)
    kb_db.__aexit__ = AsyncMock(return_value=False)
    kb_db.execute = AsyncMock(
        side_effect=lambda *_args: MockResult(scalars_result=list(db._added_items))
    )
    kb_db.commit = AsyncMock()
    sessions = iter([db, db, kb_db])

    with (
        patch(
            "src.core.database.AsyncSessionLocal", side_effect=lambda: next(sessions)
        ),
        patch.object(settings, "DO_KB_ENABLED", True),
        patch(
            "src.services.arxiv.persistence.store_arxiv_pdf",
            return_value=_storage_fields(),
        ),
        patch(
            "src.services.search.fulltext_search_service.fulltext_search_service.async_update_document_search_vectors",
            new_callable=AsyncMock,
        ),
        patch(
            "src.services.do_kb.sync_documents_to_kb",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DO unavailable"),
        ),
    ):
        result = await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=uuid4()
        )

    assert db._added_items[0].do_kb_sync_status == "failed"
    assert result.kb_sync_failed is True
    assert kb_db.commit.await_count == 2


async def test_kb_phase_does_not_merge_stale_reused_snapshot() -> None:
    existing = _existing_document()
    existing.do_kb_data_source_uuid = None
    lookup_db = MockAsyncSession().set_scalars_result([existing])
    kb_db = MagicMock()
    kb_db.__aenter__ = AsyncMock(return_value=kb_db)
    kb_db.__aexit__ = AsyncMock(return_value=False)
    kb_db.execute = AsyncMock(return_value=MockResult(scalars_result=[]))
    kb_db.commit = AsyncMock()
    sessions = iter([lookup_db, kb_db])
    sync = AsyncMock(return_value=[])

    with (
        patch(
            "src.core.database.AsyncSessionLocal", side_effect=lambda: next(sessions)
        ),
        patch.object(settings, "DO_KB_ENABLED", True),
        patch("src.services.do_kb.sync_documents_to_kb", sync),
    ):
        result = await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=existing.organization_id
        )

    assert result.document_ids == [str(existing.id)]
    kb_db.merge.assert_not_called()
    sync.assert_awaited_once_with(kb_db, [])


async def test_reused_kb_handle_without_completed_status_is_reconciled() -> None:
    existing = _existing_document()
    existing.do_kb_sync_status = None
    lookup_db = MockAsyncSession().set_scalars_result([existing])
    kb_db = MagicMock()
    kb_db.__aenter__ = AsyncMock(return_value=kb_db)
    kb_db.__aexit__ = AsyncMock(return_value=False)
    kb_db.execute = AsyncMock(return_value=MockResult(scalars_result=[existing]))
    kb_db.commit = AsyncMock()
    sessions = iter([lookup_db, kb_db])
    sync = AsyncMock(return_value=[existing.do_kb_data_source_uuid])

    with (
        patch(
            "src.core.database.AsyncSessionLocal", side_effect=lambda: next(sessions)
        ),
        patch.object(settings, "DO_KB_ENABLED", True),
        patch("src.services.do_kb.sync_documents_to_kb", sync),
    ):
        result = await persist_arxiv_documents(
            [_source()], user_id=uuid4(), organization_id=existing.organization_id
        )

    assert result.kb_sync_failed is False
    assert existing.do_kb_sync_status == "completed"
    sync.assert_awaited_once_with(kb_db, [existing])
