"""Unit tests for the DO KB sync bridge used by the ingestion Celery tasks.

After Qdrant was dropped, ``process_document_ingestion`` and
``generate_embeddings`` must push documents to DO KB instead. The bridge
``_sync_document_to_kb_blocking`` runs the async ``sync_document_to_kb`` from a
synchronous Celery task and must:

- return the data-source uuid on success,
- never raise on a KB outage (ingestion must not fail),
- return None cleanly when DO KB is disabled,
- sync a freshly loaded, org-scoped copy of the row, never ``merge()`` the
  caller's stale snapshot (that wrote stale columns back; GOO-358).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tasks.processing_tasks import _sync_document_to_kb_blocking


class _FakeAsyncSession:
    """Stand-in for an AsyncSessionLocal session."""

    def __init__(self) -> None:
        self.current = None
        self.got = None
        self.commits = 0

    async def get(self, model, ident):
        self.got = (model, ident)
        return self.current

    async def merge(self, document):  # pragma: no cover - must not be used
        raise AssertionError("must not merge the caller's stale snapshot")

    async def commit(self):
        self.commits += 1


class _FakeAsyncCtx:
    def __init__(self, session: _FakeAsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeAsyncSession:
        return self._session

    async def __aexit__(self, *exc) -> bool:
        return False


@pytest.fixture
def fake_session():
    return _FakeAsyncSession()


@pytest.fixture
def patched_async_session(fake_session):
    # Local import inside the helper resolves AsyncSessionLocal from core.database.
    with patch(
        "src.core.database.AsyncSessionLocal",
        return_value=_FakeAsyncCtx(fake_session),
    ):
        yield fake_session


@pytest.mark.unit
def test_returns_data_source_uuid_on_success(patched_async_session):
    doc = MagicMock(id="doc-1", organization_id="org-1")
    current = MagicMock(id="doc-1", organization_id="org-1")
    patched_async_session.current = current
    with patch(
        "src.services.do_kb.sync_document_to_kb",
        new=AsyncMock(return_value="ds-1"),
    ) as sync_mock:
        result = _sync_document_to_kb_blocking(doc)

    assert result == "ds-1"
    # Synced the freshly loaded row, not the caller's snapshot.
    from src.models.document import Document

    assert patched_async_session.got == (Document, "doc-1")
    assert sync_mock.await_args.args[1] is current
    assert patched_async_session.commits == 1


@pytest.mark.unit
def test_foreign_or_missing_row_is_not_synced(patched_async_session):
    doc = MagicMock(id="doc-1", organization_id="org-1")
    patched_async_session.current = MagicMock(id="doc-1", organization_id="org-2")
    with patch(
        "src.services.do_kb.sync_document_to_kb",
        new=AsyncMock(return_value="ds-1"),
    ) as sync_mock:
        assert _sync_document_to_kb_blocking(doc) is None
        patched_async_session.current = None
        assert _sync_document_to_kb_blocking(doc) is None
    sync_mock.assert_not_awaited()


@pytest.mark.unit
def test_returns_none_when_kb_disabled(patched_async_session):
    # sync_document_to_kb returns None when DO_KB_ENABLED is false.
    doc = MagicMock(id="doc-1")
    patched_async_session.current = doc
    with patch(
        "src.services.do_kb.sync_document_to_kb",
        new=AsyncMock(return_value=None),
    ):
        result = _sync_document_to_kb_blocking(doc)

    assert result is None


@pytest.mark.unit
def test_failure_is_isolated_returns_none(patched_async_session, caplog):
    # A KB outage must not propagate out of the ingestion task.
    doc = MagicMock(id="doc-1")
    patched_async_session.current = doc
    with patch(
        "src.services.do_kb.sync_document_to_kb",
        new=AsyncMock(side_effect=RuntimeError("KB down")),
    ):
        result = _sync_document_to_kb_blocking(doc)

    assert result is None
    assert any("DO KB sync failed" in r.message for r in caplog.records)
