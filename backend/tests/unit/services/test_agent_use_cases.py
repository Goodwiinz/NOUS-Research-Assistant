"""End-to-end use case tests for agent tool workflows.

Simulates real user scenarios by chaining tool calls in the order the
agent graph would execute them. Verifies data flows correctly between
steps and that documents actually persist and get linked to projects.
"""

from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from tests.mocks.services import MockAsyncSession

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_user(org_id=None):
    user = Mock()
    user.id = uuid4()
    user.organization_id = org_id or uuid4()
    return user


def _make_arxiv_paper(paper_id: str, title: str):
    """Create a mock ArXiv paper dict as returned by search_papers."""
    return {
        "id": paper_id,
        "title": title,
        "authors": [{"name": "Test Author"}],
        "abstract": f"Abstract for {title}",
        "published": "2025-01-01",
        "updated": "2025-01-02",
        "categories": ["cs.AI"],
        "links": {"pdf": f"https://arxiv.org/pdf/{paper_id}"},
    }


def _make_ingested_doc(title: str, arxiv_id: str):
    """Create a mock ingested document as returned by ingest_papers."""
    doc = Mock()
    doc.title = title
    doc.filename = f"{title.lower().replace(' ', '_')}.pdf"
    doc.file_path = f"/tmp/{doc.filename}"
    doc.file_size_bytes = 2048
    doc.mime_type = "application/pdf"
    doc.content_text = f"Full text of {title}"
    doc.content_summary = f"Summary of {title}"
    doc.document_metadata = {"source": "arxiv", "arxiv_id": arxiv_id}
    return doc


def _make_mock_project(name="Test Project", proj_id=None):
    proj = Mock()
    proj.id = proj_id or uuid4()
    proj.name = name
    return proj


async def test_ingest_surfaces_per_paper_storage_failure_as_partial() -> None:
    from src.api.agent.execute import _tool_ingest_arxiv
    from src.services.arxiv.persistence import ArxivPersistenceResult

    user = _mock_user()
    paper_ids = ["2401.00001v1", "2401.00002v1"]
    papers = [_make_arxiv_paper(pid, f"Paper {pid}") for pid in paper_ids]
    documents = [_make_ingested_doc(paper["title"], paper["id"]) for paper in papers]
    service = AsyncMock()
    service.get_papers_by_ids = AsyncMock(return_value=papers)
    service.ingest_papers = AsyncMock(return_value=documents)
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=service)
    context.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(
            "src.services.arxiv.arxiv_service.ArXivIngestionService",
            return_value=context,
        ),
        patch(
            "src.services.arxiv.persistence.persist_arxiv_documents",
            new_callable=AsyncMock,
            return_value=ArxivPersistenceResult(
                document_ids=["document-1"],
                reused_document_ids=set(),
                failed_papers={"2401.00002v1": "durable storage promotion failed"},
            ),
        ),
    ):
        result = await _tool_ingest_arxiv(
            args={"paper_ids": paper_ids},
            user_id=str(user.id),
            db=AsyncMock(),
            current_user=user,
        )

    assert result["status"] == "ingestion_partial"
    assert result["document_ids"] == ["document-1"]
    assert result["failed_papers"] == [
        {
            "paper_id": "2401.00002v1",
            "reason": "durable storage promotion failed",
        }
    ]
    assert "error" in result


# ---------------------------------------------------------------------------
# Use Case: "Grab me all papers about X and add to my project"
# ---------------------------------------------------------------------------


