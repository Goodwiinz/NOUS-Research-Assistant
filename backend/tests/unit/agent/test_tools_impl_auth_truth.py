"""Auth-guard and result-truthfulness regressions in ``tools_impl.py``.

Four independent findings, one shared theme: a tool payload must not claim
more than actually happened.

* H1 — ``_tool_ingest_arxiv`` was the only tool lacking the standard
  ``if not db or not current_user`` guard, so an unresolved ``current_user``
  (tools.py's ``_tool_context`` yields ``None`` rather than raising) drove the
  full arXiv download+extract+ingest pipeline unauthenticated, and its
  no-current_user fallback fabricated ``document_ids`` via
  ``getattr(doc, "id", None)`` from objects that were never persisted.
* M6 — ``_tool_search_external_database`` used to swallow a total connector
  outage into a success-shaped ``{"total_results": 0}`` with no ``"error"``
  key, the exact key ``_nodes_tools`` / ``tool_dedupe`` classify on.
* L5 — a paper's earlier "metadata fetch failed" marking survived even after
  it landed (via the stub-paper fallback) and persisted cleanly, so a fully
  successful ingest could still list it under ``failed_papers``.
* L6 — ``list_project_documents`` returned the raw db ``processing_status``
  ("completed") instead of the public vocabulary ``search_documents`` uses
  (``ApiDocumentStatus.from_db`` -> "indexed"), so the same document reported
  two different statuses depending on which tool was called.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional, cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from src.models.user import User
from src.services.agent import tools_impl
from src.services.connectors.base import (
    ConnectorCapability,
    ConnectorDomain,
    ConnectorInfo,
    ConnectorResult,
    ExternalDBConnector,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _mock_user(org_id: Optional[Any] = None) -> Any:
    user = Mock()
    user.id = uuid4()
    user.organization_id = org_id or uuid4()
    return user


def _mock_service_ctx(mock_service: AsyncMock) -> AsyncMock:
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_service)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# H1 — _tool_ingest_arxiv auth guard
# ---------------------------------------------------------------------------


class TestIngestArxivAuthGuard:
    async def test_unauthenticated_call_never_touches_the_service(self) -> None:
        """No db/current_user must short-circuit before ArXivIngestionService
        is even instantiated — mirrors every sibling tool's fail-closed guard.
        """
        service_ctor = Mock(
            side_effect=AssertionError(
                "ArXivIngestionService must not be instantiated for an "
                "unauthenticated _tool_ingest_arxiv call"
            )
        )
        with patch(
            "src.services.arxiv.arxiv_service.ArXivIngestionService",
            service_ctor,
        ):
            result = await tools_impl._tool_ingest_arxiv(
                {"paper_ids": ["2401.12345"]},
                "",
                db=None,
                current_user=None,
            )

        assert result.get("error") == "Authentication required"
        service_ctor.assert_not_called()

    async def test_unauthenticated_call_does_not_fabricate_document_ids(
        self,
    ) -> None:
        """Pre-fix, the ``elif ingested:`` fallback returned
        ``str(getattr(doc, "id", None))`` for objects the DB never saw —
        phantom ids that fail whatever downstream tool receives them.
        """
        phantom_doc = Mock()
        phantom_doc.id = uuid4()
        phantom_doc.document_metadata = {"arxiv_id": "2401.12345"}

        mock_service = AsyncMock()
        mock_service.get_papers_by_ids = AsyncMock(
            return_value=[{"id": "2401.12345", "title": "Phantom"}]
        )
        mock_service.ingest_papers = AsyncMock(return_value=[phantom_doc])

        with patch(
            "src.services.arxiv.arxiv_service.ArXivIngestionService",
            Mock(return_value=_mock_service_ctx(mock_service)),
        ):
            result = await tools_impl._tool_ingest_arxiv(
                {"paper_ids": ["2401.12345"]},
                "",
                db=None,
                current_user=None,
            )

        assert result.get("error") == "Authentication required"
        assert str(phantom_doc.id) not in result.get("document_ids", [])
        mock_service.ingest_papers.assert_not_called()

    async def test_authenticated_call_still_reaches_the_service(self) -> None:
        """Control: the new guard must not block a legitimate caller."""
        user = _mock_user()
        mock_service = AsyncMock()
        mock_service.get_papers_by_ids = AsyncMock(return_value=[])
        mock_service.ingest_papers = AsyncMock(return_value=[])
        service_ctor = Mock(return_value=_mock_service_ctx(mock_service))

        with patch(
            "src.services.arxiv.arxiv_service.ArXivIngestionService",
            service_ctor,
        ):
            result = await tools_impl._tool_ingest_arxiv(
                {"paper_ids": ["2401.12345"]},
                str(user.id),
                db=AsyncMock(),
                current_user=user,
            )

        service_ctor.assert_called_once()
        assert result.get("error") != "Authentication required"


# ---------------------------------------------------------------------------
# M6 — _tool_search_external_database total-failure detection
# ---------------------------------------------------------------------------


def _connector_info(name: str) -> ConnectorInfo:
    return ConnectorInfo(
        name=name,
        display_name=name,
        description="test connector",
        domains=[ConnectorDomain.GENERAL],
        capabilities=[ConnectorCapability.SEARCH],
        base_url="https://example.invalid",
    )


class _OkConn(ExternalDBConnector):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def info(self) -> ConnectorInfo:
        return _connector_info(self._name)

    async def search(
        self,
        query: str,
        max_results: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ConnectorResult]:
        del max_results, filters
        return [
            ConnectorResult(
                id="ok-1", title=query, source=self._name, url="https://example.invalid"
            )
        ]


class _FailConn(ExternalDBConnector):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def info(self) -> ConnectorInfo:
        return _connector_info(self._name)

    async def search(
        self,
        query: str,
        max_results: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ConnectorResult]:
        del query, max_results, filters
        raise RuntimeError(f"{self._name} upstream failure")


class TestSearchExternalDatabaseAllFail:
    async def test_all_connectors_failing_returns_error(self) -> None:
        from src.services.connectors import connector_registry

        connector_registry.register(_FailConn("m6_fail_a"))
        connector_registry.register(_FailConn("m6_fail_b"))
        try:
            result = await tools_impl._tool_search_external_database(
                {"query": "q", "domain": "general"}
            )
        finally:
            connector_registry._connectors.pop("m6_fail_a", None)
            connector_registry._connectors.pop("m6_fail_b", None)

        assert "error" in result, (
            "total connector failure must not look like a successful empty "
            "search — _nodes_tools/tool_dedupe key off the top-level 'error'"
        )

    async def test_partial_failure_still_returns_results_no_error(self) -> None:
        """Control: one connector failing must not poison the others' results."""
        from src.services.connectors import connector_registry

        connector_registry.register(_OkConn("m6_ok"))
        connector_registry.register(_FailConn("m6_fail"))
        try:
            result = await tools_impl._tool_search_external_database(
                {"query": "q", "domain": "general"}
            )
        finally:
            connector_registry._connectors.pop("m6_ok", None)
            connector_registry._connectors.pop("m6_fail", None)

        assert "error" not in result
        assert result["total_results"] == 1


