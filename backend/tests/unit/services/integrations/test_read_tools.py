"""Scoped integration read gateway, using a local SQLite database."""

import json
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.collection import Collection, CollectionDocument
from src.models.document import Document, DocumentType
from src.models.generated_draft import GeneratedDraft
from src.models.graph import EntityType
from src.models.organization import Organization
from src.models.research_project import ResearchProject
from src.models.research_project_role import ResearchProjectRoleAssignment
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_tools import ToolInvocation
from src.services.integrations import read_tools
from src.services.integrations.context import IntegrationAccessDenied
from src.services.integrations.read_tools import (
    READ_TOOL_NAMES,
    ToolArgumentError,
    invoke_read,
    list_read_tools,
)

pytestmark = pytest.mark.unit
USER, ORG, PROJECT, WORKSPACE, OTHER_PROJECT = (uuid4() for _ in range(5))
DOC_IN_PROJECT, DOC_OUTSIDE_PROJECT = uuid4(), uuid4()


def _document_row(document_id: UUID, title: str) -> dict[str, Any]:
    return dict(
        id=document_id,
        organization_id=ORG,
        title=title,
        filename=f"{title}.txt",
        file_path=f"/tmp/{title}.txt",
        file_size_bytes=1,
        mime_type="text/plain",
        document_type=DocumentType.TEXT,
    )


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables: list[Any] = [
        Organization,
        User,
        Workspace,
        WorkspaceMember,
        Collection,
        Document,
        CollectionDocument,
        GeneratedDraft,
        # Project authorization reads these even when no engine is enabled.
        ResearchProject,
        ResearchProjectRoleAssignment,
    ]
    async with engine.begin() as conn:
        for model in tables:
            await conn.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await session.execute(
            insert(Organization).values(
                id=ORG, name="Test", storage_limit_bytes=1000000
            )
        )
        await session.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, 'integration@example.test', 'unused', 'Test', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
            ),
            {"id": str(USER), "org": str(ORG)},
        )
        await session.execute(
            insert(Workspace).values(
                id=WORKSPACE, name="Workspace", owner_id=USER, organization_id=ORG
            )
        )
        await session.execute(
            insert(Collection).values(
                [
                    dict(id=PROJECT, name="Project", workspace_id=WORKSPACE),
                    dict(id=OTHER_PROJECT, name="Other", workspace_id=WORKSPACE),
                ]
            )
        )
        await session.execute(
            insert(Document).values(
                [
                    _document_row(DOC_IN_PROJECT, "retrieval-eval"),
                    _document_row(DOC_OUTSIDE_PROJECT, "retrieval-secret"),
                ]
            )
        )
        await session.execute(
            insert(CollectionDocument).values(
                [
                    dict(collection_id=PROJECT, document_id=DOC_IN_PROJECT),
                    dict(collection_id=OTHER_PROJECT, document_id=DOC_OUTSIDE_PROJECT),
                ]
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture
def context() -> IntegrationContext:
    return IntegrationContext(
        user_id=USER, organization_id=ORG, project_id=PROJECT, grant_id=uuid4()
    )


def _invocation(tool_name: str, **arguments: Any) -> ToolInvocation:
    return ToolInvocation(
        tool_name=tool_name, arguments=arguments, invocation_id=uuid4()
    )


def test_catalog_advertises_only_the_read_allowlist_without_identity_args() -> None:
    catalog = list_read_tools()
    names = {tool.name for tool in catalog}
    assert names == set(READ_TOOL_NAMES) | set(read_tools.LOCAL_TOOLS)
    assert set(read_tools.TOOL_SCOPES) == names
    assert set(read_tools.TOOL_SCOPES.values()) == {"tools:read"}
    assert "execute_code" not in names and "forget_memory" not in names
    for tool in catalog:
        assert "project_id" not in tool.input_schema.get("properties", {})


@pytest.mark.parametrize(
    "invocation",
    [
        _invocation("execute_code", code="print(1)"),
        _invocation("list_project_documents", project_id=str(PROJECT)),
        _invocation("search_documents", query="x", organization_id=str(ORG)),
        _invocation("search_documents", query="x", unexpected="y"),
        _invocation("search_documents"),
    ],
    ids=["unknown-tool", "identity-project", "identity-org", "extra-arg", "missing"],
)
async def test_rejected_invocations_never_reach_adapters(
    db: AsyncSession,
    context: IntegrationContext,
    invocation: ToolInvocation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = AsyncMock()
    for name in ("_tool_list_project_documents", "_tool_do_kb_retrieve"):
        monkeypatch.setattr(read_tools, name, adapter)
    with pytest.raises(ToolArgumentError):
        await invoke_read(db, context, invocation)
    adapter.assert_not_awaited()


async def test_search_is_project_scoped(
    db: AsyncSession, context: IntegrationContext
) -> None:
    result = await invoke_read(
        db, context, _invocation("search_documents", query="retrieval")
    )
    assert result.is_error is False
    assert [doc["id"] for doc in result.content[0]["documents"]] == [
        str(DOC_IN_PROJECT)
    ]
    assert result.source_refs == [{"document_id": str(DOC_IN_PROJECT)}]


async def test_list_injects_grant_project(
    db: AsyncSession, context: IntegrationContext
) -> None:
    result = await invoke_read(db, context, _invocation("list_project_documents"))
    assert result.is_error is False
    assert result.content[0]["project_name"] == "Project"
    assert result.source_refs == [{"document_id": str(DOC_IN_PROJECT)}]


@pytest.mark.parametrize(
    "document_ids,rejected_as_argument",
    [
        ([], True),
        (["not-a-uuid"], True),
        ([str(uuid4())] * 21, True),
        ([str(DOC_OUTSIDE_PROJECT)], False),
        ([str(DOC_IN_PROJECT), str(uuid4())], False),
    ],
    ids=["empty", "malformed", "too-many", "foreign-project", "unknown"],
)
async def test_selected_document_scope_never_broadens(
    db: AsyncSession,
    context: IntegrationContext,
    document_ids: list[str],
    rejected_as_argument: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieval = AsyncMock()
    monkeypatch.setattr(read_tools, "_tool_do_kb_retrieve", retrieval)
    invocation = _invocation("do_kb_retrieve", query="q", document_ids=document_ids)
    if rejected_as_argument:
        with pytest.raises(ToolArgumentError):
            await invoke_read(db, context, invocation)
    else:
        # Same shape as tools_impl's scoped_limitation: foreign and unknown
        # ids are indistinguishable, so membership cannot be probed.
        result = await invoke_read(db, context, invocation)
        assert result.is_error is True
        assert result.content[0]["reason"] == "requested_documents_unavailable"
        assert result.source_refs == []
    retrieval.assert_not_awaited()


def _real_chunk(document_id: str | None, text: str) -> dict[str, Any]:
    # Mirrors tools_impl._tool_do_kb_retrieve's chunks_payload shape.
    return {
        "text": text,
        "score": 0.5,
        "score_source": "upstream",
        "document_id": document_id,
        "title": "Doc",
        "metadata": {"item_name": "internal-storage-key.txt"},
    }


async def test_valid_retrieval_preserves_source_identity(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = AsyncMock(
        return_value={
            "chunks": [
                _real_chunk(str(DOC_IN_PROJECT), "hit"),
                _real_chunk(None, "unresolved"),
            ],
            "total": 2,
        }
    )
    monkeypatch.setattr(read_tools, "_tool_do_kb_retrieve", retrieval)
    result = await invoke_read(
        db,
        context,
        _invocation("do_kb_retrieve", query="q", document_ids=[str(DOC_IN_PROJECT)]),
    )
    assert result.is_error is False
    # Only observed identities; an unresolved chunk yields no reference.
    assert result.source_refs == [{"document_id": str(DOC_IN_PROJECT)}]
    # Provider metadata (storage keys) never leaves the gateway.
    assert all("metadata" not in chunk for chunk in result.content[0]["chunks"])
    assert retrieval.await_args is not None
    args = retrieval.await_args.args[0]
    assert args["project_id"] == str(PROJECT)
    assert args["document_ids"] == [str(DOC_IN_PROJECT)]
    # Omitted top_k uses the advertised (registry) default, not a private one.
    catalog = {tool.name: tool.input_schema for tool in list_read_tools()}
    assert args["top_k"] == catalog["do_kb_retrieve"]["properties"]["top_k"]["default"]


async def test_size_cap_measures_wire_utf8_not_ascii_escapes(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 11k CJK chars: ~33 KiB UTF-8 on the wire, ~66 KiB when ASCII-escaped.
    cjk = "\u6587" * 11_000
    monkeypatch.setattr(
        read_tools,
        "_tool_list_project_documents",
        AsyncMock(
            return_value={"documents": [{"id": str(DOC_IN_PROJECT), "title": cjk}]}
        ),
    )
    result = await invoke_read(db, context, _invocation("list_project_documents"))
    assert result.is_error is False


@pytest.mark.parametrize("model", [Collection, Workspace])
async def test_search_query_itself_rechecks_ancestors(
    db: AsyncSession, context: IntegrationContext, model: Any
) -> None:
    # Ancestor soft-deleted between invoke_read's ownership check and the
    # fetch: the fetch alone must return nothing.
    await db.execute(update(model).values(is_deleted=True))
    await db.commit()
    payload = await read_tools._search_project_documents(db, context, "retrieval", 10)
    assert payload["documents"] == []
    assert (
        await read_tools._project_document_ids(db, context, [str(DOC_IN_PROJECT)])
        is None
    )


async def test_scoped_limitation_from_tool_is_error(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        read_tools,
        "_tool_do_kb_retrieve",
        AsyncMock(
            return_value={
                "chunks": [],
                "total": 0,
                "source": "do_kb",
                "reason": "scoped_retrieval_unavailable",
                "evidence_mode": False,
            }
        ),
    )
    result = await invoke_read(
        db,
        context,
        _invocation("do_kb_retrieve", query="q", document_ids=[str(DOC_IN_PROJECT)]),
    )
    assert result.is_error is True
    assert result.source_refs == []


@pytest.mark.parametrize(
    "invocation",
    [
        _invocation("search_documents", query={"$gt": ""}),
        _invocation("search_documents", query="x", max_results=float("inf")),
        _invocation("get_current_draft", include_content="false"),
        _invocation("do_kb_retrieve", query="q"),
    ],
    ids=["dict-query", "infinite-limit", "string-bool", "retrieve-without-ids"],
)
async def test_wrongly_typed_arguments_are_rejected(
    db: AsyncSession, context: IntegrationContext, invocation: ToolInvocation
) -> None:
    with pytest.raises(ToolArgumentError):
        await invoke_read(db, context, invocation)


def test_catalog_bounds_match_enforced_limits() -> None:
    by_name = {tool.name: tool.input_schema for tool in list_read_tools()}
    assert by_name["list_project_documents"]["properties"]["limit"]["maximum"] == 50
    assert by_name["search_documents"]["properties"]["max_results"]["maximum"] == 50
    assert "document_ids" in by_name["do_kb_retrieve"]["required"]
    assert by_name["do_kb_retrieve"]["properties"]["document_ids"]["maxItems"] == 20


async def test_current_draft_reports_draft_id(
    db: AsyncSession, context: IntegrationContext
) -> None:
    draft_id = uuid4()
    await db.execute(
        insert(GeneratedDraft).values(
            id=draft_id, project_id=PROJECT, version=1, title="Draft", content="body"
        )
    )
    await db.commit()
    result = await invoke_read(db, context, _invocation("get_current_draft"))
    assert result.is_error is False
    assert result.content[0]["draft"]["id"] == str(draft_id)
    assert "content" not in result.content[0]["draft"]
    assert result.source_refs == [{"draft_id": str(draft_id)}]


async def test_large_draft_content_is_truncated_not_failed(
    db: AsyncSession, context: IntegrationContext
) -> None:
    await db.execute(
        insert(GeneratedDraft).values(
            id=uuid4(),
            project_id=PROJECT,
            version=1,
            title="Big",
            content="x" * 100_000,
        )
    )
    await db.commit()
    result = await invoke_read(
        db, context, _invocation("get_current_draft", include_content=True)
    )
    assert result.is_error is False
    draft = result.content[0]["draft"]
    assert draft["content_truncated"] is True
    assert 0 < len(draft["content"]) < 100_000


async def test_oversized_result_is_structured_error(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        read_tools,
        "_tool_list_project_documents",
        AsyncMock(return_value={"documents": [{"id": "x" * 70_000}]}),
    )
    result = await invoke_read(db, context, _invocation("list_project_documents"))
    assert result.is_error is True
    assert result.content == [{"error": "result_too_large"}]
    assert result.source_refs == []


@pytest.mark.parametrize("model", [Collection, Workspace])
async def test_deleted_ancestor_denies_read(
    db: AsyncSession, context: IntegrationContext, model: Any
) -> None:
    await db.execute(update(model).values(is_deleted=True))
    await db.commit()
    result = await invoke_read(db, context, _invocation("list_project_documents"))
    assert result.is_error is True
    assert result.source_refs == []


async def test_foreign_user_in_context_is_denied(db: AsyncSession) -> None:
    context = IntegrationContext(
        user_id=uuid4(), organization_id=ORG, project_id=PROJECT, grant_id=uuid4()
    )
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(db, context, _invocation("list_project_documents"))


# ---------------------------------------------------------------------------
# Plan 07 slice 1: args-only primitives, document content, passages
# ---------------------------------------------------------------------------


def test_catalog_lists_plan07_tools_without_identity_args() -> None:
    by_name = {tool.name: tool.input_schema for tool in list_read_tools()}
    assert {
        "search_arxiv",
        "search_external_database",
        "list_external_databases",
        "get_document_content",
        "retrieve_passages",
    } <= set(by_name)
    for schema in by_name.values():
        assert not read_tools.IDENTITY_ARGUMENTS & set(schema["properties"])
        assert schema["additionalProperties"] is False
    assert by_name["search_arxiv"]["properties"]["max_results"]["maximum"] == 20
    assert (
        by_name["search_external_database"]["properties"]["max_results"]["maximum"]
        == 20
    )
    assert by_name["retrieve_passages"]["properties"]["top_k"]["maximum"] == 20
    assert by_name["retrieve_passages"]["properties"]["document_ids"]["maxItems"] == 20
    assert by_name["get_document_content"]["properties"]["limit"]["maximum"] == 48_000
    assert by_name["get_document_content"]["required"] == ["document_id"]


async def test_search_arxiv_is_dispatched_with_clamped_results(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    arxiv = AsyncMock(return_value={"papers": [{"id": "2401.00001"}]})
    monkeypatch.setattr(read_tools, "_tool_search_arxiv", arxiv)
    # Args-only: must not touch project ownership at all.
    ownership = AsyncMock()
    monkeypatch.setattr(read_tools, "_verify_project_ownership", ownership)
    result = await invoke_read(
        db, context, _invocation("search_arxiv", query="llm", max_results=99)
    )
    assert result.is_error is False
    assert result.source_refs == [{"arxiv_id": "2401.00001"}]
    assert arxiv.await_args is not None
    assert arxiv.await_args.args[0]["max_results"] == 20
    assert "project_id" not in arxiv.await_args.args[0]
    ownership.assert_not_awaited()


async def test_search_arxiv_timeout_is_stable_error(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    async def never(_args: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {}

    monkeypatch.setattr(read_tools, "_tool_search_arxiv", never)
    monkeypatch.setattr(read_tools, "_search_arxiv_budget_seconds", lambda: 0.01)
    result = await invoke_read(db, context, _invocation("search_arxiv", query="q"))
    assert result.is_error is True
    assert result.content == [{"error": "upstream_timeout"}]


async def test_search_arxiv_budget_is_the_agent_slow_timeout() -> None:
    from src.services.agent._nodes_tools import _SLOW_TOOL_TIMEOUT_SECONDS

    assert read_tools._search_arxiv_budget_seconds() == _SLOW_TOOL_TIMEOUT_SECONDS


async def test_connector_tools_pass_args_only(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    search = AsyncMock(
        return_value={
            "total_results": 1,
            "results": [{"id": "P04637", "source": "uniprot"}],
            "connectors_searched": ["uniprot"],
        }
    )
    listing = AsyncMock(return_value={"total": 0, "connectors": []})
    monkeypatch.setattr(read_tools, "_tool_search_external_database", search)
    monkeypatch.setattr(read_tools, "_tool_list_external_databases", listing)
    searched = await invoke_read(
        db,
        context,
        _invocation("search_external_database", query="p53", max_results=40),
    )
    listed = await invoke_read(db, context, _invocation("list_external_databases"))
    assert searched.is_error is False and listed.is_error is False
    assert searched.source_refs == [{"external_id": "P04637", "connector": "uniprot"}]
    assert search.await_args is not None
    assert search.await_args.args[0] == {"query": "p53", "max_results": 20}
    assert listing.await_args is not None
    assert listing.await_args.args[0] == {}


async def test_external_tool_crash_never_leaks_exception_text(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        read_tools,
        "_tool_search_external_database",
        AsyncMock(side_effect=RuntimeError("api key sk-secret")),
    )
    result = await invoke_read(
        db, context, _invocation("search_external_database", query="q")
    )
    assert result.is_error is True
    assert "sk-secret" not in json.dumps(result.content)


async def _set_content(db: AsyncSession, document_id: UUID, text_: str) -> None:
    await db.execute(
        update(Document)
        .where(Document.id == document_id)
        .values(content_text=text_, content_summary="sum")
    )
    await db.commit()


@pytest.mark.parametrize(
    "document_id", [DOC_OUTSIDE_PROJECT, uuid4()], ids=["foreign-project", "unknown"]
)
async def test_document_content_refuses_documents_outside_the_grant(
    db: AsyncSession, context: IntegrationContext, document_id: UUID
) -> None:
    await _set_content(db, DOC_OUTSIDE_PROJECT, "secret body")
    result = await invoke_read(
        db,
        context,
        _invocation("get_document_content", document_id=str(document_id), mode="full"),
    )
    assert result.is_error is True
    assert result.content[0]["reason"] == "requested_documents_unavailable"
    assert "secret" not in json.dumps(result.content)
    assert result.source_refs == []


async def test_document_content_paginates_full_text(
    db: AsyncSession, context: IntegrationContext
) -> None:
    await _set_content(db, DOC_IN_PROJECT, "0123456789")
    first = await invoke_read(
        db,
        context,
        _invocation(
            "get_document_content",
            document_id=str(DOC_IN_PROJECT),
            mode="full",
            limit=4,
        ),
    )
    assert first.is_error is False
    page = first.content[0]
    assert page["text"] == "0123"
    assert page["next_offset"] == 4
    assert page["total_chars"] == 10
    assert page["mode"] == "full"
    assert page["title"] == "retrieval-eval"
    assert page["source_refs"] == [{"document_id": str(DOC_IN_PROJECT)}]
    assert first.source_refs == [{"document_id": str(DOC_IN_PROJECT)}]
    last = await invoke_read(
        db,
        context,
        _invocation(
            "get_document_content",
            document_id=str(DOC_IN_PROJECT),
            mode="full",
            offset=8,
            limit=4,
        ),
    )
    assert last.content[0]["text"] == "89"
    assert last.content[0]["next_offset"] is None
    summary = await invoke_read(
        db,
        context,
        _invocation("get_document_content", document_id=str(DOC_IN_PROJECT)),
    )
    assert summary.content[0]["mode"] == "summary"
    assert summary.content[0]["text"] == "sum"
    assert summary.content[0]["next_offset"] is None


async def test_document_content_stays_under_the_result_cap(
    db: AsyncSession, context: IntegrationContext
) -> None:
    # 3-byte UTF-8 chars: 48k chars would be 144 KiB on the wire.
    await _set_content(db, DOC_IN_PROJECT, "文" * 100_000)
    result = await invoke_read(
        db,
        context,
        _invocation(
            "get_document_content", document_id=str(DOC_IN_PROJECT), mode="full"
        ),
    )
    assert result.is_error is False
    page = result.content[0]
    wire = json.dumps(page, ensure_ascii=False).encode()
    assert len(wire) <= read_tools.MAX_RESULT_BYTES
    assert page["next_offset"] == len(page["text"]) > 0


@pytest.mark.parametrize(
    "invocation",
    [
        _invocation("get_document_content", document_id="nope"),
        _invocation("get_document_content", document_id=str(DOC_IN_PROJECT), mode="x"),
        _invocation(
            "get_document_content", document_id=str(DOC_IN_PROJECT), limit=48_001
        ),
        _invocation("retrieve_passages", query="q", document_ids=[]),
        _invocation("retrieve_passages", query="q", top_k=21),
    ],
    ids=["bad-uuid", "bad-mode", "limit-too-big", "empty-ids", "top-k-too-big"],
)
async def test_plan07_argument_shapes_are_rejected(
    db: AsyncSession, context: IntegrationContext, invocation: ToolInvocation
) -> None:
    with pytest.raises(ToolArgumentError):
        await invoke_read(db, context, invocation)


class _StubFulltext:
    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    def search(self, **kwargs: Any) -> Any:
        from src.models.search_schemas import SearchResponse

        self.calls.append(kwargs)
        request = kwargs["search_request"]
        return SearchResponse(
            query=request.query,
            search_id=str(uuid4()),
            search_type=request.search_type,
            results=self.results,
            total_results=len(self.results),
            returned_results=len(self.results),
            search_time_ms=1,
            limit=request.limit,
            offset=0,
            has_more=False,
        )


def _search_result(document_id: UUID, snippet: str) -> Any:
    from datetime import datetime

    from src.models.search_schemas import SearchResult, TextSnippet

    return SearchResult(
        document_id=str(document_id),
        title="retrieval-eval",
        document_type=DocumentType.TEXT,
        content_preview="preview",
        snippets=[
            TextSnippet(
                text=snippet, start_position=0, end_position=1, relevance_score=0.8
            )
        ],
        relevance_score=1.5,
        file_size_bytes=1,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
        processing_status="COMPLETED",
        tags=[],
        is_public=False,
        uploaded_by_user_id=str(USER),
        organization_id=str(ORG),
        metadata={"storage_key": "internal"},
    )


async def test_retrieve_passages_without_ids_scopes_to_the_collection(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _StubFulltext([_search_result(DOC_IN_PROJECT, "a <mark>hit</mark> here")])
    monkeypatch.setattr(read_tools, "fulltext_search_service", stub)
    result = await invoke_read(
        db, context, _invocation("retrieve_passages", query="hit", top_k=3)
    )
    assert result.is_error is False
    assert result.content[0]["chunks"] == [
        {
            "document_id": str(DOC_IN_PROJECT),
            "title": "retrieval-eval",
            "text": "a hit here",
            "score": 1.5,
        }
    ]
    assert result.source_refs == [{"document_id": str(DOC_IN_PROJECT)}]
    [call] = stub.calls
    assert call["organization_id"] == str(ORG)
    # Only the grant's collection, never the sibling project's document.
    assert call["search_request"].filters.document_ids == [str(DOC_IN_PROJECT)]
    assert call["search_request"].limit == 3


async def test_retrieve_passages_refuses_foreign_ids_before_searching(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _StubFulltext([])
    monkeypatch.setattr(read_tools, "fulltext_search_service", stub)
    result = await invoke_read(
        db,
        context,
        _invocation(
            "retrieve_passages",
            query="q",
            document_ids=[str(DOC_IN_PROJECT), str(DOC_OUTSIDE_PROJECT)],
        ),
    )
    assert result.is_error is True
    assert result.content[0]["reason"] == "requested_documents_unavailable"
    assert stub.calls == []


async def test_retrieve_passages_empty_collection_skips_search(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _StubFulltext([_search_result(DOC_OUTSIDE_PROJECT, "leak")])
    monkeypatch.setattr(read_tools, "fulltext_search_service", stub)
    await db.execute(update(CollectionDocument).values(is_deleted=True))
    await db.commit()
    context = IntegrationContext(
        user_id=USER, organization_id=ORG, project_id=PROJECT, grant_id=uuid4()
    )
    result = await invoke_read(db, context, _invocation("retrieve_passages", query="q"))
    assert result.is_error is False
    assert result.content[0]["chunks"] == []
    assert stub.calls == []


async def test_retrieve_passages_backend_failure_is_safe_error(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Boom:
        def search(self, **_kwargs: Any) -> Any:
            raise RuntimeError("dsn=postgres://user:pw@host/db")

    monkeypatch.setattr(read_tools, "fulltext_search_service", Boom())
    result = await invoke_read(db, context, _invocation("retrieve_passages", query="q"))
    assert result.is_error is True
    assert "pw@host" not in json.dumps(result.content)


async def test_arxiv_paper_content_is_transient_and_paginated(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Cache:
        store: dict[str, str] = {}

        async def get(self, key: str) -> str | None:
            return self.store.get(key)

        async def set(self, key: str, value: str, ex: int | None = None) -> None:
            self.store[key] = value

    monkeypatch.setattr(read_tools, "_arxiv_cache", Cache)
    monkeypatch.setattr(
        read_tools.arxiv_fulltext, "fetch_text", AsyncMock(return_value="t" * 10)
    )
    result = await invoke_read(
        db,
        context,
        _invocation("get_arxiv_paper_content", arxiv_id="2401.00001", limit=4),
    )
    assert result.is_error is False
    assert result.content[0]["text"] == "tttt"
    assert result.content[0]["next_offset"] == 4
    assert result.source_refs == [{"arxiv_id": "2401.00001"}]
    # Nothing landed in the documents table.
    rows = await db.execute(select(Document.id))
    assert {str(r) for r in rows.scalars().all()} == {
        str(DOC_IN_PROJECT),
        str(DOC_OUTSIDE_PROJECT),
    }


async def test_arxiv_paper_content_errors_are_stable(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(read_tools, "_arxiv_cache", lambda: read_tools._NoCache())
    monkeypatch.setattr(
        read_tools.arxiv_fulltext,
        "fetch_text",
        AsyncMock(side_effect=RuntimeError("token=abc")),
    )
    result = await invoke_read(
        db, context, _invocation("get_arxiv_paper_content", arxiv_id="2401.00001")
    )
    assert result.content == [{"error": "arxiv_unavailable"}]
    with pytest.raises(ToolArgumentError):
        await invoke_read(
            db, context, _invocation("get_arxiv_paper_content", arxiv_id="../etc")
        )


# --- Plan 07 slice 4: researcher tools ---------------------------------------
#
# The real KG agent tools are organization-wide and ignore project_id, so the
# fake below is deliberately project-blind: every researcher and paper in the
# organization is visible to it. Project scoping must come from the gateway.

SECRET_TITLE = "retrieval-secret"  # DOC_OUTSIDE_PROJECT's title (other project)


class _FakeKG:
    def __init__(self) -> None:
        self.people: dict[str, tuple[str, str, list[str]]] = {}
        self.search_calls: list[dict[str, Any]] = []
        self.neighborhood_calls: list[dict[str, Any]] = []
        self.entity_calls: list[str] = []

    def add(
        self,
        entity_id: str,
        name: str,
        papers: list[str],
        entity_type: str = "PERSON",
    ) -> None:
        self.people[entity_id] = (name, entity_type, papers)

    async def search(self, args: dict[str, Any], _user: Any = None) -> dict[str, Any]:
        self.search_calls.append(dict(args))
        needle = args["query"].lower()
        hits = [
            {"id": eid, "name": name, "type": kind, "confidence": 0.9}
            for eid, (name, kind, _papers) in self.people.items()
            if needle in name.lower() and kind in args["entity_types"]
        ]
        return {"entities": hits[: args["limit"]], "total": len(hits)}

    async def neighborhood(
        self, args: dict[str, Any], _user: Any = None
    ) -> dict[str, Any]:
        self.neighborhood_calls.append(dict(args))
        entity_id = args["entity_id"]
        connected: list[dict[str, Any]] = []
        if entity_id in self.people:
            for title in self.people[entity_id][2]:
                connected.append(
                    {"id": f"paper:{title}", "name": title, "type": "DOCUMENT"}
                )
            connected.append({"id": "org:mit", "name": "MIT", "type": "ORGANIZATION"})
        elif entity_id.startswith("paper:"):
            title = entity_id.removeprefix("paper:")
            for eid, (name, kind, papers) in self.people.items():
                if title in papers:
                    connected.append({"id": eid, "name": name, "type": kind})
            connected.append({"id": "cat:cs", "name": "cs.LG", "type": "CONCEPT"})
        return {
            "connected_entities": connected[: args["limit"]],
            "relationships": [],
            "center_entity_id": entity_id,
        }

    async def entity(self, entity_id: str, _organization_id: str) -> Any:
        self.entity_calls.append(entity_id)
        if entity_id not in self.people:
            return None
        name, kind, _papers = self.people[entity_id]
        return SimpleNamespace(id=entity_id, name=name, entity_type=EntityType(kind))


@pytest.fixture
def kg(monkeypatch: pytest.MonkeyPatch) -> _FakeKG:
    fake = _FakeKG()
    # Insertion order is search order. LeCun only wrote for the OTHER project.
    fake.add("ent-lecun", "Yann LeCun", [SECRET_TITLE])
    fake.add("ent-turing", "Alan Turing", ["retrieval-eval", SECRET_TITLE])
    fake.add("ent-lovelace", "Ada Lovelace", ["retrieval-eval"])
    monkeypatch.setattr(read_tools, "_tool_search_knowledge_graph", fake.search)
    monkeypatch.setattr(
        read_tools, "_tool_explore_entity_neighborhood", fake.neighborhood
    )
    monkeypatch.setattr(read_tools, "_kg_entity", fake.entity)
    return fake


async def _add_project_paper(
    db: AsyncSession,
    title: str,
    published: str | None = None,
    arxiv_id: str | None = None,
) -> UUID:
    document_id = uuid4()
    await db.execute(
        insert(Document).values(
            dict(
                _document_row(document_id, title),
                arxiv_id=arxiv_id,
                document_metadata={"published": published} if published else None,
            )
        )
    )
    await db.execute(
        insert(CollectionDocument).values(
            dict(collection_id=PROJECT, document_id=document_id)
        )
    )
    await db.commit()
    return document_id


async def test_find_researchers_keeps_only_in_project_authors(
    db: AsyncSession, context: IntegrationContext, kg: _FakeKG
) -> None:
    result = await invoke_read(db, context, _invocation("find_researchers", query="a"))
    assert result.is_error is False
    # LeCun's only paper is in the other project: he is dropped, and Turing's
    # count excludes his other-project paper.
    assert result.content == [
        {
            "researchers": [
                {"entity_id": "ent-turing", "name": "Alan Turing", "paper_count": 1},
                {
                    "entity_id": "ent-lovelace",
                    "name": "Ada Lovelace",
                    "paper_count": 1,
                },
            ]
        }
    ]
    # Entity ids are knowledge-graph nodes, not documents: nothing citable.
    assert result.source_refs == []
    [search] = kg.search_calls
    assert search["entity_types"] == ["PERSON"]
    assert search["query"] == "a"
    assert search["limit"] == read_tools.MAX_RESEARCHER_CANDIDATES
    assert "LeCun" not in json.dumps(result.content)


async def test_find_researchers_matches_titles_ignoring_case_and_spacing(
    db: AsyncSession, context: IntegrationContext, kg: _FakeKG
) -> None:
    kg.add("ent-babbage", "Charles Babbage", ["  RETRIEVAL-eval "])
    result = await invoke_read(
        db, context, _invocation("find_researchers", query="babbage")
    )
    assert [r["name"] for r in result.content[0]["researchers"]] == ["Charles Babbage"]


async def test_find_researchers_stops_checking_once_limit_is_met(
    db: AsyncSession, context: IntegrationContext, kg: _FakeKG
) -> None:
    result = await invoke_read(
        db, context, _invocation("find_researchers", query="a", limit=1)
    )
    assert [r["name"] for r in result.content[0]["researchers"]] == ["Alan Turing"]
    # LeCun (discarded) then Turing (kept): Lovelace is never looked up.
    assert [call["entity_id"] for call in kg.neighborhood_calls] == [
        "ent-lecun",
        "ent-turing",
    ]
    assert all(call["max_depth"] == 1 for call in kg.neighborhood_calls)


async def test_find_researchers_internal_clamp_is_defence_in_depth(
    db: AsyncSession, context: IntegrationContext, kg: _FakeKG
) -> None:
    for index in range(30):
        kg.add(f"ent-extra-{index}", f"Extra {index}", ["retrieval-eval"])
    user = await db.get(User, USER)
    result = await read_tools._find_researchers(
        db, context, user, {"query": "extra", "limit": 99}
    )
    assert len(result.content[0]["researchers"]) == read_tools.MAX_RESEARCHERS == 25


async def test_find_researchers_skips_knowledge_graph_for_an_empty_project(
    db: AsyncSession, context: IntegrationContext, kg: _FakeKG
) -> None:
    await db.execute(update(CollectionDocument).values(is_deleted=True))
    await db.commit()
    result = await invoke_read(db, context, _invocation("find_researchers", query="a"))
    assert result.is_error is False
    assert result.content == [{"researchers": []}]
    assert kg.search_calls == []


@pytest.mark.parametrize("failing", ["search", "neighborhood"])
@pytest.mark.parametrize("mode", ["raises", "error-payload"])
async def test_find_researchers_knowledge_graph_outage_is_stable_error(
    db: AsyncSession,
    context: IntegrationContext,
    kg: _FakeKG,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
    mode: str,
) -> None:
    secret = "bolt://neo4j:hunter2@10.0.0.9:7687"
    effect: Any = (
        {"side_effect": RuntimeError(secret)}
        if mode == "raises"
        else {"return_value": {"error": secret}}
    )
    name = (
        "_tool_search_knowledge_graph"
        if failing == "search"
        else "_tool_explore_entity_neighborhood"
    )
    monkeypatch.setattr(read_tools, name, AsyncMock(**effect))
    result = await invoke_read(db, context, _invocation("find_researchers", query="a"))
    assert result.is_error is True
    assert result.content == [{"error": "knowledge_graph_unavailable"}]
    assert result.source_refs == []
    assert "hunter2" not in json.dumps(result.content)


async def test_get_researcher_reshapes_in_project_papers_and_coauthors(
    db: AsyncSession, context: IntegrationContext, kg: _FakeKG
) -> None:
    await db.execute(
        update(Document)
        .where(Document.id == DOC_IN_PROJECT)
        .values(document_metadata={"published": "2020-01-01T00:00:00Z"})
    )
    attention = await _add_project_paper(
        db, "attention", published="2017-06-12T17:57:34Z", arxiv_id="1706.03762"
    )
    kg.add("ent-vaswani", "Ashish Vaswani", ["attention"])
    kg.people["ent-turing"][2].append("attention")

    result = await invoke_read(
        db, context, _invocation("get_researcher", entity_id="ent-turing")
    )
    assert result.is_error is False
    assert result.content == [
        {
            "researcher": {"entity_id": "ent-turing", "name": "Alan Turing"},
            # Newest published first; the other project's paper is absent.
            "papers": [
                {"document_id": str(DOC_IN_PROJECT), "title": "retrieval-eval"},
                {
                    "document_id": str(attention),
                    "title": "attention",
                    "arxiv_id": "1706.03762",
                },
            ],
            # LeCun co-wrote only the other project's paper with Turing.
            "coauthors": [
                {"entity_id": "ent-lovelace", "name": "Ada Lovelace"},
                {"entity_id": "ent-vaswani", "name": "Ashish Vaswani"},
            ],
        }
    ]
    assert result.source_refs == [
        {"document_id": str(DOC_IN_PROJECT)},
        {"document_id": str(attention)},
    ]
    assert kg.neighborhood_calls[0] == {
        "entity_id": "ent-turing",
        "max_depth": 1,
        "limit": 50,
    }
    assert "LeCun" not in json.dumps(result.content)


async def test_get_researcher_denies_an_entity_from_another_project(
    db: AsyncSession, context: IntegrationContext, kg: _FakeKG
) -> None:
    result = await invoke_read(
        db, context, _invocation("get_researcher", entity_id="ent-lecun")
    )
    assert result.is_error is True
    assert result.content == [{"error": "researcher_not_found"}]
    assert result.source_refs == []
    # Not even the name is fetched before an in-project paper is confirmed.
    assert kg.entity_calls == []
    assert "LeCun" not in json.dumps(result.content)


@pytest.mark.parametrize("entity_id", ["ent-nobody", "paper:retrieval-eval"])
async def test_get_researcher_unknown_or_non_person_entity_is_not_found(
    db: AsyncSession, context: IntegrationContext, kg: _FakeKG, entity_id: str
) -> None:
    kg.add("ent-topic", "Transformers", ["retrieval-eval"], entity_type="CONCEPT")
    for candidate in (entity_id, "ent-topic"):
        result = await invoke_read(
            db, context, _invocation("get_researcher", entity_id=candidate)
        )
        assert result.is_error is True
        assert result.content == [{"error": "researcher_not_found"}]


async def test_get_researcher_entity_that_vanished_is_not_found(
    db: AsyncSession,
    context: IntegrationContext,
    kg: _FakeKG,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(read_tools, "_kg_entity", AsyncMock(return_value=None))
    result = await invoke_read(
        db, context, _invocation("get_researcher", entity_id="ent-lovelace")
    )
    assert result.content == [{"error": "researcher_not_found"}]


@pytest.mark.parametrize("failing", ["neighborhood", "entity"])
async def test_get_researcher_knowledge_graph_outage_is_stable_error(
    db: AsyncSession,
    context: IntegrationContext,
    kg: _FakeKG,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
    boom = AsyncMock(side_effect=RuntimeError("token=abc"))
    monkeypatch.setattr(
        read_tools,
        (
            "_tool_explore_entity_neighborhood"
            if failing == "neighborhood"
            else "_kg_entity"
        ),
        boom,
    )
    result = await invoke_read(
        db, context, _invocation("get_researcher", entity_id="ent-lovelace")
    )
    assert result.is_error is True
    assert result.content == [{"error": "knowledge_graph_unavailable"}]
    assert "abc" not in json.dumps(result.content)


async def test_get_researcher_coauthor_lookups_are_bounded(
    db: AsyncSession, context: IntegrationContext, kg: _FakeKG
) -> None:
    titles = [f"paper-{index:02d}" for index in range(12)]
    for index, title in enumerate(titles):
        await _add_project_paper(db, title, published=f"2020-01-{index + 1:02d}")
        kg.add(f"ent-co-{index:02d}", f"Co {index:02d}", [title])
    kg.add("ent-prolific", "Prolific", titles)
    result = await invoke_read(
        db, context, _invocation("get_researcher", entity_id="ent-prolific")
    )
    assert len(result.content[0]["papers"]) == 12
    # One neighborhood for the researcher, then only the 10 newest papers.
    assert len(kg.neighborhood_calls) == 1 + read_tools.MAX_COAUTHOR_PAPERS
    assert {c["entity_id"] for c in result.content[0]["coauthors"]} == {
        f"ent-co-{index:02d}" for index in range(2, 12)
    }


async def test_get_researcher_result_stays_under_the_size_cap(
    db: AsyncSession,
    context: IntegrationContext,
    kg: _FakeKG,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kg.add("ent-hub", "Hub", ["retrieval-eval"])
    for index in range(150):
        kg.add(f"ent-big-{index}", ("\u6587" * 200) + str(index), ["retrieval-eval"])

    # The fake paper node has 150+ authors; the real tool caps a call at 50.
    async def uncapped(args: dict[str, Any], user: Any = None) -> dict[str, Any]:
        return await kg.neighborhood({**args, "limit": 500}, user)

    monkeypatch.setattr(read_tools, "_tool_explore_entity_neighborhood", uncapped)
    # 100 capped co-authors alone are ~63 KiB; shrink the cap so the trim runs.
    monkeypatch.setattr(read_tools, "MAX_RESULT_BYTES", 20_000)
    result = await invoke_read(
        db, context, _invocation("get_researcher", entity_id="ent-hub")
    )
    assert result.is_error is False
    payload = result.content[0]
    assert 0 < len(payload["coauthors"]) <= read_tools.MAX_COAUTHORS
    assert payload["truncated"] is True
    assert payload["papers"], "papers are kept while co-authors absorb the trim"
    wire = json.dumps(payload, ensure_ascii=False).encode()
    assert len(wire) <= read_tools.MAX_RESULT_BYTES


@pytest.mark.parametrize(
    "invocation",
    [
        _invocation("find_researchers"),
        _invocation("find_researchers", query=""),
        _invocation("find_researchers", query="x" * 501),
        _invocation("find_researchers", query="x", limit=26),
        _invocation("find_researchers", query="x", limit=0),
        _invocation("find_researchers", query="x", limit="5"),
        _invocation("find_researchers", query="x", project_id=str(OTHER_PROJECT)),
        _invocation("find_researchers", query="x", organization_id=str(ORG)),
        _invocation("get_researcher"),
        _invocation("get_researcher", entity_id=""),
        _invocation("get_researcher", entity_id="e" * 129),
        _invocation("get_researcher", entity_id=7),
        _invocation("get_researcher", entity_id="e", project_id=str(PROJECT)),
    ],
    ids=[
        "find-missing-query",
        "find-empty-query",
        "find-long-query",
        "find-limit-over-cap",
        "find-limit-zero",
        "find-limit-string",
        "find-project-identity",
        "find-org-identity",
        "get-missing-id",
        "get-empty-id",
        "get-long-id",
        "get-int-id",
        "get-project-identity",
    ],
)
async def test_researcher_argument_shapes_are_rejected(
    db: AsyncSession,
    context: IntegrationContext,
    kg: _FakeKG,
    invocation: ToolInvocation,
) -> None:
    with pytest.raises(ToolArgumentError):
        await invoke_read(db, context, invocation)
    assert kg.search_calls == [] and kg.neighborhood_calls == []


def test_researcher_tools_are_advertised_with_the_coverage_caveat() -> None:
    catalog = {tool.name: tool for tool in list_read_tools()}
    for name in ("find_researchers", "get_researcher"):
        assert read_tools.TOOL_SCOPES[name] == "tools:read"
        description = catalog[name].description
        assert "ingested into this project" in description
        assert "affiliations" in description
        assert "project_id" not in catalog[name].input_schema["properties"]
    assert catalog["find_researchers"].input_schema["required"] == ["query"]
    assert catalog["get_researcher"].input_schema["required"] == ["entity_id"]
