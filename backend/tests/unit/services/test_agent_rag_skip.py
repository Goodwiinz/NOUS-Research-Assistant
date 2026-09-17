"""Unit tests for the rag_node short-conversational query skip path."""

import pytest


@pytest.mark.unit
class TestIsRetrievalQuery:
    def test_empty_string(self):
        from src.services.agent.graph import _is_retrieval_query

        assert _is_retrieval_query("") is False
        assert _is_retrieval_query("   ") is False

    def test_short_conversational_returns_false(self):
        from src.services.agent.graph import _is_retrieval_query

        for q in ("hi", "thanks!", "yes", "ok cool", "sure"):
            assert _is_retrieval_query(q) is False, q

    def test_retrieval_verb_short_query_returns_true(self):
        from src.services.agent.graph import _is_retrieval_query

        for q in (
            "find papers",
            "search for GANs",
            "show docs",
            "summarize this",
            "compare them",
            "what is RAG?",
            "tell me about it",
        ):
            assert _is_retrieval_query(q) is True, q

    def test_long_query_without_verb_still_routes_to_retrieval(self):
        from src.services.agent.graph import _is_retrieval_query

        q = "the conference deadline next quarter is going to be very tight"
        assert len(q.split()) >= 8
        assert _is_retrieval_query(q) is True

    def test_tool_name_prefix_returns_true(self):
        from src.services.agent.graph import _is_retrieval_query

        for q in (
            "list_projects",
            "search_arxiv",
            "summarize_document abc",
            "ingest_arxiv_papers 1234.5678",
            "extract_entities",
        ):
            assert _is_retrieval_query(q) is True, q