class TestSearchIngestAddWorkflow:
    """
    Simulates the full user journey:
      1. search_arxiv  -> find papers
      2. ingest_arxiv_papers -> create documents in DB
      3. add_document_to_project -> link each document to the project

    Verifies that UUIDs flow from ingest to add. Ingest keeps its
    phase-isolated fresh sessions; the add step writes via the per-call
    session it is handed (audit B8).
    """

    async def test_full_search_ingest_add_flow(self):
        """Papers found by search should be ingestable and addable to a project."""
        from src.api.agent.execute import (
            _tool_add_document_to_project,
            _tool_ingest_arxiv,
            _tool_search_arxiv,
        )

        user = _mock_user()
        project = _make_mock_project(name="Attention Research")

        # --- Step 1: Search arXiv ---
        papers = [
            _make_arxiv_paper("2301.00001v1", "Attention Is All You Need"),
            _make_arxiv_paper("2301.00002v1", "Gated Sparse Attention"),
        ]

        mock_service = AsyncMock()
        mock_service.search_papers = AsyncMock(return_value=papers)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_service)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.services.arxiv.arxiv_service.ArXivIngestionService",
            return_value=mock_ctx,
        ):
            search_result = await _tool_search_arxiv(
                args={"query": "attention models", "max_results": 5}
            )

        assert len(search_result["papers"]) == 2
        paper_ids = [p["id"] for p in search_result["papers"]]
        assert "2301.00001v1" in paper_ids

        # --- Step 2: Ingest papers ---
        ingested_docs = [
            _make_ingested_doc("Attention Is All You Need", "2301.00001v1"),
            _make_ingested_doc("Gated Sparse Attention", "2301.00002v1"),
        ]

        mock_service_2 = AsyncMock()
        mock_service_2.search_papers = AsyncMock(
            side_effect=lambda query, max_results: [
                _make_arxiv_paper(query.split(":")[-1], f"Paper {query}")
            ]
        )
        mock_service_2.ingest_papers = AsyncMock(return_value=ingested_docs)

        mock_ctx_2 = AsyncMock()
        mock_ctx_2.__aenter__ = AsyncMock(return_value=mock_service_2)
        mock_ctx_2.__aexit__ = AsyncMock(return_value=False)

        ingest_fresh_db = MockAsyncSession()

        # Patch Document so each instance auto-assigns a UUID
        from src.models.document import Document as RealDocument

        original_init = RealDocument.__init__

        def _auto_id_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            if not self.id:
                self.id = uuid4()

        with (
            patch(
                "src.services.arxiv.arxiv_service.ArXivIngestionService",
                return_value=mock_ctx_2,
            ),
            patch(
                "src.core.database.AsyncSessionLocal",
                return_value=ingest_fresh_db,
            ),
            patch.object(RealDocument, "__init__", _auto_id_init),
        ):
            ingest_result = await _tool_ingest_arxiv(
                args={"paper_ids": paper_ids},
                user_id=str(user.id),
                db=AsyncMock(),  # shared session — should be ignored
                current_user=user,
            )

        assert ingest_result["status"] == "ingestion_complete"
        assert ingest_result["ingested_count"] == 2
        document_ids = ingest_result["document_ids"]
        assert len(document_ids) == 2
        ingest_fresh_db.assert_added(count=2)
        ingest_fresh_db.assert_committed()

        # --- Step 3: Add each document to the project ---
        # audit B8: the tool writes via the tool-call-scoped session passed
        # in by execute_tool (one fresh session per call), not an ad-hoc
        # AsyncSessionLocal of its own.
        for doc_uuid in document_ids:
            mock_doc = Mock()
            mock_doc.id = doc_uuid
            mock_doc.title = "Ingested Paper"

            add_db = MockAsyncSession()
            add_db.set_scalar_result(None)  # not already linked

            with (
                patch(
                    "src.services.agent.tools_impl._resolve_document_id",
                    new_callable=AsyncMock,
                    return_value=mock_doc,
                ),
                patch(
                    "src.services.agent.tools_impl._verify_project_ownership",
                    new_callable=AsyncMock,
                    return_value=project,
                ),
            ):
                add_result = await _tool_add_document_to_project(
                    args={"document_id": doc_uuid, "project_id": str(project.id)},
                    db=add_db,
                    current_user=user,
                )

            assert add_result["status"] == "success"
            assert str(project.id) in add_result["project_id"]
            # Bulk INSERT..ON CONFLICT goes through db.execute (not db.add).
            assert len(add_db.execute_calls) >= 2
            add_db.assert_committed()