# ---------------------------------------------------------------------------
# L5 — failed_papers reconciled against what actually landed
# ---------------------------------------------------------------------------


class TestIngestArxivFailedPapersReconciliation:
    async def test_landed_paper_is_removed_from_failed_papers(self) -> None:
        """A batch metadata-fetch failure pessimistically marks every
        requested id; if the stub-paper fallback later lands and persists
        that id anyway, the stale failure entry must not survive.
        """
        from src.services.arxiv.persistence import ArxivPersistenceResult

        user = _mock_user()
        pid = "2401.12345"
        landed_doc = Mock()
        landed_doc.id = uuid4()
        landed_doc.document_metadata = {"arxiv_id": pid}

        mock_service = AsyncMock()
        mock_service.get_papers_by_ids = AsyncMock(
            side_effect=RuntimeError("arXiv batch metadata endpoint down")
        )
        mock_service.ingest_papers = AsyncMock(return_value=[landed_doc])

        persisted = ArxivPersistenceResult(
            document_ids=[str(landed_doc.id)],
            reused_document_ids=set(),
            kb_sync_failed=False,
            failed_papers={},
        )

        with (
            patch(
                "src.services.arxiv.arxiv_service.ArXivIngestionService",
                Mock(return_value=_mock_service_ctx(mock_service)),
            ),
            patch(
                "src.services.arxiv.persistence.persist_arxiv_documents",
                AsyncMock(return_value=persisted),
            ),
        ):
            result = await tools_impl._tool_ingest_arxiv(
                {"paper_ids": [pid]},
                str(user.id),
                db=AsyncMock(),
                current_user=user,
            )

        assert result["failed_papers"] == [], (
            "a paper that landed via the stub fallback and persisted "
            "cleanly must not still be reported as failed: "
            f"{result['failed_papers']!r}"
        )
        assert result["status"] == "ingestion_complete"


