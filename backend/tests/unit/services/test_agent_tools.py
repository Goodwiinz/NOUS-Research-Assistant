"""Unit tests for agent tool functions.

Covers the fresh-session pattern used by destructive tools
(_tool_add_document_to_project, _tool_ingest_arxiv) to ensure
DB writes are committed independently of the shared graph session.
"""

import json
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, Mock, patch
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


def _mock_document(title="Test Doc", doc_id=None):
    doc = Mock()
    doc.id = doc_id or uuid4()
    doc.title = title
    doc.filename = f"{title.lower().replace(' ', '_')}.pdf"
    return doc


def _mock_project(name="Test Project", proj_id=None):
    proj = Mock()
    proj.id = proj_id or uuid4()
    proj.name = name
    return proj


def _mock_arxiv_service_module(service_context):
    module = ModuleType("src.services.arxiv.arxiv_service")
    module.ArXivIngestionService = Mock(return_value=service_context)
    return module


# ---------------------------------------------------------------------------
# _tool_add_document_to_project
# ---------------------------------------------------------------------------


class TestAddDocumentToProject:
    """_tool_add_document_to_project writes via the per-call session (B8)."""

    async def test_success_writes_via_tool_call_session(self):
        """Tool commits via the per-call session passed in (audit B8) —
        no ad-hoc AsyncSessionLocal of its own."""
        from src.api.agent.execute import _tool_add_document_to_project

        user = _mock_user()
        doc = _mock_document(title="Attention Paper")
        project = _mock_project(name="My Project")

        tool_db = MockAsyncSession()
        tool_db._scalar_result = None  # existing-link check → not linked

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
        assert "Attention Paper" in result["message"]
        # _link_documents_to_project uses a bulk INSERT..ON CONFLICT via
        # db.execute (not db.add) — assert the statements + commit landed
        # on the per-call session.
        assert len(tool_db.execute_calls) >= 2  # existing-links SELECT + INSERT
        tool_db.assert_committed()

    async def test_document_not_found_returns_error(self):
        """Tool should return a helpful error when document doesn't exist."""
        from src.api.agent.execute import _tool_add_document_to_project

        user = _mock_user()

        with patch(
            "src.services.agent.tools_impl._resolve_document_id",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await _tool_add_document_to_project(
                args={"document_id": "fake-id", "project_id": str(uuid4())},
                db=MockAsyncSession(),
                current_user=user,
            )

        assert "error" in result
        assert "not found" in result["error"].lower()
        assert "ingest" in result["error"].lower()

    async def test_already_linked_returns_status(self):
        """Tool should detect and report already-linked documents."""
        from src.api.agent.execute import _tool_add_document_to_project

        user = _mock_user()
        doc = _mock_document()
        project = _mock_project()

        tool_db = MockAsyncSession()
        # The already-linked branch keys off the existence SELECT returning
        # rows: _link_documents_to_project reads row[0] per row.
        tool_db.set_query_result([(str(doc.id),)])

        with (
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

        assert result["status"] == "already_linked"

    async def test_missing_document_id_returns_error(self):
        """Tool should reject calls without document_id."""
        from src.api.agent.execute import _tool_add_document_to_project

        result = await _tool_add_document_to_project(
            args={"project_id": str(uuid4())},
            db=AsyncMock(),
            current_user=_mock_user(),
        )

        assert "error" in result

    async def test_no_auth_returns_error(self):
        """Tool should reject unauthenticated calls."""
        from src.api.agent.execute import _tool_add_document_to_project

        result = await _tool_add_document_to_project(
            args={"document_id": str(uuid4()), "project_id": str(uuid4())},
            db=AsyncMock(),
            current_user=None,
        )

        assert "error" in result
        assert "authentication" in result["error"].lower()


# ---------------------------------------------------------------------------
# _tool_ingest_arxiv
# ---------------------------------------------------------------------------


class TestIngestArxiv:
    """Tests for _tool_ingest_arxiv fresh-session behaviour."""

    async def test_ingested_pdf_is_persisted_to_s3(self, tmp_path):
        """A downloaded arXiv PDF must survive the pod that downloaded it."""
        from src.core.config import settings
        from src.services.agent.tools_impl import _tool_ingest_arxiv

        user = _mock_user()
        pdf_path = tmp_path / "2601.05264v1.pdf"
        pdf_bytes = b"%PDF-1.7\npreviewable arxiv paper"
        pdf_path.write_bytes(pdf_bytes)

        ingested_doc = Mock()
        ingested_doc.title = "Previewable Paper"
        ingested_doc.filename = pdf_path.name
        ingested_doc.file_size_bytes = len(pdf_bytes)
        ingested_doc.mime_type = "application/pdf"
        ingested_doc.content_text = "paper text"
        ingested_doc.content_summary = None
        ingested_doc.document_metadata = {
            "arxiv_id": "2601.05264v1",
            "pdf_path": str(pdf_path),
        }

        mock_service = AsyncMock()
        mock_service.search_papers = AsyncMock(return_value=[{"id": "2601.05264v1"}])
        mock_service.ingest_papers = AsyncMock(return_value=[ingested_doc])
        mock_service_ctx = AsyncMock()
        mock_service_ctx.__aenter__ = AsyncMock(return_value=mock_service)
        mock_service_ctx.__aexit__ = AsyncMock(return_value=False)
        fresh_db = MockAsyncSession()

        with (
            patch.dict(
                sys.modules,
                {
                    "src.services.arxiv.arxiv_service": _mock_arxiv_service_module(
                        mock_service_ctx
                    )
                },
            ),
            patch("src.core.database.AsyncSessionLocal", return_value=fresh_db),
            patch.object(settings, "STORAGE_BACKEND", "s3"),
            patch.object(settings, "S3_BUCKET_NAME", "rag-system-storage"),
            patch("src.core.s3_client.S3StorageHelper") as helper_cls,
        ):
            helper_cls.return_value.upload_file.side_effect = (
                lambda key, _data, _mime_type: key
            )
            result = await _tool_ingest_arxiv(
                args={"paper_ids": ["2601.05264v1"]},
                user_id=str(user.id),
                db=AsyncMock(),
                current_user=user,
            )

        assert result["status"] == "ingestion_complete"
        persisted = fresh_db._added_items[0]
        assert persisted.storage_backend == "s3"
        assert persisted.storage_path.startswith(
            f"documents/{user.organization_id}/{persisted.id}/"
        )
        assert persisted.file_path == (
            f"s3://rag-system-storage/{persisted.storage_path}"
        )
        helper_cls.return_value.upload_file.assert_called_once_with(
            persisted.storage_path,
            pdf_bytes,
            "application/pdf",
        )

    async def test_abstract_only_paper_persists_as_text_in_s3(self):
        """A transient PDF failure must not abort metadata-only ingestion."""
        from src.core.config import settings
        from src.models.document import DocumentType
        from src.services.agent.tools_impl import _tool_ingest_arxiv

        user = _mock_user()
        ingested_doc = Mock()
        ingested_doc.title = "Abstract Only"
        ingested_doc.filename = "2601.00001v1.pdf"
        ingested_doc.file_size_bytes = 0
        ingested_doc.mime_type = "application/pdf"
        ingested_doc.content_text = "# Abstract\n\nMetadata remains useful."
        ingested_doc.content_summary = None
        ingested_doc.document_metadata = {
            "arxiv_id": "2601.00001v1",
            "source": "arxiv",
            "pdf_extraction_failed": True,
            "has_full_text": False,
        }

        mock_service = AsyncMock()
        mock_service.search_papers = AsyncMock(return_value=[{"id": "2601.00001v1"}])
        mock_service.ingest_papers = AsyncMock(return_value=[ingested_doc])
        mock_service_ctx = AsyncMock()
        mock_service_ctx.__aenter__ = AsyncMock(return_value=mock_service)
        mock_service_ctx.__aexit__ = AsyncMock(return_value=False)
        fresh_db = MockAsyncSession()

        with (
            patch.dict(
                sys.modules,
                {
                    "src.services.arxiv.arxiv_service": _mock_arxiv_service_module(
                        mock_service_ctx
                    )
                },
            ),
            patch("src.core.database.AsyncSessionLocal", return_value=fresh_db),
            patch.object(settings, "STORAGE_BACKEND", "s3"),
            patch.object(settings, "S3_BUCKET_NAME", "rag-system-storage"),
            patch("src.core.s3_client.S3StorageHelper") as helper_cls,
        ):
            helper_cls.return_value.upload_file.side_effect = (
                lambda key, _data, _mime_type: key
            )
            result = await _tool_ingest_arxiv(
                args={"paper_ids": ["2601.00001v1"]},
                user_id=str(user.id),
                db=AsyncMock(),
                current_user=user,
            )

        assert result["status"] == "ingestion_complete"
        persisted = fresh_db._added_items[0]
        assert persisted.document_type is DocumentType.TEXT
        assert persisted.filename == "2601.00001v1.txt"
        assert persisted.mime_type == "text/plain"
        assert persisted.storage_backend == "s3"
        helper_cls.return_value.upload_file.assert_called_once_with(
            persisted.storage_path,
            ingested_doc.content_text.encode("utf-8"),
            "text/plain",
        )

    async def test_storage_is_deleted_when_document_transaction_fails(self, tmp_path):
        """Object promotion must be compensated when the DB row rolls back."""
        from src.core.config import settings
        from src.services.agent.tools_impl import _tool_ingest_arxiv

        user = _mock_user()
        pdf_path = tmp_path / "2601.00002v1.pdf"
        pdf_path.write_bytes(b"%PDF-1.7\nrollback")
        ingested_doc = Mock(
            title="Rollback Paper",
            filename=pdf_path.name,
            file_size_bytes=pdf_path.stat().st_size,
            mime_type="application/pdf",
            content_text="paper text",
            content_summary=None,
            document_metadata={
                "arxiv_id": "2601.00002v1",
                "pdf_path": str(pdf_path),
            },
        )
        mock_service = AsyncMock()
        mock_service.search_papers = AsyncMock(return_value=[{"id": "2601.00002v1"}])
        mock_service.ingest_papers = AsyncMock(return_value=[ingested_doc])
        mock_service_ctx = AsyncMock()
        mock_service_ctx.__aenter__ = AsyncMock(return_value=mock_service)
        mock_service_ctx.__aexit__ = AsyncMock(return_value=False)
        fresh_db = MockAsyncSession()
        fresh_db.flush = AsyncMock(side_effect=RuntimeError("database unavailable"))

        with (
            patch.dict(
                sys.modules,
                {
                    "src.services.arxiv.arxiv_service": _mock_arxiv_service_module(
                        mock_service_ctx
                    )
                },
            ),
            patch("src.core.database.AsyncSessionLocal", return_value=fresh_db),
            patch.object(settings, "STORAGE_BACKEND", "s3"),
            patch.object(settings, "S3_BUCKET_NAME", "rag-system-storage"),
            patch("src.core.s3_client.S3StorageHelper") as helper_cls,
        ):
            helper_cls.return_value.upload_file.side_effect = (
                lambda key, _data, _mime_type: key
            )
            result = await _tool_ingest_arxiv(
                args={"paper_ids": ["2601.00002v1"]},
                user_id=str(user.id),
                db=AsyncMock(),
                current_user=user,
            )

        uploaded_key = helper_cls.return_value.upload_file.call_args.args[0]
        assert "error" in result
        helper_cls.return_value.delete_file.assert_called_once_with(uploaded_key)

    async def test_ingested_papers_use_fresh_session(self):
        """Ingest should persist documents via AsyncSessionLocal, not shared db."""
        from src.services.agent.tools_impl import _tool_ingest_arxiv

        user = _mock_user()
        paper_ids = ["2301.00001v1", "2301.00002v1"]

        mock_paper = Mock()
        mock_paper.title = "Test Paper"
        mock_paper.filename = "test.pdf"
        mock_paper.file_path = "/tmp/test.pdf"
        mock_paper.file_size_bytes = 1024
        mock_paper.mime_type = "application/pdf"
        mock_paper.content_text = "Some content"
        mock_paper.content_summary = "Summary"
        mock_paper.document_metadata = {"arxiv_id": "2301.00001v1"}

        fresh_db = MockAsyncSession()

        mock_service = AsyncMock()
        mock_service.search_papers = AsyncMock(
            return_value=[{"id": "2301.00001v1", "title": "Test Paper"}]
        )
        mock_service.ingest_papers = AsyncMock(return_value=[mock_paper, mock_paper])

        mock_service_ctx = AsyncMock()
        mock_service_ctx.__aenter__ = AsyncMock(return_value=mock_service)
        mock_service_ctx.__aexit__ = AsyncMock(return_value=False)

        shared_db = AsyncMock()  # graph session — should NOT be used for writes

        with (
            patch.dict(
                sys.modules,
                {
                    "src.services.arxiv.arxiv_service": _mock_arxiv_service_module(
                        mock_service_ctx
                    )
                },
            ),
            patch(
                "src.core.database.AsyncSessionLocal",
                return_value=fresh_db,
            ),
        ):
            result = await _tool_ingest_arxiv(
                args={"paper_ids": paper_ids},
                user_id=str(user.id),
                db=shared_db,
                current_user=user,
            )

        assert result["status"] == "ingestion_complete"
        assert len(result["document_ids"]) == 2
        fresh_db.assert_added(count=2)
        fresh_db.assert_committed()

        # Verify shared db was NOT used for writes
        shared_db.add.assert_not_called()
        shared_db.commit.assert_not_called()

    async def test_ingest_persists_metadata_title_for_immediate_search(self):
        """The arXiv metadata title must win over a transient placeholder."""
        from src.services.agent.tools_impl import _tool_ingest_arxiv

        user = _mock_user()
        ingested_doc = Mock()
        ingested_doc.title = "arXiv:2601.00003v1"
        ingested_doc.filename = "2601.00003v1.pdf"
        ingested_doc.file_size_bytes = 0
        ingested_doc.mime_type = "text/plain"
        ingested_doc.content_text = "abstract"
        ingested_doc.content_summary = None
        ingested_doc.document_metadata = {
            "title": "  Real\nPaper\tTitle  ",
            "arxiv_id": "2601.00003v1",
        }

        mock_service = AsyncMock()
        mock_service.get_papers_by_ids = AsyncMock(
            return_value=[{"id": "2601.00003v1", "title": "Real Paper Title"}]
        )
        mock_service.ingest_papers = AsyncMock(return_value=[ingested_doc])
        mock_service_ctx = AsyncMock()
        mock_service_ctx.__aenter__ = AsyncMock(return_value=mock_service)
        mock_service_ctx.__aexit__ = AsyncMock(return_value=False)
        fresh_db = MockAsyncSession()

        with (
            patch.dict(
                sys.modules,
                {
                    "src.services.arxiv.arxiv_service": _mock_arxiv_service_module(
                        mock_service_ctx
                    )
                },
            ),
            patch("src.core.database.AsyncSessionLocal", return_value=fresh_db),
        ):
            result = await _tool_ingest_arxiv(
                args={"paper_ids": ["2601.00003v1"]},
                user_id=str(user.id),
                db=AsyncMock(),
                current_user=user,
            )

        assert result["status"] == "ingestion_complete"
        assert fresh_db._added_items[0].title == "Real Paper Title"

    async def test_ingest_no_papers_returns_error(self):
        """Should reject empty paper_ids list."""
        from src.api.agent.execute import _tool_ingest_arxiv

        result = await _tool_ingest_arxiv(
            args={"paper_ids": []},
            user_id="user-1",
            db=AsyncMock(),
            current_user=_mock_user(),
        )

        assert "error" in result

    async def test_ingest_too_many_papers_returns_error(self):
        """Should reject more than 10 papers."""
        from src.api.agent.execute import _tool_ingest_arxiv

        result = await _tool_ingest_arxiv(
            args={"paper_ids": [f"id-{i}" for i in range(11)]},
            user_id="user-1",
            db=AsyncMock(),
            current_user=_mock_user(),
        )

        assert "error" in result
        assert "10" in result["error"]

    async def test_ingest_service_failure_returns_error(self):
        """Should catch service exceptions and return an error dict."""
        from src.api.agent.execute import _tool_ingest_arxiv

        mock_service_ctx = AsyncMock()
        mock_service_ctx.__aenter__ = AsyncMock(
            side_effect=RuntimeError("ArXiv API down")
        )
        mock_service_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.services.arxiv.arxiv_service.ArXivIngestionService",
            return_value=mock_service_ctx,
        ):
            result = await _tool_ingest_arxiv(
                args={"paper_ids": ["2301.00001v1"]},
                user_id="user-1",
                db=AsyncMock(),
                current_user=_mock_user(),
            )

        assert "error" in result

    async def test_ingest_without_user_fails_closed(self):
        """Without current_user, must fail closed like every sibling tool —
        no ArXivIngestionService instantiation, no document_ids fabricated
        from ingested-but-never-persisted objects (audit H1). Previously this
        fell through to a fallback that returned ``getattr(doc, "id", None)``
        for documents the DB never saw.
        """
        from src.api.agent.execute import _tool_ingest_arxiv

        mock_service_ctor = Mock(
            side_effect=AssertionError(
                "ArXivIngestionService must not be instantiated without an "
                "authenticated current_user"
            )
        )

        with patch(
            "src.services.arxiv.arxiv_service.ArXivIngestionService",
            mock_service_ctor,
        ):
            result = await _tool_ingest_arxiv(
                args={"paper_ids": ["2301.00001v1"]},
                user_id="",
                db=AsyncMock(),
                current_user=None,
            )

        assert result == {"error": "Authentication required"}
        mock_service_ctor.assert_not_called()


# ---------------------------------------------------------------------------
# execute_tool dispatch
# ---------------------------------------------------------------------------


class TestExecuteToolDispatch:
    """Test that execute_tool routes to the correct tool function."""

    async def test_unknown_tool_returns_error(self):
        """Unrecognised tool names should return an error, not crash."""
        from src.api.agent.execute import execute_tool

        result = await execute_tool(
            tool_name="nonexistent_tool",
            args={},
            user_id="user-1",
            db=AsyncMock(),
            current_user=_mock_user(),
        )

        assert "error" in result

    async def test_add_document_dispatches_correctly(self):
        """execute_tool should route add_document_to_project to its handler."""
        from src.api.agent.execute import execute_tool

        with patch(
            "src.services.agent.tools_impl._tool_add_document_to_project",
            new_callable=AsyncMock,
            return_value={"status": "success"},
        ) as mock_handler:
            result = await execute_tool(
                tool_name="add_document_to_project",
                args={"document_id": "d1", "project_id": "p1"},
                user_id="user-1",
                db=AsyncMock(),
                current_user=_mock_user(),
            )

        mock_handler.assert_awaited_once()
        assert result["status"] == "success"

    async def test_create_project_dispatches_correctly(self):
        """execute_tool should route create_project to its handler."""
        from src.api.agent.execute import execute_tool

        with patch(
            "src.services.agent.tools_impl._tool_create_project",
            new_callable=AsyncMock,
            return_value={"status": "success", "project_id": "p1"},
        ) as mock_handler:
            result = await execute_tool(
                tool_name="create_project",
                args={"name": "Diffusion Transformers"},
                user_id="user-1",
                db=AsyncMock(),
                current_user=_mock_user(),
            )

        mock_handler.assert_awaited_once()
        assert result["status"] == "success"

    async def test_forget_memory_dispatches_correctly(self):
        """execute_tool must route forget_memory to its handler (was Unknown tool)."""
        from src.api.agent.execute import execute_tool

        acting_user = _mock_user()
        with patch(
            "src.services.agent.tools_impl._tool_forget_memory",
            new_callable=AsyncMock,
            return_value={"status": "completed", "deleted": 1, "matches": []},
        ) as mock_handler:
            result = await execute_tool(
                tool_name="forget_memory",
                args={"query": "forget my transformer searches"},
                user_id="user-1",
                db=AsyncMock(),
                current_user=acting_user,
            )

        mock_handler.assert_awaited_once()
        # keyword-only handler — routing must pass query + user_id through.
        # Audit R7-L9: the id is the *resolved* user's, not the raw
        # config-supplied user_id — a destructive delete never runs for a
        # caller nobody verified.
        assert (
            mock_handler.await_args.kwargs["query"] == "forget my transformer searches"
        )
        assert mock_handler.await_args.kwargs["user_id"] == str(acting_user.id)
        assert result.get("status") == "completed"
        assert "error" not in result  # must NOT be the "Unknown tool" catch-all

    async def test_execute_code_threads_thread_id(self):
        """execute_tool must forward thread_id to _tool_execute_code (was hardcoded "")."""
        from src.api.agent.execute import execute_tool

        with patch(
            "src.services.agent.tools_impl._tool_execute_code",
            new_callable=AsyncMock,
            return_value={"status": "ok"},
        ) as mock_handler:
            await execute_tool(
                tool_name="execute_code",
                args={"code": "print(1)"},
                user_id="user-1",
                db=AsyncMock(),
                current_user=_mock_user(),
                thread_id="thread-abc",
            )

        mock_handler.assert_awaited_once()
        # The sandbox is keyed by thread_id; isolation breaks if it's empty.
        assert mock_handler.await_args.kwargs["thread_id"] == "thread-abc"


class TestCreateProject:
    """Tests for _tool_create_project."""

    async def test_missing_user_returns_error(self):
        from src.api.agent.execute import _tool_create_project

        result = await _tool_create_project(
            {"name": "Diffusion Transformers"}, None, None
        )
        assert "error" in result
        assert "Authentication" in result["error"]

    async def test_missing_name_returns_error(self):
        from src.api.agent.execute import _tool_create_project

        result = await _tool_create_project(
            {"name": "  "}, MockAsyncSession(), _mock_user()
        )
        assert "error" in result
        assert "name is required" in result["error"]

    async def test_invalid_workspace_id_returns_error(self):
        from src.api.agent.execute import _tool_create_project

        result = await _tool_create_project(
            {"name": "X", "workspace_id": "not-a-uuid"},
            MockAsyncSession(),
            _mock_user(),
        )
        assert "error" in result
        assert "UUID" in result["error"]

    async def test_success_uses_first_workspace_when_omitted(self):
        """When workspace_id is omitted, the user's first workspace is used."""
        from src.api.agent.execute import _tool_create_project

        user = _mock_user()
        workspace_id = uuid4()
        project = _mock_project(name="Diffusion Transformers")
        project.workspace_id = workspace_id

        service = MagicMock()
        service._get_workspace_ids_for_user = AsyncMock(return_value=[workspace_id])
        service.create_project = AsyncMock(return_value=project)

        with patch(
            "src.services.research.project_service.ProjectService",
            return_value=service,
        ):
            result = await _tool_create_project(
                {"name": "Diffusion Transformers", "tags": ["ml"]},
                MockAsyncSession(),
                user,
            )

        assert result["status"] == "success"
        assert result["project_id"] == str(project.id)
        assert result["workspace_id"] == str(workspace_id)
        service._get_workspace_ids_for_user.assert_awaited_once_with(user.id)
        service.create_project.assert_awaited_once()

    async def test_success_no_workspace_returns_error(self):
        """If the user has no workspace, the tool returns a clear error."""
        from src.api.agent.execute import _tool_create_project

        user = _mock_user()
        service = MagicMock()
        service._get_workspace_ids_for_user = AsyncMock(return_value=[])

        with patch(
            "src.services.research.project_service.ProjectService",
            return_value=service,
        ):
            result = await _tool_create_project({"name": "X"}, MockAsyncSession(), user)

        assert "error" in result
        assert "workspace" in result["error"].lower()


class TestListProjects:
    """Tests for _tool_list_projects (user-facing project discovery)."""

    async def test_missing_user_returns_error(self):
        from src.api.agent.execute import _tool_list_projects

        result = await _tool_list_projects({}, None, None)
        assert "error" in result
        assert "Authentication" in result["error"]

    async def test_success_returns_compact_payload(self):
        """The tool should call ProjectService.list_projects and flatten the result."""
        from datetime import datetime, timezone

        from src.api.agent.execute import _tool_list_projects

        user = _mock_user()
        project_a = _mock_project(name="Alpha")
        project_a.description = "first"
        project_a.research_status = "active"
        project_a.tags = ["ml"]
        project_a.updated_at = datetime(2026, 4, 24, tzinfo=timezone.utc)
        project_b = _mock_project(name="Beta")
        project_b.description = None
        project_b.research_status = "archived"
        project_b.tags = []
        project_b.updated_at = None

        service = MagicMock()
        service.list_projects = AsyncMock(
            return_value={
                "projects": [project_a, project_b],
                "total": 2,
                "page": 1,
                "size": 20,
                "has_next": False,
                "has_prev": False,
            }
        )

        with patch(
            "src.services.research.project_service.ProjectService",
            return_value=service,
        ):
            result = await _tool_list_projects(
                {"status": "active", "tag": "ml", "search": "Alp", "limit": 5},
                MockAsyncSession(),
                user,
            )

        service.list_projects.assert_awaited_once()
        call_kwargs = service.list_projects.await_args.kwargs
        assert call_kwargs["user_id"] == user.id
        assert call_kwargs["project_status"] == "active"
        assert call_kwargs["tag"] == "ml"
        assert call_kwargs["search"] == "Alp"
        assert call_kwargs["limit"] == 5

        assert result["total"] == 2
        assert result["returned"] == 2
        assert [p["name"] for p in result["projects"]] == ["Alpha", "Beta"]
        assert result["projects"][0]["id"] == str(project_a.id)
        assert result["projects"][0]["tags"] == ["ml"]
        assert result["projects"][0]["updated_at"] == "2026-04-24T00:00:00+00:00"
        assert result["projects"][1]["updated_at"] is None

    async def test_limit_is_clamped(self):
        """limit must be coerced into the [1, 50] range."""
        from src.api.agent.execute import _tool_list_projects

        user = _mock_user()
        service = MagicMock()
        service.list_projects = AsyncMock(return_value={"projects": [], "total": 0})

        with patch(
            "src.services.research.project_service.ProjectService",
            return_value=service,
        ):
            db = MockAsyncSession()
            await _tool_list_projects({"limit": 9999}, db, user)
            assert service.list_projects.await_args.kwargs["limit"] == 50

            service.list_projects.reset_mock()
            await _tool_list_projects({"limit": -3}, db, user)
            assert service.list_projects.await_args.kwargs["limit"] == 1

            service.list_projects.reset_mock()
            await _tool_list_projects({"limit": "not-a-number"}, db, user)
            assert service.list_projects.await_args.kwargs["limit"] == 20


class TestExtractProjectIdFromText:
    """Tests for _extract_project_id_from_text used in rag_node."""

    async def test_project_url_returns_uuid(self):
        from src.services.agent.graph import _extract_project_id_from_text

        text = "look at https://dev-app.gen-text.app/projects/fd68b324-5a89-47a4-a8ff-38d29f4fa496"
        assert (
            _extract_project_id_from_text(text)
            == "fd68b324-5a89-47a4-a8ff-38d29f4fa496"
        )

    async def test_bare_uuid_returns_uuid(self):
        from src.services.agent.graph import _extract_project_id_from_text

        text = "use project fd68b324-5a89-47a4-a8ff-38d29f4fa496 please"
        assert (
            _extract_project_id_from_text(text)
            == "fd68b324-5a89-47a4-a8ff-38d29f4fa496"
        )

    async def test_uppercase_is_normalised(self):
        from src.services.agent.graph import _extract_project_id_from_text

        text = "/projects/FD68B324-5A89-47A4-A8FF-38D29F4FA496"
        assert (
            _extract_project_id_from_text(text)
            == "fd68b324-5a89-47a4-a8ff-38d29f4fa496"
        )

    async def test_no_uuid_returns_none(self):
        from src.services.agent.graph import _extract_project_id_from_text

        assert _extract_project_id_from_text("nothing to see here") is None
        assert _extract_project_id_from_text("") is None

    async def test_prefers_project_url_over_bare_uuid(self):
        """When both a /projects/<uuid> URL and an unrelated UUID appear,
        the URL-scoped UUID wins."""
        from src.services.agent.graph import _extract_project_id_from_text

        text = (
            "doc 11111111-1111-1111-1111-111111111111 "
            "and /projects/22222222-2222-2222-2222-222222222222"
        )
        assert (
            _extract_project_id_from_text(text)
            == "22222222-2222-2222-2222-222222222222"
        )


# ---------------------------------------------------------------------------
# search_arxiv wrapper schema (recency_days / chronological)
# ---------------------------------------------------------------------------


class TestSearchArxivRecencyParams:
    async def test_search_arxiv_schema_exposes_recency_params(self):
        import inspect

        from src.services.agent.tools import search_arxiv

        args = getattr(search_arxiv, "args", None)
        if args is not None:
            assert {"recency_days", "chronological"} <= set(args)
        else:
            params = inspect.signature(
                getattr(search_arxiv, "func", search_arxiv)
            ).parameters
            assert {"recency_days", "chronological"} <= set(params)

    async def test_search_arxiv_passes_recency_through(self, monkeypatch):
        from src.services.agent.tools import search_arxiv

        captured: dict = {}

        async def fake_tool_search_arxiv(args):
            captured.update(args)
            return {"results": []}

        monkeypatch.setattr(
            "src.services.agent.tools_impl._tool_search_arxiv",
            fake_tool_search_arxiv,
        )

        await search_arxiv.ainvoke(
            {"query": "q", "recency_days": 0, "chronological": True}
        )

        assert captured["recency_days"] == 0
        assert captured["chronological"] is True