@pytest.mark.unit
@pytest.mark.asyncio
class TestRagNodeFastPath:
    async def test_rag_node_skips_search_for_trivial_query(self):
        from unittest.mock import AsyncMock

        from langchain_core.messages import HumanMessage

        from src.services.agent.graph import rag_node

        state = {
            "messages": [HumanMessage(content="hi")],
            "page_context": {},
            "retrieved_contexts": [],
            "thread_id": "t-1",
        }
        mock_search = AsyncMock(return_value=[])
        config = {"configurable": {"search_fn": mock_search}}

        result = await rag_node(state, config)

        assert mock_search.called is False
        assert result.get("retrieved_contexts", []) == []

    async def test_rag_node_runs_search_for_retrieval_query(self):
        from unittest.mock import AsyncMock

        from langchain_core.messages import HumanMessage

        from src.services.agent.graph import rag_node

        state = {
            "messages": [HumanMessage(content="find papers about transformers")],
            "page_context": {},
            "retrieved_contexts": [],
            "thread_id": "t-2",
        }
        mock_search = AsyncMock(return_value=[])
        config = {"configurable": {"search_fn": mock_search, "user_id": "user-test-1"}}

        await rag_node(state, config)

        assert mock_search.called is True

    async def test_rag_node_grounds_the_current_turn_attachment_without_broad_search(
        self, monkeypatch
    ):
        """An uploaded document must reach the model as the turn's source."""
        from unittest.mock import AsyncMock

        from langchain_core.messages import HumanMessage

        from src.services.agent.graph import rag_node

        document_id = "11111111-1111-4111-8111-111111111111"
        attachment_context = {
            "document_id": document_id,
            "title": "Kestrel launch notes",
            "content": "The launch authorization code is KESTREL-DOC-7314.",
            "score": 1.0,
            "score_source": "attachment",
            "context_origin": "attachment",
        }
        load_attachments = AsyncMock(return_value=([attachment_context], []))
        monkeypatch.setattr(
            "src.services.agent._nodes_rag._load_attachment_contexts",
            load_attachments,
            raising=False,
        )
        broad_search = AsyncMock(
            return_value=[
                {
                    "document_id": "22222222-2222-4222-8222-222222222222",
                    "title": "Unrelated org document",
                    "content": "A distractor source.",
                    "score": 0.99,
                }
            ]
        )
        state = {
            "messages": [
                HumanMessage(
                    content="Using only the attached file, give me the launch code"
                )
            ],
            "page_context": {},
            "retrieved_contexts": [],
            "thread_id": "33333333-3333-4333-8333-333333333333",
            "attachment_ids": [document_id],
            "use_rag": True,
        }
        config = {
            "configurable": {
                "user_id": "44444444-4444-4444-8444-444444444444",
                "organization_id": "55555555-5555-4555-8555-555555555555",
                "search_fn": broad_search,
            }
        }

        result = await rag_node(state, config)

        load_attachments.assert_awaited_once()
        broad_search.assert_not_awaited()
        assert result["retrieved_contexts"] == [attachment_context]

    async def test_explicit_attachment_scope_survives_rag_toggle_off(self, monkeypatch):
        """The toggle disables corpus search, while a selected file stays usable."""
        from unittest.mock import AsyncMock

        from langchain_core.messages import HumanMessage

        from src.services.agent.graph import rag_node

        document_id = "11111111-1111-4111-8111-111111111111"
        attachment_context = {
            "document_id": document_id,
            "title": "Kestrel launch notes",
            "content": "The launch authorization code is KESTREL-DOC-7314.",
            "score": 1.0,
            "score_source": "attachment",
            "context_origin": "attachment",
        }
        load_attachments = AsyncMock(return_value=([attachment_context], []))
        monkeypatch.setattr(
            "src.services.agent._nodes_rag._load_attachment_contexts",
            load_attachments,
        )
        broad_search = AsyncMock(return_value=[])
        state = {
            "messages": [HumanMessage(content="What is the launch code?")],
            "page_context": {},
            "retrieved_contexts": [],
            "thread_id": "33333333-3333-4333-8333-333333333333",
            "attachment_ids": [document_id],
            "use_rag": False,
        }
        config = {
            "configurable": {
                "user_id": "44444444-4444-4444-8444-444444444444",
                "organization_id": "55555555-5555-4555-8555-555555555555",
                "search_fn": broad_search,
            }
        }

        result = await rag_node(state, config)

        load_attachments.assert_awaited_once()
        broad_search.assert_not_awaited()
        assert result["retrieved_contexts"] == [attachment_context]

    async def test_rag_node_reuses_latest_thread_attachment_for_follow_up(
        self, monkeypatch
    ):
        """A retrieval-enabled follow-up can continue using its thread file."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from uuid import UUID

        from langchain_core.messages import HumanMessage

        from src.models.document import ProcessingStatus
        from src.services.agent import _nodes_rag

        user_id = UUID("44444444-4444-4444-8444-444444444444")
        organization_id = UUID("55555555-5555-4555-8555-555555555555")
        thread_id = UUID("33333333-3333-4333-8333-333333333333")
        document_id = UUID("11111111-1111-4111-8111-111111111111")
        document = SimpleNamespace(
            id=document_id,
            title="Kestrel launch notes",
            organization_id=organization_id,
            processing_status=ProcessingStatus.COMPLETED,
            content_text="The launch authorization code is KESTREL-DOC-7314.",
            is_deleted=False,
        )

        class _Result:
            def __init__(self, *, rows=(), docs=()):
                self._rows = list(rows)
                self._docs = list(docs)

            def all(self):
                return self._rows

            def scalars(self):
                return SimpleNamespace(all=lambda: self._docs)

        class _Session:
            def __init__(self):
                self.calls = 0

            async def execute(self, _statement):
                self.calls += 1
                if self.calls == 1:
                    return _Result(rows=[("message-1", document_id)])
                return _Result(docs=[document])

        session = _Session()

        class _SessionContext:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *_args):
                return False

        monkeypatch.setattr(
            "src.services.agent.tool_session.tool_session",
            lambda: _SessionContext(),
        )
        monkeypatch.setattr(
            "src.services.agent.tool_session.resolve_tool_user",
            AsyncMock(return_value=SimpleNamespace(id=user_id)),
        )
        thread_access = AsyncMock(return_value=SimpleNamespace(id=thread_id))
        monkeypatch.setattr(
            "src.services.threads.workspace_access.get_thread", thread_access
        )
        monkeypatch.setattr(
            "src.services.threads.workspace_access.filter_owned_document_ids",
            AsyncMock(return_value=[document_id]),
        )

        state = {
            "messages": [HumanMessage(content="What is the launch code?")],
            "page_context": {},
            "retrieved_contexts": [],
            "thread_id": str(thread_id),
            "thread_persistence": "durable",
            "use_rag": True,
        }
        config = {
            "configurable": {
                "thread_id": str(thread_id),
                "user_id": str(user_id),
                "organization_id": str(organization_id),
            }
        }

        result = await _nodes_rag.rag_node(state, config)

        thread_access.assert_awaited_once()
        assert result["retrieved_contexts"][0]["document_id"] == str(document_id)
        assert "KESTREL-DOC-7314" in result["retrieved_contexts"][0]["content"]

    async def test_follow_up_does_not_read_attachments_from_inaccessible_thread(
        self, monkeypatch
    ):
        """A guessed thread id cannot disclose its historical attachments."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from uuid import UUID

        from src.services.agent._nodes_rag import _load_attachment_contexts

        user_id = UUID("44444444-4444-4444-8444-444444444444")
        organization_id = UUID("55555555-5555-4555-8555-555555555555")
        thread_id = UUID("33333333-3333-4333-8333-333333333333")

        class _Session:
            def __init__(self):
                self.execute_calls = 0

            async def execute(self, _statement):
                self.execute_calls += 1
                raise AssertionError("history must not be queried")

        session = _Session()

        class _SessionContext:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *_args):
                return False

        monkeypatch.setattr(
            "src.services.agent.tool_session.tool_session",
            lambda: _SessionContext(),
        )
        monkeypatch.setattr(
            "src.services.agent.tool_session.resolve_tool_user",
            AsyncMock(return_value=SimpleNamespace(id=user_id)),
        )
        thread_access = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "src.services.threads.workspace_access.get_thread", thread_access
        )

        contexts, statuses = await _load_attachment_contexts(
            attachment_ids=[],
            thread_id=str(thread_id),
            user_id=str(user_id),
            organization_id=str(organization_id),
            query="What is the launch code?",
        )

        thread_access.assert_awaited_once()
        assert session.execute_calls == 0
        assert contexts == []
        assert statuses == [
            {
                "status": "unavailable",
                "title": "previous thread attachment",
            }
        ]

    async def test_attachment_loader_reports_unready_file_without_content(
        self, monkeypatch
    ):
        """Processing files are explicit and honest, never guessed from metadata."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from uuid import UUID

        from src.models.document import ProcessingStatus
        from src.services.agent._nodes_rag import _load_attachment_contexts

        user_id = UUID("44444444-4444-4444-8444-444444444444")
        organization_id = UUID("55555555-5555-4555-8555-555555555555")
        document_id = UUID("11111111-1111-4111-8111-111111111111")
        document = SimpleNamespace(
            id=document_id,
            title="Still uploading.txt",
            organization_id=organization_id,
            processing_status=ProcessingStatus.PROCESSING,
            content_text="secret text that must not be exposed yet",
            is_deleted=False,
        )

        class _Result:
            def scalars(self):
                return SimpleNamespace(all=lambda: [document])

        class _Session:
            async def execute(self, _statement):
                return _Result()

        class _SessionContext:
            async def __aenter__(self):
                return _Session()

            async def __aexit__(self, *_args):
                return False

        owned = AsyncMock(return_value=[document_id])
        monkeypatch.setattr(
            "src.services.agent.tool_session.tool_session",
            lambda: _SessionContext(),
        )
        monkeypatch.setattr(
            "src.services.agent.tool_session.resolve_tool_user",
            AsyncMock(return_value=SimpleNamespace(id=user_id)),
        )
        monkeypatch.setattr(
            "src.services.threads.workspace_access.filter_owned_document_ids", owned
        )

        contexts, statuses = await _load_attachment_contexts(
            attachment_ids=[str(document_id)],
            thread_id=None,
            user_id=str(user_id),
            organization_id=str(organization_id),
            query="What is the launch code?",
        )

        owned.assert_awaited_once()
        assert contexts == []
        assert statuses == [
            {
                "document_id": str(document_id),
                "status": "processing",
                "title": "Still uploading.txt",
            }
        ]

    async def test_explicit_attachment_lookup_failure_fails_closed_without_corpus_search(
        self, monkeypatch
    ):
        """A document-read outage must not widen an explicit source scope."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from uuid import UUID

        from langchain_core.messages import HumanMessage

        from src.services.agent import _nodes_rag

        user_id = UUID("44444444-4444-4444-8444-444444444444")
        organization_id = UUID("55555555-5555-4555-8555-555555555555")
        document_id = UUID("11111111-1111-4111-8111-111111111111")

        class _Session:
            async def execute(self, _statement):
                raise RuntimeError("database unavailable")

        class _SessionContext:
            async def __aenter__(self):
                return _Session()

            async def __aexit__(self, *_args):
                return False

        broad_search = AsyncMock(return_value=[{"content": "unrelated"}])
        monkeypatch.setattr(
            "src.services.agent.tool_session.tool_session",
            lambda: _SessionContext(),
        )
        monkeypatch.setattr(
            "src.services.agent.tool_session.resolve_tool_user",
            AsyncMock(return_value=SimpleNamespace(id=user_id)),
        )

        state = {
            "messages": [HumanMessage(content="What is the launch code?")],
            "page_context": {},
            "retrieved_contexts": [],
            "thread_id": "33333333-3333-4333-8333-333333333333",
            "attachment_ids": [str(document_id)],
            "use_rag": True,
        }
        config = {
            "configurable": {
                "user_id": str(user_id),
                "organization_id": str(organization_id),
                "search_fn": broad_search,
            }
        }

        result = await _nodes_rag.rag_node(state, config)

        broad_search.assert_not_awaited()
        assert result["retrieved_contexts"] == []
        assert result["attachment_status"] == [
            {"document_id": str(document_id), "status": "unavailable"}
        ]