class TestAddWithoutIngestFails:
    """
    Simulates the bug scenario: user asks agent to add papers that
    were never ingested. The tool should return a clear error.
    """

    async def test_add_non_ingested_paper_returns_helpful_error(self):
        """add_document_to_project should fail with guidance when doc doesn't exist."""
        from src.api.agent.execute import _tool_add_document_to_project

        user = _mock_user()
        fake_doc_id = "1803.10916v1"  # arXiv ID, not a UUID

        with patch(
            "src.services.agent.tools_impl._resolve_document_id",
            new_callable=AsyncMock,
            return_value=None,  # document not found
        ):
            result = await _tool_add_document_to_project(
                args={"document_id": fake_doc_id, "project_id": str(uuid4())},
                db=MockAsyncSession(),
                current_user=user,
            )

        assert "error" in result
        assert "not found" in result["error"].lower()
        assert "ingest" in result["error"].lower()

    async def test_add_with_fabricated_uuid_returns_error(self):
        """Even a valid-looking UUID should fail if the document doesn't exist."""
        from src.api.agent.execute import _tool_add_document_to_project

        user = _mock_user()
        fake_uuid = str(uuid4())

        with patch(
            "src.services.agent.tools_impl._resolve_document_id",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await _tool_add_document_to_project(
                args={"document_id": fake_uuid, "project_id": str(uuid4())},
                db=MockAsyncSession(),
                current_user=user,
            )

        assert "error" in result
        assert fake_uuid in result["error"]


class TestProjectContextAutoFill:
    """
    Verifies that when the user is on a project page, the agent's
    tool_node auto-fills project_id from page_context so the user
    doesn't have to specify it.
    """

    async def test_project_id_auto_filled_from_context(self):
        """_execute_single_tool should inject project_id from page context."""
        from src.services.agent.graph import _execute_single_tool

        project_id = str(uuid4())
        page_context = {"type": "project", "project_id": project_id}

        tool_call = {
            "id": "tc-1",
            "name": "add_document_to_project",
            "args": {"document_id": str(uuid4())},  # no project_id
        }

        config = {
            "configurable": {
                "user_id": str(uuid4()),
                "organization_id": str(uuid4()),
                "thread_id": str(uuid4()),
            }
        }
        operation_context = {
            "tool_operation_protocol_version": 1,
            "tool_operation_turn_id": str(uuid4()),
        }

        with patch(
            "src.services.agent.tools_impl.execute_tool",
            new_callable=AsyncMock,
            return_value={"status": "success"},
        ) as mock_exec:
            await _execute_single_tool(
                tool_call,
                config,
                page_context,
                operation_context=operation_context,
            )

        # Verify project_id was injected into the tool args
        call_kwargs = mock_exec.call_args
        passed_args = call_kwargs.kwargs.get("args") or call_kwargs[1].get("args")
        assert passed_args["project_id"] == project_id

    async def test_project_id_not_overwritten_if_provided(self):
        """If LLM already provided project_id, context should not override."""
        from src.services.agent.graph import _execute_single_tool

        explicit_id = str(uuid4())
        context_id = str(uuid4())
        page_context = {"type": "project", "project_id": context_id}

        tool_call = {
            "id": "tc-2",
            "name": "add_document_to_project",
            "args": {"document_id": str(uuid4()), "project_id": explicit_id},
        }

        config = {
            "configurable": {
                "user_id": str(uuid4()),
                "organization_id": str(uuid4()),
                "thread_id": str(uuid4()),
            }
        }
        operation_context = {
            "tool_operation_protocol_version": 1,
            "tool_operation_turn_id": str(uuid4()),
        }

        with patch(
            "src.services.agent.tools_impl.execute_tool",
            new_callable=AsyncMock,
            return_value={"status": "success"},
        ) as mock_exec:
            await _execute_single_tool(
                tool_call,
                config,
                page_context,
                operation_context=operation_context,
            )

        call_kwargs = mock_exec.call_args
        passed_args = call_kwargs.kwargs.get("args") or call_kwargs[1].get("args")
        assert passed_args["project_id"] == explicit_id


class TestIngestReturnsUsableUUIDs:
    """
    Verifies that ingest_arxiv_papers returns real UUIDs that can be
    passed directly to add_document_to_project.
    """

    async def test_document_ids_are_valid_uuids(self):
        """Ingest should return UUID strings, not arXiv IDs."""
        from uuid import UUID

        from src.api.agent.execute import _tool_ingest_arxiv

        user = _mock_user()
        ingested_doc = _make_ingested_doc("Test Paper", "2301.00001v1")

        mock_service = AsyncMock()
        mock_service.search_papers = AsyncMock(
            return_value=[_make_arxiv_paper("2301.00001v1", "Test Paper")]
        )
        mock_service.ingest_papers = AsyncMock(return_value=[ingested_doc])

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_service)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        fresh_db = MockAsyncSession()

        # Patch Document so each instance gets a UUID on construction
        from src.models.document import Document as RealDocument

        original_init = RealDocument.__init__

        def _auto_id_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            if not self.id:
                self.id = uuid4()

        with (
            patch(
                "src.services.arxiv.arxiv_service.ArXivIngestionService",
                return_value=mock_ctx,
            ),
            patch(
                "src.core.database.AsyncSessionLocal",
                return_value=fresh_db,
            ),
            patch.object(RealDocument, "__init__", _auto_id_init),
        ):
            result = await _tool_ingest_arxiv(
                args={"paper_ids": ["2301.00001v1"]},
                user_id=str(user.id),
                db=AsyncMock(),
                current_user=user,
            )

        assert result["status"] == "ingestion_complete"
        assert len(result["document_ids"]) >= 1
        for doc_id in result["document_ids"]:
            # Should not raise — confirms it's a valid UUID, not an arXiv ID
            UUID(doc_id)
            assert "arxiv" not in doc_id.lower()


