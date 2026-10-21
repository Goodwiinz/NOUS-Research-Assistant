"""Native attachment page selection with real SQLite authorization predicates."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.models.base import BaseModel
from src.models.chat_message import ChatMessage, MessageRole
from src.models.conversation import Conversation
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.message_attachment import MessageAttachment
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.alphaxiv import (
    AlphaXivChatFields,
    AlphaXivChatRequest,
    AlphaXivLiveChatRequest,
)
from src.services.agent import _nodes_rag
from src.services.compat.alphaxiv import prepare_request
from src.services.search.cohere_rerank_service import (
    RerankOutcome,
    RerankResult,
    cohere_rerank_service,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def pdf_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    models: list[type[BaseModel]] = [
        Organization,
        User,
        Workspace,
        WorkspaceMember,
        Conversation,
        Thread,
        Document,
        ChatMessage,
        MessageAttachment,
    ]
    async with engine.begin() as connection:
        for model in models:
            await connection.run_sync(model.__table__.create)
    make_session = async_sessionmaker(engine, expire_on_commit=False)
    env = SimpleNamespace(
        user=uuid4(),
        org=uuid4(),
        other_org=uuid4(),
        workspace=uuid4(),
        conversation=uuid4(),
        thread=uuid4(),
        document=uuid4(),
        make_session=make_session,
        open_sessions=0,
    )
    async with make_session() as session:
        await session.execute(
            insert(Organization).values(
                [
                    {"id": env.org, "name": "PDF", "storage_limit_bytes": 1000000},
                    {
                        "id": env.other_org,
                        "name": "Other",
                        "storage_limit_bytes": 1000000,
                    },
                ]
            )
        )
        await session.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, "
                "first_name, last_name, role, is_active, login_count, created_at, "
                "updated_at, is_deleted) VALUES (:id, :org, 'pdf@example.test', "
                "'unused', 'PDF', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, 0)"
            ),
            {"id": str(env.user), "org": str(env.org)},
        )
        await session.execute(
            insert(Workspace).values(
                id=env.workspace,
                name="Workspace",
                owner_id=env.user,
                organization_id=env.org,
            )
        )
        await session.execute(
            insert(Conversation).values(
                id=env.conversation,
                title="PDF",
                workspace_id=env.workspace,
                created_by_id=env.user,
            )
        )
        await session.execute(
            insert(Thread).values(
                id=env.thread,
                conversation_id=env.conversation,
                title="PDF",
            )
        )
        await session.execute(
            insert(Document).values(
                id=env.document,
                organization_id=env.org,
                title="PDF source",
                filename="paper.pdf",
                file_path="/tmp/paper.pdf",
                file_size_bytes=1,
                mime_type="application/pdf",
                document_type=DocumentType.PDF,
                processing_status=ProcessingStatus.COMPLETED,
                content_text="[Page 1]\nFirst source\n[Page 2]\nSecond source",
            )
        )
        message = uuid4()
        await session.execute(
            insert(ChatMessage).values(
                id=message,
                thread_id=env.thread,
                user_id=env.user,
                role=MessageRole.USER,
                content="Read the attached PDF",
            )
        )
        await session.execute(
            insert(MessageAttachment).values(
                message_id=message,
                document_id=env.document,
            )
        )
        await session.commit()

    @asynccontextmanager
    async def scoped_session() -> AsyncIterator[AsyncSession]:
        async with make_session() as session:
            env.open_sessions += 1
            try:
                yield session
            finally:
                env.open_sessions -= 1

    monkeypatch.setattr("src.services.agent.tool_session.tool_session", scoped_session)
    monkeypatch.setattr(settings, "AGENT_PAGE_FILTER", True)
    monkeypatch.setattr(settings, "RETRIEVE_PASSAGES_PAGE_SHORT_CIRCUIT", 8)
    monkeypatch.setattr(settings, "RETRIEVE_PASSAGES_PAGE_TOP_N", 6)
    monkeypatch.setattr(cohere_rerank_service, "_enabled", True)
    yield env
    await engine.dispose()


async def _change(
    env: SimpleNamespace,
    model: (
        type[Document]
        | type[User]
        | type[Workspace]
        | type[Conversation]
        | type[Thread]
    ) = Document,
    **values: Any,
) -> None:
    identity = {
        Document: env.document,
        User: env.user,
        Workspace: env.workspace,
        Conversation: env.conversation,
        Thread: env.thread,
    }[model]
    async with env.make_session() as session:
        await session.execute(
            update(model).where(model.id == identity).values(**values)
        )
        await session.commit()


async def _load(
    env: SimpleNamespace,
    *,
    implicit: bool = False,
    selected: tuple[int, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    metadata: dict[str, Any] = {}
    contexts, statuses = await _nodes_rag._load_attachment_contexts(
        attachment_ids=[] if implicit else [str(env.document)],
        thread_id=str(env.thread),
        user_id=str(env.user),
        organization_id=str(env.org),
        query="Explain the second source",
        selection_page_range=selected,
        page_filter_out=metadata,
    )
    return contexts, statuses, metadata


@pytest.mark.parametrize("implicit", [False, True])
async def test_native_short_pdf_returns_pages_and_honest_metadata(
    pdf_env: SimpleNamespace, implicit: bool
) -> None:
    contexts, statuses, metadata = await _load(pdf_env, implicit=implicit)
    assert [item["page_number"] for item in contexts] == [1, 2]
    assert [item["content"] for item in contexts] == ["First source", "Second source"]
    assert statuses[0]["status"] == "ready"
    assert metadata == {
        "document_id": str(pdf_env.document),
        "accepted_pages": [1, 2],
        "total_pages": 2,
        "short_circuited": True,
        "model": None,
    }


async def test_native_selected_range_is_zero_based_and_never_reranks(
    pdf_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "AGENT_PAGE_FILTER", False)
    provider = AsyncMock(
        side_effect=AssertionError("explicit selection is authoritative")
    )
    monkeypatch.setattr(cohere_rerank_service, "rerank_with_outcome", provider)
    contexts, _, metadata = await _load(pdf_env, selected=(1, 1))
    assert [item["content"] for item in contexts] == ["Second source"]
    assert contexts[0]["page_number"] == 2
    assert metadata["selection_page_range"] == [1, 1]
    provider.assert_not_awaited()


@pytest.mark.parametrize(
    "source,selected",
    [
        ("unmarked unrelated content", (0, 0)),
        ("[Page 1]\nfirst\n[Page 3]\nthird", (0, 2)),
        ("[Page 1]\nfirst\n[Page 2]\n ", (1, 1)),
        ("[Page 1]\nfirst", (1, 1)),
    ],
    ids=["unmarked", "gap", "blank", "out-of-range"],
)
async def test_invalid_or_unavailable_selection_cannot_widen(
    pdf_env: SimpleNamespace, source: str, selected: tuple[int, int]
) -> None:
    await _change(pdf_env, content_text=source)
    contexts, statuses, metadata = await _load(pdf_env, selected=selected)
    assert contexts == []
    assert statuses[0]["status"] == "unavailable"
    assert metadata == {}


async def test_selected_pages_beyond_first_hundred_remain_addressable(
    pdf_env: SimpleNamespace,
) -> None:
    await _change(
        pdf_env,
        content_text="\n".join(
            f"[Page {number}]\nsource {number}" for number in range(1, 125)
        ),
    )
    contexts, _, metadata = await _load(pdf_env, selected=(121, 122))
    assert [item["page_number"] for item in contexts] == [122, 123]
    assert metadata["total_pages"] == 124


def _long_thesis(filler: int) -> str:
    """300 physical pages; only page 250 answers the zymurgy question."""
    pad = "background discussion " * filler
    return "\n".join(
        f"[Page {number}]\n"
        + ("Chapter seven: zymurgy fermentation yields" if number == 250 else pad)
        for number in range(1, 301)
    )


@pytest.mark.parametrize("filler", [1, 200], ids=["under-1mib", "over-1mib"])
async def test_long_pdf_candidates_cover_pages_beyond_the_first_hundred(
    pdf_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, filler: int
) -> None:
    source = _long_thesis(filler)
    assert (len(source) > 1024 * 1024) is (filler == 200)
    await _change(pdf_env, content_text=source)
    seen: list[str] = []

    async def rerank(
        query: str, documents: list[dict[str, Any]], **kwargs: Any
    ) -> RerankOutcome:
        seen.extend(document["id"] for document in documents)
        return RerankOutcome(
            [
                RerankResult(document["id"], index, 0.9, 0)
                for index, document in enumerate(documents)
                if "zymurgy" in document["content"]
            ]
        )

    monkeypatch.setattr(cohere_rerank_service, "rerank_with_outcome", rerank)
    metadata: dict[str, Any] = {}
    contexts, statuses = await _nodes_rag._load_attachment_contexts(
        attachment_ids=[str(pdf_env.document)],
        thread_id=str(pdf_env.thread),
        user_id=str(pdf_env.user),
        organization_id=str(pdf_env.org),
        query="What does chapter seven say about zymurgy?",
        page_filter_out=metadata,
    )
    assert len(seen) == 100
    assert "250" in seen
    assert [item["page_number"] for item in contexts] == [250]
    assert "zymurgy" in contexts[0]["content"]
    assert statuses[0]["status"] == "ready"
    assert metadata["total_pages"] == 300


async def test_long_pdf_excerpt_fallback_searches_the_whole_document(
    pdf_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _change(pdf_env, content_text=_long_thesis(200))
    monkeypatch.setattr(
        cohere_rerank_service,
        "rerank_with_outcome",
        AsyncMock(side_effect=RuntimeError("provider unavailable")),
    )
    metadata: dict[str, Any] = {}
    contexts, statuses = await _nodes_rag._load_attachment_contexts(
        attachment_ids=[str(pdf_env.document)],
        thread_id=str(pdf_env.thread),
        user_id=str(pdf_env.user),
        organization_id=str(pdf_env.org),
        query="What does chapter seven say about zymurgy?",
        page_filter_out=metadata,
    )
    assert len(contexts) == 1
    assert "zymurgy" in contexts[0]["content"]
    assert statuses[0]["status"] == "ready"


async def test_native_reranks_without_database_session_and_preserves_scores(
    pdf_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _change(
        pdf_env,
        content_text="\n".join(
            f"[Page {number}]\nsource {number}" for number in range(1, 10)
        ),
    )

    async def rerank(
        query: str, documents: list[dict[str, Any]], **kwargs: Any
    ) -> RerankOutcome:
        assert pdf_env.open_sessions == 0
        assert len(documents) == 9
        assert kwargs["top_n"] == 6
        return RerankOutcome(
            [
                RerankResult("8", 7, 0.9, 0),
                RerankResult("2", 1, 0.8, 0),
            ]
        )

    monkeypatch.setattr(cohere_rerank_service, "rerank_with_outcome", rerank)
    contexts, _, metadata = await _load(pdf_env)
    assert [item["page_number"] for item in contexts] == [2, 8]
    assert [item["score"] for item in contexts] == [0.8, 0.9]
    assert metadata["priorities"] == [
        {"page": 2, "score": 0.8},
        {"page": 8, "score": 0.9},
    ]
    assert metadata["timing_ms"]["count"] == 1
    assert metadata["timing_ms"]["average"] >= 0


# Mutation verified: removing the fresh authorization/readiness block at
# src/services/agent/_nodes_rag.py:606 makes all 12 cases expose revoked content.
# From repo root: python -m pytest -q
# backend/tests/unit/services/test_agent_pdf_pages.py
# -k native_rechecks_authority_after_provider
# The exact original block was restored before rerunning the passing suite.
@pytest.mark.parametrize(
    "revoked",
    ["document", "organization", "user", "thread", "conversation", "workspace"],
)
@pytest.mark.parametrize("provider_fails", [False, True])
async def test_native_rechecks_authority_after_provider(
    pdf_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    revoked: str,
    provider_fails: bool,
) -> None:
    await _change(
        pdf_env,
        content_text="\n".join(
            f"[Page {number}]\nsecret {number}" for number in range(1, 10)
        ),
    )

    async def rerank(*args: Any, **kwargs: Any) -> RerankOutcome:
        if revoked == "organization":
            await _change(pdf_env, organization_id=pdf_env.other_org)
        elif revoked == "user":
            await _change(pdf_env, User, is_active=False)
        else:
            model = {
                "document": Document,
                "thread": Thread,
                "conversation": Conversation,
                "workspace": Workspace,
            }[revoked]
            await _change(pdf_env, model, is_deleted=True)
        if provider_fails:
            raise RuntimeError("provider failed after revocation")
        return RerankOutcome([RerankResult("1", 0, 0.9, 0)])

    monkeypatch.setattr(cohere_rerank_service, "rerank_with_outcome", rerank)
    contexts, statuses, metadata = await _load(pdf_env, implicit=True)
    assert contexts == []
    assert statuses[0]["status"] == "unavailable"
    assert metadata == {}


async def test_native_page_budget_preserves_all_selected_identities(
    pdf_env: SimpleNamespace,
) -> None:
    await _change(
        pdf_env,
        title="😀" * 500,
        content_text="\n".join(
            f"[Page {number}]\n" + "漢😀" * 1000 for number in range(1, 101)
        ),
    )
    contexts, _, metadata = await _load(pdf_env, selected=(0, 99))
    assert [item["page_number"] for item in contexts] == list(range(1, 101))
    assert (
        len(json.dumps({"contexts": contexts, "page_filter": metadata}).encode())
        <= 64 * 1024
    )
    assert any(item["truncated"] for item in contexts)


async def test_flag_off_preserves_existing_attachment_excerpt(
    pdf_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "AGENT_PAGE_FILTER", False)
    contexts, _, metadata = await _load(pdf_env)
    assert contexts[0]["content"] == "[Page 1]\nFirst source\n[Page 2]\nSecond source"
    assert "page_number" not in contexts[0]
    assert metadata == {}


async def test_multiple_attachment_scope_keeps_existing_excerpt_path(
    pdf_env: SimpleNamespace,
) -> None:
    metadata: dict[str, Any] = {}
    contexts, statuses = await _nodes_rag._load_attachment_contexts(
        attachment_ids=[str(pdf_env.document), str(uuid4())],
        thread_id=str(pdf_env.thread),
        user_id=str(pdf_env.user),
        organization_id=str(pdf_env.org),
        query="Explain the paper",
        page_filter_out=metadata,
    )
    assert len(contexts) == 1
    assert "page_number" not in contexts[0]
    assert [status["status"] for status in statuses] == ["ready", "unavailable"]
    assert metadata == {}


async def test_foreign_document_never_reaches_page_provider(
    pdf_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _change(pdf_env, organization_id=pdf_env.other_org)
    provider = AsyncMock(
        side_effect=AssertionError("foreign content must stay private")
    )
    monkeypatch.setattr(cohere_rerank_service, "rerank_with_outcome", provider)
    contexts, statuses, metadata = await _load(pdf_env)
    assert contexts == []
    assert statuses[0]["status"] == "unavailable"
    assert metadata == {}
    provider.assert_not_awaited()


async def test_provider_failure_falls_back_only_inside_selected_document(
    pdf_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _change(
        pdf_env,
        content_text="\n".join(
            f"[Page {number}]\nsource {number}" for number in range(1, 10)
        ),
    )
    provider = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    monkeypatch.setattr(cohere_rerank_service, "rerank_with_outcome", provider)
    contexts, statuses, metadata = await _load(pdf_env)
    assert len(contexts) == 1
    assert contexts[0]["document_id"] == str(pdf_env.document)
    assert contexts[0]["score_source"] == "attachment"
    assert "page_number" not in contexts[0]
    assert statuses[0]["status"] == "ready"
    assert metadata == {}


async def test_native_rag_update_carries_selected_page_provenance(
    pdf_env: SimpleNamespace,
) -> None:
    result = await _nodes_rag.rag_node(
        {
            "messages": [HumanMessage(content="Explain the selected page")],
            "attachment_ids": [str(pdf_env.document)],
            "selection_page_range": (1, 1),
            "document_only": True,
            "use_rag": True,
        },
        {
            "configurable": {
                "user_id": str(pdf_env.user),
                "organization_id": str(pdf_env.org),
                "thread_id": str(pdf_env.thread),
            }
        },
    )
    assert result["retrieved_contexts"][0]["page_number"] == 2
    assert result["page_filter"]["accepted_pages"] == [2]
    assert result["page_filter"]["document_id"] == str(pdf_env.document)


async def test_document_only_without_pdf_never_searches_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broad = AsyncMock(side_effect=AssertionError("must not search outside PDF scope"))
    monkeypatch.setattr(_nodes_rag, "_try_primary_do_kb_read", broad)
    monkeypatch.setattr(_nodes_rag, "_legacy_hybrid_search_fallback", broad)
    result = await _nodes_rag.rag_node(
        {
            "messages": [HumanMessage(content="Find the relevant research")],
            "document_only": True,
            "use_rag": True,
        },
        {"configurable": {}},
    )
    assert result["retrieved_contexts"] == []
    assert result["attachment_status"] == [{"status": "unavailable"}]
    broad.assert_not_awaited()


async def test_explicit_cohere_filter_runs_with_feature_default_disabled(
    pdf_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "AGENT_PAGE_FILTER", False)
    monkeypatch.setattr(settings, "RETRIEVE_PASSAGES_PAGE_SHORT_CIRCUIT", 0)
    provider = AsyncMock(return_value=RerankOutcome([RerankResult("2", 1, 0.9, 0)]))
    monkeypatch.setattr(cohere_rerank_service, "rerank_with_outcome", provider)
    result = await _nodes_rag.rag_node(
        {
            "messages": [HumanMessage(content="Explain the second source")],
            "attachment_ids": [str(pdf_env.document)],
            "page_filter_model": "cohere",
        },
        {
            "configurable": {
                "user_id": str(pdf_env.user),
                "organization_id": str(pdf_env.org),
                "thread_id": str(pdf_env.thread),
            }
        },
    )
    provider.assert_awaited_once()
    assert [item["page_number"] for item in result["retrieved_contexts"]] == [2]
    assert result["page_filter"]["accepted_pages"] == [2]


@pytest.mark.parametrize("request_type", [AlphaXivChatRequest, AlphaXivLiveChatRequest])
@pytest.mark.parametrize("model", ["", "pro"])
@pytest.mark.parametrize("page_range", [None, [1, 1]])
async def test_compat_defaults_reach_native_page_selection_with_global_filter_off(
    pdf_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    request_type: type[AlphaXivChatFields],
    model: str,
    page_range: list[int] | None,
) -> None:
    await _change(
        pdf_env,
        content_text="\n".join(
            f"[Page {number}]\nPhysical page {number} evidence"
            for number in range(1, 10)
        ),
    )
    monkeypatch.setattr(settings, "AGENT_PAGE_FILTER", False)
    provider = AsyncMock(return_value=RerankOutcome([RerankResult("9", 8, 0.91, 0)]))
    monkeypatch.setattr(cohere_rerank_service, "rerank_with_outcome", provider)
    body = request_type.model_validate(
        {
            "message": "> Physical\n\nExplain the ninth page evidence.",
            "paperVersionId": pdf_env.document,
            "model": model,
            "files": [],
            "thinking": None,
            "webSearch": "off",
            "selectionPageRange": page_range,
            "assistantVariant": "paper",
        }
    )
    async with pdf_env.make_session() as db:
        user = await db.get(User, pdf_env.user)
        assert user is not None
        native = await prepare_request(body, db, user)
    assert native.attachment_ids is not None
    # Real access preflight and request mapping feed the actual native RAG node.
    # Only the external ranker's result is controlled; no answer model is called.
    result = await _nodes_rag.rag_node(
        {
            "messages": [HumanMessage(content=native.messages[0].content)],
            "attachment_ids": [str(item) for item in native.attachment_ids],
            "branch_document_id": str(native.branch_document_id),
            "selection_page_range": native.selection_page_range,
            "page_filter_model": native.page_filter_model,
            "document_only": native.document_only,
            "use_rag": native.use_rag,
        },
        {
            "configurable": {
                "user_id": str(pdf_env.user),
                "organization_id": str(pdf_env.org),
                "thread_id": str(pdf_env.thread),
            }
        },
    )
    expected_page = 2 if page_range else 9
    assert [item["page_number"] for item in result["retrieved_contexts"]] == [
        expected_page
    ]
    assert (
        result["retrieved_contexts"][0]["content"]
        == f"Physical page {expected_page} evidence"
    )
    assert result["page_filter"]["accepted_pages"] == [expected_page]
    assert result["page_filter"]["document_id"] == str(pdf_env.document)
    assert result["page_filter"]["total_pages"] == 9
    assert settings.AGENT_PAGE_FILTER is False
    assert body.model == model
    if page_range:
        provider.assert_not_awaited()
        assert native.page_filter_model is None
        assert result["page_filter"]["model"] is None
        assert "timing_ms" not in result["page_filter"]
    else:
        provider.assert_awaited_once()
        assert provider.await_args is not None
        assert len(provider.await_args.kwargs["documents"]) == 9
        assert result["page_filter"]["priorities"] == [{"page": 9, "score": 0.91}]
        assert result["page_filter"]["timing_ms"]["count"] == 1


@pytest.mark.parametrize("revoke_primary", [False, True])
@pytest.mark.parametrize("large_extra", [False, True])
async def test_bound_selection_and_extra_pdf_have_independent_ranges_and_authority(
    pdf_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    revoke_primary: bool,
    large_extra: bool,
) -> None:
    extra = uuid4()
    extra_source = (
        "Supplementary source " + "x" * (1024 * 1024) + " far-tail-sentinel"
        if large_extra
        else "[Page 1]\nSupplementary evidence"
    )
    async with pdf_env.make_session() as session:
        await session.execute(
            insert(Document).values(
                id=extra,
                organization_id=pdf_env.org,
                title="Supplementary PDF",
                filename="extra.pdf",
                file_path="/tmp/extra.pdf",
                file_size_bytes=1,
                mime_type="application/pdf",
                document_type=DocumentType.PDF,
                processing_status=ProcessingStatus.COMPLETED,
                content_text=extra_source,
            )
        )
        await session.commit()
    monkeypatch.setattr(settings, "RETRIEVE_PASSAGES_PAGE_SHORT_CIRCUIT", 0)

    original_ready = _nodes_rag._attachment_scope_still_ready

    async def reauthorize(*args: Any, **kwargs: Any) -> bool:
        # Revoke after both content reads, then execute the real final guard.
        if revoke_primary:
            await _change(pdf_env, is_deleted=True)
        return await original_ready(*args, **kwargs)

    monkeypatch.setattr(_nodes_rag, "_attachment_scope_still_ready", reauthorize)
    provider = AsyncMock(
        side_effect=AssertionError(
            "Neither explicit pages nor supplementary files should rerank"
        )
    )
    monkeypatch.setattr(cohere_rerank_service, "rerank_with_outcome", provider)
    metadata: dict[str, Any] = {}
    contexts, statuses = await _nodes_rag._load_attachment_contexts(
        attachment_ids=[str(pdf_env.document), str(extra)],
        branch_document_id=str(pdf_env.document),
        thread_id=str(pdf_env.thread),
        user_id=str(pdf_env.user),
        organization_id=str(pdf_env.org),
        query="Explain far-tail-sentinel",
        selection_page_range=(1, 1),
        page_filter_model="cohere",
        page_filter_out=metadata,
    )
    if revoke_primary:
        assert contexts == []
        assert metadata == {}
        assert all(item["status"] == "unavailable" for item in statuses)
    else:
        assert [
            (item["document_id"], item.get("page_number")) for item in contexts
        ] == [
            (str(pdf_env.document), 2),
            (str(extra), None),
        ]
        if large_extra:
            assert "far-tail-sentinel" not in contexts[1]["content"]
            assert contexts[1]["content"].startswith("Supplementary source")
        else:
            assert contexts[1]["content"] == extra_source
        assert metadata["document_id"] == str(pdf_env.document)
        assert metadata["accepted_pages"] == [2]
        assert (
            len(json.dumps({"contexts": contexts, "page_filter": metadata}).encode())
            <= 64 * 1024
        )
    provider.assert_not_awaited()


@pytest.mark.parametrize("unavailable", [False, True])
async def test_branch_preprocessing_cannot_answer_after_attachment_becomes_unavailable(
    pdf_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, unavailable: bool
) -> None:
    from src.services.agent import _nodes_classify
    from src.services.threads.message_branches import BranchAccessError

    monkeypatch.setattr(
        _nodes_classify, "_classify_core", AsyncMock(return_value={"intent": "general"})
    )
    monkeypatch.setattr(
        _nodes_classify,
        "memory_retrieval_node",
        AsyncMock(return_value={"user_memories": []}),
    )
    if unavailable:
        await _change(pdf_env, processing_status=ProcessingStatus.FAILED)
    state = {
        "messages": [HumanMessage(content="Explain this selected page")],
        "attachment_ids": [str(pdf_env.document)],
        "branch_document_id": str(pdf_env.document),
        "selection_page_range": (1, 1),
        "document_only": True,
    }
    config = {
        "configurable": {
            "user_id": str(pdf_env.user),
            "organization_id": str(pdf_env.org),
            "thread_id": str(pdf_env.thread),
        }
    }
    if unavailable:
        with pytest.raises(BranchAccessError, match="PDF chat files are unavailable"):
            await _nodes_classify.preprocessing_node(state, config)
    else:
        result = await _nodes_classify.preprocessing_node(state, config)
        assert result["retrieved_contexts"][0]["content"] == "Second source"
