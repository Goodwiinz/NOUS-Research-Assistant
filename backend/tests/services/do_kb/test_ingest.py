"""Unit tests for sync_document_to_kb dual-write helper.

Failure isolation is the load-bearing property here: every external call
can fail and the helper must still return None gracefully without raising.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.do_kb.client import DOKnowledgeBaseError
from src.services.do_kb.models import DataSource, IndexingJob


class _FakeDoc:
    def __init__(
        self,
        *,
        doc_id: str = "doc-1",
        org_id: str = "org-1",
        storage_backend: str = "s3",
        storage_path: str = "documents/doc-1.pdf",
        content_text: str | None = None,
    ) -> None:
        self.id = doc_id
        self.organization_id = org_id
        self.storage_backend = storage_backend
        self.storage_path = storage_path
        self.content_text = content_text
        self.do_kb_data_source_uuid: str | None = None
        self.do_kb_indexed_at: datetime | None = None
        self.do_kb_index_status: str | None = None
        self.is_deleted = False


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.refresh_calls: list[tuple[Any, tuple]] = []

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: Any, attribute_names: tuple = ()) -> None:
        self.refresh_calls.append((obj, attribute_names))


@pytest.fixture
def stub_settings(monkeypatch):
    from src.services.do_kb import ingest as ingest_module

    fake = MagicMock()
    fake.DO_KB_ENABLED = True
    fake.S3_BUCKET_NAME = "test-bucket"
    monkeypatch.setattr(ingest_module, "settings", fake)
    return fake


@pytest.mark.unit
@pytest.mark.asyncio
async def test_returns_none_when_disabled(monkeypatch):
    from src.services.do_kb import ingest as ingest_module
    from src.services.do_kb.ingest import sync_document_to_kb

    fake = MagicMock()
    fake.DO_KB_ENABLED = False
    monkeypatch.setattr(ingest_module, "settings", fake)

    result = await sync_document_to_kb(_FakeSession(), _FakeDoc())
    assert result is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_returns_existing_uuid_when_already_synced(stub_settings):
    from src.services.do_kb.ingest import sync_document_to_kb

    doc = _FakeDoc()
    doc.do_kb_data_source_uuid = "ds-existing"

    client = MagicMock()
    result = await sync_document_to_kb(_FakeSession(), doc, client=client)

    assert result == "ds-existing"
    client.add_spaces_data_source.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skips_when_cached_document_already_deleted(stub_settings):
    """Cheap defense-in-depth check: a caller that forgot the is_deleted filter
    still can't push a deleted doc's content into DO KB."""
    from src.services.do_kb.ingest import sync_document_to_kb

    session = _FakeSession()
    doc = _FakeDoc()
    doc.is_deleted = True

    client = MagicMock()
    result = await sync_document_to_kb(session, doc, client=client)

    assert result is None
    client.add_spaces_data_source.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skips_when_refresh_reveals_delete_since_batch_load(stub_settings):
    """Codex P1: the Document instance may have been loaded by a batch query
    some time before this call runs; a delete committed in between must be
    caught by refreshing from the DB, not by trusting the cached instance."""
    from src.services.do_kb.ingest import sync_document_to_kb

    class _RefreshingSession(_FakeSession):
        async def refresh(self, obj: Any, attribute_names: tuple = ()) -> None:
            await super().refresh(obj, attribute_names)
            # Simulate: another transaction soft-deleted the doc after it was
            # loaded into this batch but before this refresh.
            obj.is_deleted = True

    session = _RefreshingSession()
    doc = _FakeDoc()
    doc.is_deleted = False  # stale cached value

    client = MagicMock()
    result = await sync_document_to_kb(session, doc, client=client)

    assert result is None
    assert session.refresh_calls == [(doc, ["is_deleted"])]
    client.add_spaces_data_source.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_happy_path_uses_s3_source(stub_settings):
    from src.services.do_kb.ingest import sync_document_to_kb

    session = _FakeSession()
    doc = _FakeDoc()

    client = MagicMock()
    client.add_spaces_data_source = AsyncMock(return_value=DataSource(uuid="ds-fresh"))
    client.start_indexing = AsyncMock(
        return_value=IndexingJob(uuid="job-1", status="PENDING")
    )

    with patch(
        "src.services.do_kb.ingest.ensure_kb_for_org",
        AsyncMock(return_value="kb-1"),
    ):
        result = await sync_document_to_kb(session, doc, client=client)

    assert result == "ds-fresh"
    assert doc.do_kb_data_source_uuid == "ds-fresh"
    assert doc.do_kb_indexed_at is not None
    assert doc.do_kb_indexed_at.tzinfo == timezone.utc
    client.add_spaces_data_source.assert_awaited_once_with(
        kb_uuid="kb-1", bucket="test-bucket", key="documents/doc-1.pdf"
    )
    client.start_indexing.assert_awaited_once_with(kb_uuid="kb-1")
    # The adapter stages changes; the caller owns the transaction commit.
    assert session.commits == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_falls_back_to_text_upload_when_no_storage_path(stub_settings):
    from src.services.do_kb.ingest import sync_document_to_kb

    session = _FakeSession()
    doc = _FakeDoc(storage_backend="local", storage_path=None, content_text="hello")

    client = MagicMock()
    client.add_spaces_data_source = AsyncMock(return_value=DataSource(uuid="ds-text"))
    client.start_indexing = AsyncMock(return_value=IndexingJob(uuid="job-1"))

    helper = MagicMock()
    helper.bucket = "test-bucket"
    helper.upload_file = MagicMock(return_value="ok")

    with (
        patch(
            "src.services.do_kb.ingest.ensure_kb_for_org",
            AsyncMock(return_value="kb-1"),
        ),
        patch("src.core.s3_client.S3StorageHelper", return_value=helper),
    ):
        result = await sync_document_to_kb(session, doc, client=client)

    assert result == "ds-text"
    helper.upload_file.assert_called_once()
    args, kwargs = client.add_spaces_data_source.call_args
    assert kwargs["bucket"] == "test-bucket"
    assert kwargs["key"] == f"documents/{doc.organization_id}/{doc.id}.txt"
    # The canonical key is deterministic from the doc id; we must NOT clobber the
    # document's real storage pointer (it still references the original upload).
    assert doc.storage_backend == "local"
    assert doc.storage_path is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skips_when_no_source_available(stub_settings):
    from src.services.do_kb.ingest import sync_document_to_kb

    session = _FakeSession()
    doc = _FakeDoc(storage_backend="local", storage_path=None, content_text=None)

    client = MagicMock()
    client.add_spaces_data_source = AsyncMock()

    with patch(
        "src.services.do_kb.ingest.ensure_kb_for_org",
        AsyncMock(return_value="kb-1"),
    ):
        result = await sync_document_to_kb(session, doc, client=client)

    assert result is None
    client.add_spaces_data_source.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provisioning_failure_is_swallowed(stub_settings):
    from src.services.do_kb.ingest import sync_document_to_kb

    session = _FakeSession()
    doc = _FakeDoc()

    client = MagicMock()
    client.add_spaces_data_source = AsyncMock()

    with patch(
        "src.services.do_kb.ingest.ensure_kb_for_org",
        AsyncMock(side_effect=DOKnowledgeBaseError("boom")),
    ):
        result = await sync_document_to_kb(session, doc, client=client)

    assert result is None
    client.add_spaces_data_source.assert_not_called()
    # Document untouched
    assert doc.do_kb_data_source_uuid is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_data_source_failure_is_swallowed(stub_settings):
    from src.services.do_kb.ingest import sync_document_to_kb

    session = _FakeSession()
    doc = _FakeDoc()

    client = MagicMock()
    client.add_spaces_data_source = AsyncMock(
        side_effect=DOKnowledgeBaseError("api 500")
    )
    client.start_indexing = AsyncMock()

    with patch(
        "src.services.do_kb.ingest.ensure_kb_for_org",
        AsyncMock(return_value="kb-1"),
    ):
        result = await sync_document_to_kb(session, doc, client=client)

    assert result is None
    client.start_indexing.assert_not_called()
    assert session.commits == 0
    assert doc.do_kb_data_source_uuid is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_indexing_kick_failure_does_not_fail_sync(stub_settings):
    from src.services.do_kb.ingest import sync_document_to_kb

    session = _FakeSession()
    doc = _FakeDoc()

    client = MagicMock()
    client.add_spaces_data_source = AsyncMock(return_value=DataSource(uuid="ds-1"))
    client.start_indexing = AsyncMock(side_effect=DOKnowledgeBaseError("queue full"))

    with patch(
        "src.services.do_kb.ingest.ensure_kb_for_org",
        AsyncMock(return_value="kb-1"),
    ):
        result = await sync_document_to_kb(session, doc, client=client)

    # Sync still succeeds — indexing is fire-and-forget.
    assert result == "ds-1"
    assert doc.do_kb_data_source_uuid == "ds-1"
    assert session.commits == 0
    # FIX C2: never claim "indexed" — the kick failed, so the docs are NOT
    # queryable. The truthful status is "registered" (data source added only).
    assert doc.do_kb_index_status != "indexed"
    assert doc.do_kb_index_status == "registered"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_canonical_text_preferred_over_s3_when_text_present(stub_settings):
    """Even for an s3-backed doc, prefer the canonical text object in the KB
    bucket so it lands under the documents/{org}/ prefix the data source reads."""
    from src.services.do_kb.ingest import sync_document_to_kb

    session = _FakeSession()
    doc = _FakeDoc(
        storage_backend="s3",
        storage_path="uploads/elsewhere/doc-1.pdf",
        content_text="hello",
    )

    client = MagicMock()
    client.add_spaces_data_source = AsyncMock(return_value=DataSource(uuid="ds-x"))
    client.start_indexing = AsyncMock(return_value=IndexingJob(uuid="job-1"))

    helper = MagicMock()
    helper.bucket = "test-bucket"
    helper.upload_file = MagicMock(return_value="ok")

    with (
        patch(
            "src.services.do_kb.ingest.ensure_kb_for_org",
            AsyncMock(return_value="kb-1"),
        ),
        patch("src.core.s3_client.S3StorageHelper", return_value=helper),
    ):
        result = await sync_document_to_kb(session, doc, client=client)

    assert result == "ds-x"
    helper.upload_file.assert_called_once()
    _, kwargs = client.add_spaces_data_source.call_args
    # Canonical .txt key in the KB bucket, NOT the original uploads/... path.
    assert kwargs["key"] == f"documents/{doc.organization_id}/{doc.id}.txt"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_kicks_indexing_via_org_id(stub_settings):
    """A1 regression: bulk must resolve the KB via organization_id (a column) and
    actually kick indexing — the old code keyed off a lazy `doc.organization`
    relationship that is None in async context, so indexing never ran."""
    from src.services.do_kb.ingest import sync_documents_to_kb

    session = _FakeSession()
    docs = [_FakeDoc(doc_id="doc-1"), _FakeDoc(doc_id="doc-2")]  # same org-1

    client = MagicMock()
    client.add_spaces_data_source = AsyncMock(
        side_effect=[DataSource(uuid="ds-1"), DataSource(uuid="ds-2")]
    )
    client.start_indexing = AsyncMock(return_value=IndexingJob(uuid="job-1"))

    with patch(
        "src.services.do_kb.ingest.ensure_kb_for_org",
        AsyncMock(return_value="kb-1"),
    ):
        results = await sync_documents_to_kb(session, docs, client=client)

    assert results == ["ds-1", "ds-2"]
    # Indexing kicked exactly once for the single org, via the resolved kb_uuid.
    client.start_indexing.assert_awaited_once_with(kb_uuid="kb-1")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reuses_existing_data_source_for_same_item_path(stub_settings):
    """A7: when a data source for the canonical key already exists, reuse its uuid
    instead of adding a duplicate."""
    from src.services.do_kb.ingest import sync_document_to_kb

    session = _FakeSession()
    doc = _FakeDoc(storage_backend="local", storage_path=None, content_text="hi")
    key = f"documents/{doc.organization_id}/{doc.id}.txt"

    client = MagicMock()
    client.find_data_source = AsyncMock(return_value="ds-existing")
    client.add_spaces_data_source = AsyncMock()
    client.start_indexing = AsyncMock(return_value=IndexingJob(uuid="job-1"))

    helper = MagicMock()
    helper.bucket = "test-bucket"
    helper.upload_file = MagicMock(return_value="ok")

    with (
        patch(
            "src.services.do_kb.ingest.ensure_kb_for_org",
            AsyncMock(return_value="kb-1"),
        ),
        patch("src.core.s3_client.S3StorageHelper", return_value=helper),
    ):
        result = await sync_document_to_kb(session, doc, client=client)

    assert result == "ds-existing"
    client.find_data_source.assert_awaited_once_with(kb_uuid="kb-1", item_path=key)
    client.add_spaces_data_source.assert_not_called()
    assert doc.do_kb_data_source_uuid == "ds-existing"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsync_deletes_data_source_and_clears_columns(stub_settings):
    """unsync_document_from_kb deletes the DS from DO KB and clears DB columns."""
    from src.services.do_kb.ingest import unsync_document_from_kb

    session = _FakeSession()
    doc = _FakeDoc()
    doc.do_kb_data_source_uuid = "ds-old"
    doc.do_kb_indexed_at = datetime.now(timezone.utc)
    doc.do_kb_index_status = "indexed"

    client = MagicMock()
    client.delete_data_source = AsyncMock(return_value=None)

    with patch(
        "src.services.do_kb.ingest.ensure_kb_for_org",
        AsyncMock(return_value="kb-1"),
    ):
        result = await unsync_document_from_kb(session, doc, client=client)

    assert result is True
    client.delete_data_source.assert_awaited_once_with(kb_uuid="kb-1", ds_uuid="ds-old")
    assert doc.do_kb_data_source_uuid is None
    assert doc.do_kb_indexed_at is None
    assert doc.do_kb_index_status is None
    assert session.commits == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsync_noop_when_no_data_source(stub_settings):
    """No data source UUID → nothing to delete, no DO KB call, returns True."""
    from src.services.do_kb.ingest import unsync_document_from_kb

    doc = _FakeDoc()  # do_kb_data_source_uuid is None
    client = MagicMock()
    client.delete_data_source = AsyncMock()

    result = await unsync_document_from_kb(_FakeSession(), doc, client=client)

    assert result is True
    client.delete_data_source.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsync_clears_columns_when_data_source_is_already_absent(stub_settings):
    """A confirmed 404 is a successful idempotent delete."""
    from src.services.do_kb.ingest import unsync_document_from_kb

    session = _FakeSession()
    doc = _FakeDoc()
    doc.do_kb_data_source_uuid = "ds-old"

    client = MagicMock()
    client.delete_data_source = AsyncMock(
        side_effect=DOKnowledgeBaseError("gone", status_code=404)
    )

    with patch(
        "src.services.do_kb.ingest.ensure_kb_for_org",
        AsyncMock(return_value="kb-1"),
    ):
        result = await unsync_document_from_kb(session, doc, client=client)

    assert result is True
    assert doc.do_kb_data_source_uuid is None
    assert session.commits == 1


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        DOKnowledgeBaseError("upstream down", status_code=503),
        RuntimeError("connection reset"),
    ],
)
async def test_unsync_retains_columns_when_delete_fails(stub_settings, error):
    """Unconfirmed deletes retain the retry handle and index metadata."""
    from src.services.do_kb.ingest import unsync_document_from_kb

    session = _FakeSession()
    doc = _FakeDoc()
    indexed_at = datetime.now(timezone.utc)
    doc.do_kb_data_source_uuid = "ds-old"
    doc.do_kb_indexed_at = indexed_at
    doc.do_kb_index_status = "indexed"

    client = MagicMock()
    client.delete_data_source = AsyncMock(side_effect=error)

    with patch(
        "src.services.do_kb.ingest.ensure_kb_for_org",
        AsyncMock(return_value="kb-1"),
    ):
        result = await unsync_document_from_kb(session, doc, client=client)

    assert result is False
    assert doc.do_kb_data_source_uuid == "ds-old"
    assert doc.do_kb_indexed_at == indexed_at
    assert doc.do_kb_index_status == "indexed"
    assert session.commits == 0


@pytest.mark.unit
def test_record_metric_routes_to_increment_counter():
    """Regression: _record_metric used to read a module attribute
    (agent_do_kb_ingest_total) that is defined nowhere, so every ingest-outcome
    metric was a silent no-op. It must now route through the registered
    increment_counter, like the RAG read path does."""
    from src.services.do_kb.ingest import _record_metric

    with patch("src.observability.metrics.increment_counter") as inc:
        _record_metric("ok")

    inc.assert_called_once_with("do_kb_ingest_total", attributes={"status": "ok"})


@pytest.mark.unit
def test_record_metric_swallows_observability_errors():
    """Observability is best-effort — a metrics failure must never break ingest."""
    from src.services.do_kb.ingest import _record_metric

    with patch(
        "src.observability.metrics.increment_counter",
        side_effect=RuntimeError("meter down"),
    ):
        _record_metric("provision_failed")  # must not raise