class TestSharedSessionNeverUsedForWrites:
    """
    audit B8: the session passed to a tool is now tool-call-scoped (one
    fresh session per execute_tool call). Ingest keeps deliberately
    phase-isolated AsyncSessionLocal transactions and must still never
    write to the passed session; add_document_to_project writes via the
    passed per-call session and commits it.
    """

    async def test_ingest_does_not_write_to_shared_session(self):
        """_tool_ingest_arxiv must not call add/commit on the passed-in db."""
        from src.api.agent.execute import _tool_ingest_arxiv

        user = _mock_user()
        shared_db = AsyncMock()

        ingested_doc = _make_ingested_doc("Paper X", "id1")
        mock_service = AsyncMock()
        mock_service.search_papers = AsyncMock(
            return_value=[_make_arxiv_paper("id1", "Paper X")]
        )
        mock_service.ingest_papers = AsyncMock(return_value=[ingested_doc])

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_service)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "src.services.arxiv.arxiv_service.ArXivIngestionService",
                return_value=mock_ctx,
            ),
            patch(
                "src.core.database.AsyncSessionLocal",
                return_value=MockAsyncSession(),
            ),
        ):
            await _tool_ingest_arxiv(
                args={"paper_ids": ["id1"]},
                user_id=str(user.id),
                db=shared_db,
                current_user=user,
            )

        shared_db.add.assert_not_called()
        shared_db.flush.assert_not_called()
        shared_db.commit.assert_not_called()

    async def test_add_doc_writes_via_tool_call_session(self):
        """_tool_add_document_to_project writes to the per-call session it
        is handed (audit B8) — no ad-hoc AsyncSessionLocal of its own."""
        from src.api.agent.execute import _tool_add_document_to_project

        user = _mock_user()

        doc = Mock(id=uuid4(), title="Doc")
        project = Mock(id=uuid4(), name="Proj")
        tool_db = MockAsyncSession()
        tool_db.set_scalar_result(None)

        def _fail_sessionmaker(*_a, **_kw):  # pragma: no cover - guard
            raise AssertionError(
                "add_document_to_project must not open its own session"
            )

        with (
            patch(
                "src.core.database.AsyncSessionLocal",
                side_effect=_fail_sessionmaker,
            ),
            patch(
                "src.services.agent.tools_impl._resolve_document_id",
                new_callable=AsyncMock,
                return_value=doc,
            ),
            patch(
                "src.services.agent.tools_impl._verify_project_ownership",
                new_callable=AsyncMock,
                return_value=project,
            ),
        ):
            result = await _tool_add_document_to_project(
                args={"document_id": str(doc.id), "project_id": str(project.id)},
                db=tool_db,
                current_user=user,
            )

        assert result["status"] == "success"
        # _link_documents_to_project writes via a bulk INSERT..ON CONFLICT
        # (db.execute), not db.add() — assert the statements + commit hit
        # the per-call session that was passed in.
        assert len(tool_db.execute_calls) >= 2  # existing-links SELECT + INSERT
        tool_db.assert_committed()