# ---------------------------------------------------------------------------
# L6 — list_project_documents status vocabulary
# ---------------------------------------------------------------------------


class TestListProjectDocumentsStatusMapping:
    async def test_completed_document_reports_indexed(self) -> None:
        from src.models.document import ProcessingStatus

        project = SimpleNamespace(id=uuid4(), name="Scoped project")
        current_user = cast(User, SimpleNamespace(id=uuid4(), organization_id=uuid4()))

        doc = Mock()
        doc.id = uuid4()
        doc.title = "Doc"
        doc.document_type = None
        doc.processing_status = ProcessingStatus.COMPLETED

        count_result = Mock()
        count_result.scalar_one.return_value = 1
        rows_result = Mock()
        rows_result.scalars.return_value.all.return_value = [doc]

        db = AsyncMock()
        calls: list[Any] = []

        async def capture_execute(statement: Any) -> Mock:
            calls.append(statement)
            return count_result if len(calls) == 1 else rows_result

        db.execute = AsyncMock(side_effect=capture_execute)

        with patch.object(
            tools_impl,
            "_verify_project_ownership",
            AsyncMock(return_value=project),
        ):
            result = await tools_impl._tool_list_project_documents(
                {"project_id": str(project.id)}, db, current_user
            )

        assert result["documents"][0]["status"] == "indexed", (
            "must mirror search_documents' ApiDocumentStatus.from_db mapping, "
            f"not the raw db string: {result['documents'][0]['status']!r}"
        )


class TestGetCurrentDraftTruth:
    async def test_returns_persisted_current_draft(self) -> None:
        project = SimpleNamespace(id=uuid4(), name="Draft project")
        current_user = cast(User, SimpleNamespace(id=uuid4(), organization_id=uuid4()))
        draft = SimpleNamespace(
            id=uuid4(),
            version=2,
            title="Literature Review",
            word_count=1200,
            citation_count=6,
            created_at=None,
            content="# Draft\n\nGrounded text.",
        )

        with (
            patch.object(
                tools_impl,
                "_verify_project_ownership",
                AsyncMock(return_value=project),
            ),
            patch(
                "src.services.research.draft_generation_service."
                "DraftGenerationService.get_draft",
                AsyncMock(return_value=draft),
            ),
        ):
            result = await tools_impl._tool_get_current_draft(
                {"project_id": str(project.id), "include_content": True},
                AsyncMock(),
                current_user,
            )

        assert result["status"] == "draft_found"
        assert result["draft"]["version"] == 2
        assert result["draft"]["content"] == draft.content


# ---------------------------------------------------------------------------
# B8-S2 / B8-S3 — extract_entities payload honesty
# ---------------------------------------------------------------------------


def _extraction_result(entities: List[Any], error: Optional[str]) -> Any:
    return SimpleNamespace(entities=entities, chunks_processed=1, error=error)


async def _run_extract_entities(result: Any) -> Dict[str, Any]:
    doc = SimpleNamespace(content_text="some text", title="Doc")
    service = AsyncMock()
    service.extract_entities = AsyncMock(return_value=result)
    with (
        patch.object(tools_impl, "_resolve_document_id", AsyncMock(return_value=doc)),
        patch(
            "src.services.processing.llm_entity_extraction.LLMEntityExtractionService",
            Mock(return_value=service),
        ),
    ):
        return await tools_impl._tool_extract_entities(
            {"document_id": str(uuid4())}, Mock(), _mock_user()
        )


async def test_zero_entities_without_failure_carries_no_error_key():
    payload = await _run_extract_entities(_extraction_result([], None))

    # B8-S2: `_execute_single_tool` classifies on key PRESENCE, so an
    # `"error": None` reported an empty-but-successful extraction as a
    # crashed tool and counted it toward MAX_ERRORS.
    assert "error" not in payload
    assert payload["total"] == 0


async def test_zero_entities_with_failure_keeps_the_error():
    payload = await _run_extract_entities(_extraction_result([], "llm timeout"))

    assert payload["error"] == "llm timeout"


async def test_partial_extraction_reports_partial_error_with_the_entities():
    entity = SimpleNamespace(
        name="X", type="PERSON", description="", confidence=0.9, aliases=[]
    )
    payload = await _run_extract_entities(_extraction_result([entity], "truncated"))

    # B8-S3: the partial signal must reach the model, but not under `error`
    # (that key means "the tool failed" to the classifier).
    assert payload["partial_error"] == "truncated"
    assert "error" not in payload
    assert payload["total"] == 1
