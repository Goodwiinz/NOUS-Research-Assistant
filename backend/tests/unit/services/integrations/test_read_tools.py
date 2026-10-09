"""Scoped integration read gateway, using a local SQLite database."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Iterable, Iterator
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.artifact import Artifact, ArtifactVersion
from src.models.collection import Collection, CollectionDocument
from src.models.conversation import Conversation
from src.models.document import Document, DocumentType
from src.models.generated_draft import GeneratedDraft
from src.models.organization import Organization
from src.models.research_project import ResearchProject
from src.models.research_project_role import ResearchProjectRoleAssignment
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.artifact import ArtifactNotFound
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_tools import ToolInvocation
from src.services.integrations import read_tools
from src.services.integrations.context import IntegrationAccessDenied
from src.services.integrations.read_tools import (
    MAX_ARTIFACTS,
    MAX_RESULTS,
    READ_TOOL_NAMES,
    ToolArgumentError,
    invoke_read,
    list_read_tools,
)

pytestmark = pytest.mark.unit
USER, ORG, WORKSPACE = (uuid4() for _ in range(3))
# Fixed, and in the reverse of the order of their names ("Other" sorts before
# "Project"), so a page ordered by id instead of by name is a different page.
# With random ids the ordering tests passed or failed by the luck of the draw.
PROJECT, OTHER_PROJECT = UUID(int=1), UUID(int=2)
DOC_IN_PROJECT, DOC_OUTSIDE_PROJECT = uuid4(), uuid4()
# A second workspace of the same user: reachable by that user, but outside a
# grant bound to WORKSPACE. One of its projects is named like str(None).
OTHER_WORKSPACE, FOREIGN_PROJECT, NAMED_NONE_PROJECT = uuid4(), uuid4(), uuid4()
DOC_FOREIGN, DOC_NAMED_NONE = uuid4(), uuid4()
# What a grant holds. The gateway checks the scope of each tool on every call.
TOOLS_READ = frozenset({"tools:read"})
LIBRARY_READ = frozenset({"tools:read", "library:read"})


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
        Artifact,
        ArtifactVersion,
        Conversation,
        Thread,
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
            insert(Workspace).values(
                id=OTHER_WORKSPACE,
                name="Other workspace",
                owner_id=USER,
                organization_id=ORG,
            )
        )
        await session.execute(
            insert(Collection).values(
                [
                    dict(id=PROJECT, name="Project", workspace_id=WORKSPACE),
                    dict(id=OTHER_PROJECT, name="Other", workspace_id=WORKSPACE),
                    dict(
                        id=FOREIGN_PROJECT,
                        name="Foreign",
                        workspace_id=OTHER_WORKSPACE,
                    ),
                    dict(
                        id=NAMED_NONE_PROJECT,
                        name="None",
                        workspace_id=OTHER_WORKSPACE,
                    ),
                ]
            )
        )
        await session.execute(
            insert(Document).values(
                [
                    _document_row(DOC_IN_PROJECT, "retrieval-eval"),
                    _document_row(DOC_OUTSIDE_PROJECT, "retrieval-secret"),
                    _document_row(DOC_FOREIGN, "retrieval-foreign"),
                    _document_row(DOC_NAMED_NONE, "retrieval-none"),
                ]
            )
        )
        await session.execute(
            insert(CollectionDocument).values(
                [
                    dict(collection_id=PROJECT, document_id=DOC_IN_PROJECT),
                    dict(collection_id=OTHER_PROJECT, document_id=DOC_OUTSIDE_PROJECT),
                    dict(collection_id=FOREIGN_PROJECT, document_id=DOC_FOREIGN),
                    dict(collection_id=NAMED_NONE_PROJECT, document_id=DOC_NAMED_NONE),
                ]
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture
def context() -> IntegrationContext:
    return IntegrationContext(
        user_id=USER,
        organization_id=ORG,
        project_id=PROJECT,
        grant_id=uuid4(),
        scopes=TOOLS_READ,
    )


def _invocation(tool_name: str, **arguments: Any) -> ToolInvocation:
    return ToolInvocation(
        tool_name=tool_name, arguments=arguments, invocation_id=uuid4()
    )


def test_catalog_advertises_only_the_read_allowlist_without_identity_args() -> None:
    catalog = list_read_tools()
    names = {tool.name for tool in catalog}
    assert names == set(READ_TOOL_NAMES) | set(read_tools.LOCAL_TOOLS)
    assert "list_library" in names
    assert set(read_tools.TOOL_SCOPES) == names
    # Every tool needs tools:read except the library listing.
    assert {
        name: scope
        for name, scope in read_tools.TOOL_SCOPES.items()
        if scope != "tools:read"
    } == {"list_library": "library:read"}
    assert "execute_code" not in names and "forget_memory" not in names
    for tool in catalog:
        assert "project_id" not in tool.input_schema.get("properties", {})


def test_selected_context_is_not_a_gateway_read_tool() -> None:
    # read_selected_context is served by GET /integrations/context and takes its
    # project from the grant: the memories a user selects are stored per project
    # consent. Listing it here would give a workspace grant a project_id selector
    # over memories nobody selected for that project. A workspace grant cannot
    # hold context:read, so a project grant is the only one that reaches it.
    assert "read_selected_context" not in {tool.name for tool in list_read_tools()}
    assert "context:read" not in set(read_tools.TOOL_SCOPES.values())


@pytest.mark.parametrize(
    "invocation",
    [
        _invocation("execute_code", code="print(1)"),
        _invocation("list_project_documents", project_id=str(PROJECT)),
        _invocation("list_project_artifacts", project_id=str(PROJECT)),
        _invocation("list_project_artifacts", limit=MAX_ARTIFACTS + 1),
        _invocation("search_documents", query="x", organization_id=str(ORG)),
        _invocation("search_documents", query="x", unexpected="y"),
        _invocation("search_documents"),
    ],
    ids=[
        "unknown-tool",
        "identity-project",
        "artifacts-identity-project",
        "artifacts-limit-over-max",
        "identity-org",
        "extra-arg",
        "missing",
    ],
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


# WG-2b: a legacy workspace (organization_id NULL) is its owner's, so every
# project read must agree with list_project_documents, which already
# coalesces (project_access.py). Mutation: restore
# `Workspace.organization_id == organization_id` in _live_project_documents
# and the search below finds nothing.
async def test_legacy_workspace_documents_are_searchable(
    db: AsyncSession, context: IntegrationContext
) -> None:
    await db.execute(
        update(Workspace).where(Workspace.id == WORKSPACE).values(organization_id=None)
    )
    await db.commit()
    listed = await invoke_read(db, context, _invocation("list_project_documents"))
    assert listed.source_refs == [{"document_id": str(DOC_IN_PROJECT)}]
    found = await invoke_read(
        db, context, _invocation("search_documents", query="retrieval")
    )
    assert found.is_error is False
    assert [doc["id"] for doc in found.content[0]["documents"]] == [str(DOC_IN_PROJECT)]


async def test_a_legacy_workspace_follows_its_owner_out_of_the_org(
    db: AsyncSession,
) -> None:
    # The negative that keeps the predicate: deleting it instead of fixing it
    # would still pass the test above. The document keeps ORG, so only the
    # workspace rule can hide it.
    await db.execute(
        update(Workspace).where(Workspace.id == WORKSPACE).values(organization_id=None)
    )
    elsewhere = uuid4()
    await db.execute(
        insert(Organization).values(
            id=elsewhere, name="Elsewhere", storage_limit_bytes=1000000
        )
    )
    await db.commit()
    rows = await db.scalars(read_tools._live_project_documents(PROJECT, ORG))
    assert [d.id for d in rows.all()] == [DOC_IN_PROJECT]
    await db.execute(
        update(User).where(User.id == USER).values(organization_id=elsewhere)
    )
    await db.commit()
    rows = await db.scalars(read_tools._live_project_documents(PROJECT, ORG))
    assert rows.all() == []


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
    db: AsyncSession, model: Any
) -> None:
    # Ancestor soft-deleted between invoke_read's ownership check and the
    # fetch: the fetch alone must return nothing.
    await db.execute(update(model).values(is_deleted=True))
    await db.commit()
    payload = await read_tools._search_project_documents(
        db, PROJECT, ORG, "retrieval", 10
    )
    assert payload["documents"] == []
    assert (
        await read_tools._project_document_ids(db, PROJECT, ORG, [str(DOC_IN_PROJECT)])
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
        user_id=uuid4(),
        organization_id=ORG,
        project_id=PROJECT,
        grant_id=uuid4(),
        scopes=TOOLS_READ,
    )
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(db, context, _invocation("list_project_documents"))


async def _artifact(
    db: AsyncSession, project: UUID, title: str, minute: int, org: UUID = ORG
) -> tuple[UUID, UUID]:
    artifact_id, version_id = uuid4(), uuid4()
    await db.execute(
        insert(ArtifactVersion).values(
            id=version_id,
            artifact_id=artifact_id,
            upload_id=uuid4(),
            title=title,
            mime_type="text/markdown",
            byte_size=7,
            sha256="a" * 64,
            storage_key=f"private/{version_id}",
            producer="harness",
            provenance={"producer": "harness"},
            created_at=datetime(2026, 10, 1, tzinfo=timezone.utc)
            + timedelta(minutes=minute),
        )
    )
    await db.execute(
        insert(Artifact).values(
            id=artifact_id,
            organization_id=org,
            project_id=project,
            owner_id=USER,
            title=title,
            current_version_id=version_id,
        )
    )
    await db.commit()
    return artifact_id, version_id


async def test_list_project_artifacts_returns_only_the_grant_project(
    db: AsyncSession, context: IntegrationContext
) -> None:
    older = await _artifact(db, PROJECT, "older.md", 1)
    newer = await _artifact(db, PROJECT, "newer.md", 2)
    await _artifact(db, OTHER_PROJECT, "sibling.md", 3)
    await _artifact(db, PROJECT, "foreign-org.md", 4, org=uuid4())

    result = await invoke_read(db, context, _invocation("list_project_artifacts"))

    assert result.is_error is False
    rows = result.content[0]["artifacts"]
    assert [row["title"] for row in rows] == ["newer.md", "older.md"]
    assert rows[0]["sha256"] == "a" * 64 and rows[0]["thread_id"] is None
    assert result.source_refs == [
        {"artifact_id": str(newer[0]), "version_id": str(newer[1])},
        {"artifact_id": str(older[0]), "version_id": str(older[1])},
    ]
    assert "storage_key" not in str(result.content)


async def test_list_project_artifacts_honours_limit(
    db: AsyncSession, context: IntegrationContext
) -> None:
    for minute in range(3):
        await _artifact(db, PROJECT, f"a{minute}.md", minute)
    result = await invoke_read(
        db, context, _invocation("list_project_artifacts", limit=2)
    )
    assert len(result.content[0]["artifacts"]) == 2


async def test_every_artifact_row_at_the_cap_has_a_source_ref(
    db: AsyncSession, context: IntegrationContext
) -> None:
    for minute in range(MAX_ARTIFACTS + 1):
        await _artifact(db, PROJECT, f"a{minute}.md", minute)
    result = await invoke_read(db, context, _invocation("list_project_artifacts"))
    rows = result.content[0]["artifacts"]
    assert len(rows) == MAX_ARTIFACTS == MAX_RESULTS
    assert result.source_refs == [
        {"artifact_id": row["artifact_id"], "version_id": row["version_id"]}
        for row in rows
    ]


async def test_list_project_artifacts_is_advertised_with_bounded_limit() -> None:
    tool = {t.name: t for t in list_read_tools()}["list_project_artifacts"]
    assert tool.input_schema["additionalProperties"] is False
    assert set(tool.input_schema["properties"]) == {"limit"}
    assert tool.input_schema["properties"]["limit"]["minimum"] == 1
    assert tool.input_schema["properties"]["limit"]["maximum"] == MAX_ARTIFACTS


async def test_list_project_artifacts_denial_is_a_structured_error(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Project vanishes between grant authorization and the artifact query.
    monkeypatch.setattr(
        read_tools, "list_project_artifacts", AsyncMock(side_effect=ArtifactNotFound())
    )
    result = await invoke_read(db, context, _invocation("list_project_artifacts"))
    assert result.is_error is True
    assert result.content == [{"error": "Project not found or access denied"}]
    assert result.source_refs == []


# ---------------------------------------------------------------------------
# Plan 07 slice 2: args-only primitives, document content, passages
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


def test_retrieve_passages_says_it_is_document_level() -> None:
    # RT-6: one excerpt per document and every word required, so a model
    # neither expects every passage nor sends a whole question.
    description = {tool.name: tool.description for tool in list_read_tools()}[
        "retrieve_passages"
    ]
    assert "best-matching documents, at most top_k" in description
    assert "one short excerpt" in description
    assert "every query word" in description


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


def _external_row(source: str, index: int) -> dict[str, Any]:
    # About the size of a real connector row (verifier V5 measured ~723 B).
    return {
        "id": f"{source}-{index}",
        "title": "t" * 120,
        "source": source,
        "url": f"https://example.test/{source}/{index}",
        "content": "c" * 300,
        "authors": [f"Author {n}" for n in range(5)],
        "published_date": "2026-01-01",
        "document_type": "article",
    }


@pytest.mark.parametrize("max_results", [20, 5])
async def test_external_search_caps_results_in_total_across_connectors(
    db: AsyncSession,
    context: IntegrationContext,
    monkeypatch: pytest.MonkeyPatch,
    max_results: int,
) -> None:
    # No connector or domain: the registry tool searches every keyless
    # connector (8) and applies max_results to each one (RT-4).
    sources = [f"connector{n}" for n in range(8)]
    rows = [_external_row(s, i) for s in sources for i in range(max_results)]
    monkeypatch.setattr(
        read_tools,
        "_tool_search_external_database",
        AsyncMock(
            return_value={
                "query": "insulin",
                "total_results": len(rows),
                "results": rows,
                "connectors_searched": sources,
            }
        ),
    )
    result = await invoke_read(
        db,
        context,
        _invocation(
            "search_external_database", query="insulin", max_results=max_results
        ),
    )
    assert result.is_error is False
    payload = result.content[0]
    assert len(payload["results"]) == max_results
    assert payload["truncated"] is True
    assert payload["total_results"] == 8 * max_results  # what was found
    # Round-robin: every connector is represented while the cap allows.
    assert {row["source"] for row in payload["results"]} == set(sources[:max_results])
    assert len(result.source_refs) == max_results


async def test_external_search_cap_round_robins_uneven_connectors(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Connectors return 1, 6 and 2 rows, plus one row with no source: a
    # connector that runs out drops out of later rounds instead of failing.
    rows = (
        [_external_row("a", 0)]
        + [_external_row("b", i) for i in range(6)]
        + [_external_row("c", i) for i in range(2)]
        + [{**_external_row("none", 0), "source": None}]
    )
    monkeypatch.setattr(
        read_tools,
        "_tool_search_external_database",
        AsyncMock(return_value={"total_results": len(rows), "results": rows}),
    )
    result = await invoke_read(
        db, context, _invocation("search_external_database", query="q", max_results=7)
    )
    assert result.is_error is False
    payload = result.content[0]
    assert [row["id"] for row in payload["results"]] == [
        "a-0",
        "b-0",
        "c-0",
        "none-0",
        "b-1",
        "c-1",
        "b-2",
    ]
    assert payload["truncated"] is True
    assert payload["total_results"] == 10


@pytest.mark.parametrize(
    ("tool", "arguments", "hint", "valid"),
    [
        (
            "search_external_database",
            {"query": "p53", "domain": "biology"},
            "valid_domains",
            "biomedical",
        ),
        (
            "search_external_database",
            {"query": "p53", "connector": "no-such-db"},
            "available_connectors",
            "pubmed",
        ),
        (
            "list_external_databases",
            {"domain": "biology"},
            "valid_domains",
            "biomedical",
        ),
    ],
    ids=["bad-domain", "unknown-connector", "list-bad-domain"],
)
async def test_external_argument_mistakes_return_the_valid_values(
    db: AsyncSession,
    context: IntegrationContext,
    tool: str,
    arguments: dict[str, Any],
    hint: str,
    valid: str,
) -> None:
    result = await invoke_read(db, context, _invocation(tool, **arguments))
    assert result.is_error is True
    payload = result.content[0]
    assert payload["error"] == "invalid_arguments"
    assert valid in payload[hint]
    # Only the public registry list: never the message text.
    assert set(payload) == {"error", hint}
    assert result.source_refs == []


async def test_rejected_connector_filters_are_an_argument_error(
    db: AsyncSession, context: IntegrationContext
) -> None:
    result = await invoke_read(
        db,
        context,
        _invocation(
            "search_external_database",
            query="p53",
            connector="pubmed",
            filters={"no_such_filter": "x"},
        ),
    )
    assert result.content == [
        {
            "error": "invalid_arguments",
            "error_category": "unsupported_connector_filter",
        }
    ]


async def test_unlisted_error_category_is_an_upstream_error(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the two filter categories are caller mistakes; any other category
    # is an outage and leaves the gateway without its message or category.
    monkeypatch.setattr(
        read_tools,
        "_tool_search_external_database",
        AsyncMock(
            return_value={"error": "boom", "error_category": "connector_crashed"}
        ),
    )
    result = await invoke_read(
        db, context, _invocation("search_external_database", query="p53")
    )
    assert result.is_error is True
    assert result.content == [{"error": "upstream_unavailable"}]
    assert result.source_refs == []


@pytest.mark.parametrize(
    ("tool", "upstream", "arguments", "expected"),
    [
        (
            "search_external_database",
            "_tool_search_external_database",
            {"query": "p53", "connector": "PubMed", "domain": " Biomedical "},
            {"connector": "pubmed", "domain": "biomedical"},
        ),
        (
            "list_external_databases",
            "_tool_list_external_databases",
            {"domain": "Biomedical"},
            {"domain": "biomedical"},
        ),
    ],
    ids=["search", "list"],
)
async def test_connector_and_domain_names_match_case_insensitively(
    db: AsyncSession,
    context: IntegrationContext,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    upstream: str,
    arguments: dict[str, Any],
    expected: dict[str, str],
) -> None:
    registry = AsyncMock(return_value={"results": []})
    monkeypatch.setattr(read_tools, upstream, registry)
    await invoke_read(db, context, _invocation(tool, **arguments))
    assert registry.await_args is not None
    sent = registry.await_args.args[0]
    assert {key: sent[key] for key in expected} == expected


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
        user_id=USER,
        organization_id=ORG,
        project_id=PROJECT,
        grant_id=uuid4(),
        scopes=TOOLS_READ,
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
    before = set((await db.execute(select(Document.id))).scalars().all())
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
    after = set((await db.execute(select(Document.id))).scalars().all())
    assert before and after == before


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


async def test_retrieve_passages_canonicalizes_document_ids(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _StubFulltext([_search_result(DOC_IN_PROJECT, "hit")])
    monkeypatch.setattr(read_tools, "fulltext_search_service", stub)
    result = await invoke_read(
        db,
        context,
        _invocation(
            "retrieve_passages", query="hit", document_ids=[str(DOC_IN_PROJECT).upper()]
        ),
    )
    assert result.is_error is False
    assert result.content[0]["chunks"][0]["document_id"] == str(DOC_IN_PROJECT)
    assert stub.calls[0]["search_request"].filters.document_ids == [str(DOC_IN_PROJECT)]


async def test_arxiv_paper_content_stays_under_the_result_cap(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(read_tools, "_arxiv_cache", lambda: read_tools._NoCache())
    monkeypatch.setattr(
        read_tools.arxiv_fulltext,
        "fetch_text",
        AsyncMock(return_value="\u6587" * 100_000),
    )
    result = await invoke_read(
        db, context, _invocation("get_arxiv_paper_content", arxiv_id="2401.00001")
    )
    assert result.is_error is False
    page = result.content[0]
    assert (
        len(json.dumps(page, ensure_ascii=False).encode())
        <= read_tools.MAX_RESULT_BYTES
    )
    assert 0 < len(page["text"]) < 48_000
    assert page["next_offset"] == len(page["text"])
    assert result.source_refs == [{"arxiv_id": "2401.00001"}]


async def test_arxiv_paper_content_timeout_and_empty_text(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    monkeypatch.setattr(read_tools, "_arxiv_cache", lambda: read_tools._NoCache())

    async def slow(_id: str) -> str:
        await asyncio.sleep(5)
        return "x"

    monkeypatch.setattr(read_tools.arxiv_fulltext, "fetch_text", slow)
    monkeypatch.setattr(read_tools, "_search_arxiv_budget_seconds", lambda: 0.01)
    result = await invoke_read(
        db, context, _invocation("get_arxiv_paper_content", arxiv_id="2401.00001")
    )
    assert result.content == [{"error": "upstream_timeout"}]
    monkeypatch.setattr(read_tools, "_search_arxiv_budget_seconds", lambda: 5)
    monkeypatch.setattr(
        read_tools.arxiv_fulltext, "fetch_text", AsyncMock(return_value="")
    )
    result = await invoke_read(
        db, context, _invocation("get_arxiv_paper_content", arxiv_id="2401.00001")
    )
    assert result.is_error is True
    assert result.content == [{"error": "arxiv_no_text"}]


async def test_retrieve_passages_drops_hits_revoked_during_search(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _StubFulltext([_search_result(DOC_IN_PROJECT, "hit")])
    monkeypatch.setattr(read_tools, "fulltext_search_service", stub)

    async def revoke_then_search(func: Any, /, **kwargs: Any) -> Any:
        # The pre-filter already captured DOC_IN_PROJECT; revoke it now.
        await db.execute(update(CollectionDocument).values(is_deleted=True))
        await db.commit()
        return func(**kwargs)

    monkeypatch.setattr(read_tools, "run_in_threadpool", revoke_then_search)
    result = await invoke_read(
        db, context, _invocation("retrieve_passages", query="hit")
    )
    assert result.is_error is False
    assert stub.calls[0]["search_request"].filters.document_ids == [str(DOC_IN_PROJECT)]
    assert result.content[0]["chunks"] == []
    assert result.source_refs == []


async def test_document_summary_falls_back_to_sql_preview(
    db: AsyncSession, context: IntegrationContext
) -> None:
    await db.execute(
        update(Document)
        .where(Document.id == DOC_IN_PROJECT)
        .values(content_text="b" * 700, content_summary=None)
    )
    await db.commit()
    result = await invoke_read(
        db,
        context,
        _invocation("get_document_content", document_id=str(DOC_IN_PROJECT)),
    )
    assert result.is_error is False
    assert result.content[0]["text"] == "b" * 500
    assert result.content[0]["total_chars"] == 500


async def test_returned_error_payloads_are_sanitized(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        read_tools,
        "_tool_search_external_database",
        AsyncMock(return_value={"error": "set ACME_API_KEY", "results": []}),
    )
    monkeypatch.setattr(
        read_tools,
        "_tool_search_arxiv",
        AsyncMock(return_value={"error": "Traceback: boom", "papers": []}),
    )
    for name in ("search_external_database", "search_arxiv"):
        result = await invoke_read(db, context, _invocation(name, query="q"))
        assert result.is_error is True
        assert result.content == [{"error": "upstream_unavailable"}]
        assert result.source_refs == []


# --- Plan 07 slice 4: researcher tools ---------------------------------------
#
# The researcher tools index Document.document_metadata["authors"] of the
# grant's live documents; the sibling project's papers (same organization,
# overlapping authors) are what the leak tests probe.


async def _add_doc(
    db: AsyncSession,
    title: str,
    metadata: Any,
    *,
    created: datetime,
    project: UUID = PROJECT,
    arxiv_id: str | None = None,
) -> UUID:
    document_id = uuid4()
    await db.execute(
        insert(Document).values(
            dict(
                _document_row(document_id, title),
                arxiv_id=arxiv_id,
                document_metadata=metadata,
                created_at=created,
            )
        )
    )
    await db.execute(
        insert(CollectionDocument).values(
            dict(collection_id=project, document_id=document_id)
        )
    )
    await db.commit()
    return document_id


def _day(day: int) -> datetime:
    return datetime(2026, 1, day)


async def _seed_authors(db: AsyncSession) -> None:
    """Two project papers sharing Turing, plus a sibling project's paper."""
    await _add_doc(
        db,
        "Computing Machinery",
        {"authors": ["Alan Turing", "Ada Lovelace"]},
        created=_day(1),
    )
    await _add_doc(
        db,
        "Computable Numbers",
        {"authors": ["alan   TURING", "Charles Babbage"]},
        created=_day(2),
    )
    await _add_doc(
        db,
        "Secret Work",
        {"authors": ["Alan Turing", "Yann LeCun"]},
        created=_day(4),
        project=OTHER_PROJECT,
    )


async def _find(db: AsyncSession, context: IntegrationContext, **arguments: Any) -> Any:
    result = await invoke_read(
        db, context, _invocation("find_researchers", **arguments)
    )
    assert result.source_refs == []
    return result


async def test_find_researchers_never_sees_another_projects_authors(
    db: AsyncSession, context: IntegrationContext
) -> None:
    await _seed_authors(db)
    result = await _find(db, context, query="a")
    assert result.is_error is False
    # Turing's count is the two in-project papers, not the sibling's third.
    assert result.content == [
        {
            "researchers": [
                {
                    "researcher_id": "alan turing",
                    "name": "alan TURING",
                    "paper_count": 2,
                },
                {
                    "researcher_id": "ada lovelace",
                    "name": "Ada Lovelace",
                    "paper_count": 1,
                },
                {
                    "researcher_id": "charles babbage",
                    "name": "Charles Babbage",
                    "paper_count": 1,
                },
            ]
        }
    ]
    lecun = await _find(db, context, query="lecun")
    assert lecun.content == [{"researchers": []}]
    assert "LeCun" not in json.dumps(result.content)


@pytest.mark.parametrize("query", ["TURING", "  alan   Tur ", "uring"])
async def test_find_researchers_is_a_normalised_substring_match(
    db: AsyncSession, context: IntegrationContext, query: str
) -> None:
    await _seed_authors(db)
    result = await _find(db, context, query=query)
    [researcher] = result.content[0]["researchers"]
    # Spelling variants merge; the newest document's spelling is kept.
    assert researcher["researcher_id"] == "alan turing"
    assert researcher["name"] == "alan TURING"
    assert researcher["paper_count"] == 2


async def test_find_researchers_orders_by_papers_then_name_and_honours_limit(
    db: AsyncSession, context: IntegrationContext
) -> None:
    await _seed_authors(db)
    two = await _find(db, context, query="a", limit=2)
    assert [r["name"] for r in two.content[0]["researchers"]] == [
        "alan TURING",
        "Ada Lovelace",
    ]
    # Cut by limit: the caller is told the list is incomplete.
    assert two.content[0]["truncated"] is True
    for complete in (
        await _find(db, context, query="a"),  # default limit 10
        await _find(db, context, query="a", limit=3),  # exactly the matches
    ):
        assert len(complete.content[0]["researchers"]) == 3
        assert "truncated" not in complete.content[0]


async def test_find_researchers_internal_clamp_is_defence_in_depth(
    db: AsyncSession, context: IntegrationContext
) -> None:
    names = [f"Person {index:02d}" for index in range(30)]
    await _add_doc(db, "Crowd", {"authors": names}, created=_day(1))
    result = await read_tools._find_researchers(
        db, PROJECT, ORG, {"query": "person", "limit": 99}
    )
    assert len(result.content[0]["researchers"]) == read_tools.MAX_RESEARCHERS == 25


async def test_find_researchers_tolerates_documents_without_clean_authors(
    db: AsyncSession, context: IntegrationContext
) -> None:
    await _add_doc(db, "none", None, created=_day(1))
    await _add_doc(db, "no-key", {"source": "arxiv"}, created=_day(2))
    await _add_doc(db, "list-meta", ["authors"], created=_day(3))
    await _add_doc(db, "null", {"authors": None}, created=_day(4))
    await _add_doc(db, "lone", {"authors": "Solo Author"}, created=_day(5))
    await _add_doc(
        db,
        "messy",
        {
            "authors": [
                "Grace Hopper",
                "grace  hopper",
                "",
                "  ",
                None,
                7,
                {"nom": "x"},
                {"name": "Edsger Dijkstra"},
            ]
        },
        created=_day(6),
    )
    result = await _find(db, context, query="r")
    assert {
        r["researcher_id"]: r["paper_count"] for r in result.content[0]["researchers"]
    } == {
        "grace hopper": 1,
        "edsger dijkstra": 1,
        "solo author": 1,
    }


async def test_find_researchers_excludes_deleted_documents_and_ancestors(
    db: AsyncSession, context: IntegrationContext
) -> None:
    doc = await _add_doc(db, "Paper", {"authors": ["Ada Lovelace"]}, created=_day(1))
    assert (await _find(db, context, query="ada")).content[0]["researchers"]
    await db.execute(update(Document).where(Document.id == doc).values(is_deleted=True))
    await db.commit()
    assert (await _find(db, context, query="ada")).content == [{"researchers": []}]
    await db.execute(
        update(Document).where(Document.id == doc).values(is_deleted=False)
    )
    await db.execute(update(Collection).values(is_deleted=True))
    await db.commit()
    # invoke_read refuses a dead project first; the scan alone must also be empty.
    assert await read_tools._scan_authored_papers(db, PROJECT, ORG) == ([], False)


async def test_find_researchers_reports_when_the_scan_cap_hides_documents(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fixture's authorless document is the newest, so it uses one slot.
    monkeypatch.setattr(read_tools, "MAX_SCANNED_DOCUMENTS", 3)
    await _add_doc(db, "old", {"authors": ["Old Author"]}, created=_day(1))
    await _add_doc(db, "mid", {"authors": ["Mid Author"]}, created=_day(2))
    await _add_doc(db, "new", {"authors": ["New Author"]}, created=_day(3))
    result = await _find(db, context, query="author")
    assert {r["name"] for r in result.content[0]["researchers"]} == {
        "Mid Author",
        "New Author",
    }
    assert result.content[0]["truncated"] is True
    # Truncation describes the scan, so it is reported even for a narrow hit.
    assert (await _find(db, context, query="new")).content[0]["truncated"] is True


async def test_researcher_id_round_trips_when_the_name_is_cut_at_the_cap(
    db: AsyncSession, context: IntegrationContext
) -> None:
    # The 200-character cut lands right after the space.
    long_name = "A" * 199 + " B"
    doc = await _add_doc(db, "Long Name", {"authors": [long_name]}, created=_day(1))
    [found] = (await _find(db, context, query="a")).content[0]["researchers"]
    assert found["researcher_id"] == "a" * 199
    assert found["name"] == "A" * 199
    result = await invoke_read(
        db, context, _invocation("get_researcher", researcher_id=found["researcher_id"])
    )
    assert result.is_error is False
    assert result.content[0]["papers"] == [
        {"document_id": str(doc), "title": "Long Name"}
    ]


async def test_find_researchers_blank_query_is_an_argument_error(
    db: AsyncSession, context: IntegrationContext
) -> None:
    with pytest.raises(ToolArgumentError):
        await invoke_read(db, context, _invocation("find_researchers", query="   "))


async def _seed_turing(db: AsyncSession) -> dict[str, UUID]:
    """Four in-project Turing papers (mixed date keys) and a sibling's fifth."""
    ids = {
        "published": await _add_doc(
            db,
            "Published Later",
            {
                "authors": ["Alan Turing", "Ada Lovelace"],
                "published": "2020-05-01T00:00:00+00:00",
            },
            created=_day(1),
        ),
        "arxiv": await _add_doc(
            db,
            "Ingested Newer",
            {
                "authors": ["alan  turing", "Charles Babbage", "Ada Lovelace"],
                "publication_date": "2017-06-12T17:57:34+00:00",
            },
            created=_day(3),
            arxiv_id="1706.03762",
        ),
        "undated_old": await _add_doc(
            db,
            "Undated Old",
            {"authors": ["Alan Turing", "Charles Babbage"]},
            created=_day(2),
        ),
        "undated_new": await _add_doc(
            db,
            "Undated New",
            {"authors": ["Alan Turing", "Ada Lovelace"], "arxiv_id": "2401.00001"},
            created=_day(4),
        ),
        "secret": await _add_doc(
            db,
            "Secret Work",
            {"authors": ["Alan Turing", "Yann LeCun"]},
            created=_day(5),
            project=OTHER_PROJECT,
        ),
    }
    return ids


async def _get(
    db: AsyncSession, context: IntegrationContext, researcher_id: str
) -> Any:
    return await invoke_read(
        db, context, _invocation("get_researcher", researcher_id=researcher_id)
    )


async def test_get_researcher_reads_in_project_papers_and_coauthors(
    db: AsyncSession, context: IntegrationContext
) -> None:
    ids = await _seed_turing(db)
    result = await _get(db, context, "alan turing")
    assert result.is_error is False
    assert result.content == [
        {
            "researcher": {"researcher_id": "alan turing", "name": "Alan Turing"},
            # Published date first (either metadata key), then newest ingest.
            "papers": [
                {
                    "document_id": str(ids["published"]),
                    "title": "Published Later",
                    "published": "2020-05-01T00:00:00+00:00",
                },
                {
                    "document_id": str(ids["arxiv"]),
                    "title": "Ingested Newer",
                    "arxiv_id": "1706.03762",
                    "published": "2017-06-12T17:57:34+00:00",
                },
                {
                    "document_id": str(ids["undated_new"]),
                    "title": "Undated New",
                    "arxiv_id": "2401.00001",
                },
                {"document_id": str(ids["undated_old"]), "title": "Undated Old"},
            ],
            # Shared papers, most first; the sibling project's LeCun is absent.
            "coauthors": [
                {
                    "researcher_id": "ada lovelace",
                    "name": "Ada Lovelace",
                    "paper_count": 3,
                },
                {
                    "researcher_id": "charles babbage",
                    "name": "Charles Babbage",
                    "paper_count": 2,
                },
            ],
        }
    ]
    assert result.source_refs == [
        {"document_id": str(ids["published"])},
        {"document_id": str(ids["arxiv"])},
        {"document_id": str(ids["undated_new"])},
        {"document_id": str(ids["undated_old"])},
    ]
    dumped = json.dumps(result.content)
    assert "LeCun" not in dumped and str(ids["secret"]) not in dumped


async def test_get_researcher_accepts_any_spelling_of_the_id(
    db: AsyncSession, context: IntegrationContext
) -> None:
    await _seed_turing(db)
    result = await _get(db, context, "  ALAN   Turing ")
    assert result.is_error is False
    assert result.content[0]["researcher"]["researcher_id"] == "alan turing"
    assert len(result.content[0]["papers"]) == 4


@pytest.mark.parametrize("researcher_id", ["yann lecun", "nobody at all"])
async def test_get_researcher_denies_other_projects_and_unknown_authors(
    db: AsyncSession, context: IntegrationContext, researcher_id: str
) -> None:
    await _seed_turing(db)
    result = await _get(db, context, researcher_id)
    # An author who only wrote for the sibling project looks like no author.
    assert result.is_error is True
    assert result.content == [{"error": "researcher_not_found"}]
    assert result.source_refs == []


async def test_get_researcher_redacts_titles_it_returns(
    db: AsyncSession, context: IntegrationContext
) -> None:
    await _add_doc(
        db,
        "Contact bob@example.com",
        {"authors": ["Ada Lovelace"]},
        created=_day(1),
    )
    result = await _get(db, context, "ada lovelace")
    assert result.content[0]["papers"][0]["title"] == "Contact <email>"
    assert "bob@example.com" not in json.dumps(result.content)


async def test_get_researcher_blank_id_is_an_argument_error(
    db: AsyncSession, context: IntegrationContext
) -> None:
    with pytest.raises(ToolArgumentError):
        await _get(db, context, "   ")


async def test_get_researcher_tolerates_malformed_coauthor_entries(
    db: AsyncSession, context: IntegrationContext
) -> None:
    await _add_doc(
        db,
        "Messy",
        {"authors": ["Grace Hopper", None, 7, "", {"name": "Ada Lovelace"}, {"x": 1}]},
        created=_day(1),
    )
    await _add_doc(db, "No authors", {"source": "arxiv"}, created=_day(2))
    result = await _get(db, context, "grace hopper")
    assert result.is_error is False
    assert result.content[0]["coauthors"] == [
        {"researcher_id": "ada lovelace", "name": "Ada Lovelace", "paper_count": 1}
    ]


async def test_get_researcher_caps_papers_and_flags_truncation(
    db: AsyncSession, context: IntegrationContext
) -> None:
    ids = [uuid4() for _ in range(101)]
    await db.execute(
        insert(Document).values(
            [
                dict(
                    _document_row(document_id, f"paper-{index:03d}"),
                    document_metadata={"authors": ["Prolific Author"]},
                    created_at=datetime(2025, 1, 1) + timedelta(minutes=index),
                )
                for index, document_id in enumerate(ids)
            ]
        )
    )
    await db.execute(
        insert(CollectionDocument).values(
            [dict(collection_id=PROJECT, document_id=i) for i in ids]
        )
    )
    await db.commit()
    result = await _get(db, context, "prolific author")
    payload = result.content[0]
    assert len(payload["papers"]) == read_tools.MAX_RESEARCHER_PAPERS == 100
    assert payload["truncated"] is True
    titles = {paper["title"] for paper in payload["papers"]}
    assert "paper-000" not in titles and "paper-100" in titles  # oldest dropped
    assert len(result.source_refs) == 100


async def test_get_researcher_caps_coauthors_and_flags_truncation(
    db: AsyncSession, context: IntegrationContext
) -> None:
    names = ["Lead Author"] + [f"Co {index:03d}" for index in range(150)]
    await _add_doc(db, "Consortium", {"authors": names}, created=_day(1))
    payload = (await _get(db, context, "lead author")).content[0]
    assert len(payload["coauthors"]) == read_tools.MAX_COAUTHORS == 100
    assert payload["coauthors"][0]["researcher_id"] == "co 000"
    assert payload["truncated"] is True


async def test_get_researcher_result_stays_under_the_size_cap(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 100 co-authors of ~370 UTF-8 bytes each; shrink the cap so the trim runs.
    names = ["Lead Author"] + [("\u6587" * 60) + str(index) for index in range(100)]
    await _add_doc(db, "Consortium", {"authors": names}, created=_day(1))
    monkeypatch.setattr(read_tools, "MAX_RESULT_BYTES", 20_000)
    result = await _get(db, context, "lead author")
    assert result.is_error is False
    payload = result.content[0]
    assert 0 < len(payload["coauthors"]) < 100
    assert payload["truncated"] is True
    assert payload["papers"], "papers are kept while co-authors absorb the trim"
    envelope = {
        "content": result.content,
        "is_error": result.is_error,
        "source_refs": result.source_refs,
    }
    assert len(json.dumps(envelope, ensure_ascii=False).encode()) <= 20_000


async def test_get_researcher_size_cap_counts_source_refs(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [uuid4() for _ in range(100)]
    await db.execute(
        insert(Document).values(
            [
                dict(
                    _document_row(document_id, f"p{index}"),
                    document_metadata={"authors": ["Prolific Author"]},
                    created_at=datetime(2025, 1, 1) + timedelta(minutes=index),
                )
                for index, document_id in enumerate(ids)
            ]
        )
    )
    await db.execute(
        insert(CollectionDocument).values(
            [dict(collection_id=PROJECT, document_id=i) for i in ids]
        )
    )
    await db.commit()
    full = await _get(db, context, "prolific author")
    assert len(full.source_refs) == 100 and "truncated" not in full.content[0]
    content_bytes = len(json.dumps(full.content[0], ensure_ascii=False).encode())
    # The content alone fits this cap; content plus the 100 refs does not.
    cap = content_bytes + 200
    monkeypatch.setattr(read_tools, "MAX_RESULT_BYTES", cap)
    result = await _get(db, context, "prolific author")
    assert result.is_error is False
    assert result.content[0]["truncated"] is True
    assert 0 < len(result.content[0]["papers"]) < 100
    assert len(result.source_refs) == len(result.content[0]["papers"])
    envelope = {
        "content": result.content,
        "is_error": result.is_error,
        "source_refs": result.source_refs,
    }
    assert len(json.dumps(envelope, ensure_ascii=False).encode()) <= cap


async def test_get_researcher_reports_when_the_scan_cap_hides_documents(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(read_tools, "MAX_SCANNED_DOCUMENTS", 2)
    await _add_doc(db, "old", {"authors": ["Old Author"]}, created=_day(1))
    await _add_doc(db, "new", {"authors": ["New Author"]}, created=_day(2))
    assert (await _get(db, context, "new author")).content[0]["truncated"] is True
    # Older than the scan window: an error that says the scan was incomplete,
    # so it is not mistaken for an unknown id.
    hidden = await _get(db, context, "old author")
    assert hidden.is_error is True
    assert hidden.content == [{"error": "researcher_not_found", "truncated": True}]
    assert hidden.source_refs == []


async def test_get_researcher_unknown_id_after_a_complete_scan_is_plain(
    db: AsyncSession, context: IntegrationContext
) -> None:
    await _add_doc(db, "only", {"authors": ["Known Author"]}, created=_day(1))
    unknown = await _get(db, context, "nobody")
    assert unknown.is_error is True
    assert unknown.content == [{"error": "researcher_not_found"}]


@pytest.mark.parametrize(
    "invocation",
    [
        _invocation("find_researchers"),
        _invocation("find_researchers", query=""),
        _invocation("find_researchers", query="x" * 201),
        _invocation("find_researchers", query="x", limit=26),
        _invocation("find_researchers", query="x", limit=0),
        _invocation("find_researchers", query="x", limit="5"),
        _invocation("find_researchers", query="x", project_id=str(OTHER_PROJECT)),
        _invocation("find_researchers", query="x", organization_id=str(ORG)),
        _invocation("get_researcher"),
        _invocation("get_researcher", researcher_id=""),
        _invocation("get_researcher", researcher_id="r" * 201),
        _invocation("get_researcher", researcher_id=7),
        _invocation("get_researcher", entity_id="r"),
        _invocation("get_researcher", researcher_id="r", project_id=str(PROJECT)),
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
        "get-old-entity-id-name",
        "get-project-identity",
    ],
)
async def test_researcher_argument_shapes_are_rejected(
    db: AsyncSession, context: IntegrationContext, invocation: ToolInvocation
) -> None:
    with pytest.raises(ToolArgumentError):
        await invoke_read(db, context, invocation)


def test_researcher_tools_are_advertised_with_the_coverage_caveat() -> None:
    catalog = {tool.name: tool for tool in list_read_tools()}
    for name in ("find_researchers", "get_researcher"):
        assert read_tools.TOOL_SCOPES[name] == "tools:read"
        description = catalog[name].description
        assert "ingested into this project" in description
        assert "affiliations" in description
        assert "project_id" not in catalog[name].input_schema["properties"]
    assert catalog["find_researchers"].input_schema["required"] == ["query"]
    assert catalog["get_researcher"].input_schema["required"] == ["researcher_id"]


# --- workspace grants: project selector --------------------------------------

PROJECT_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("search_documents", {"query": "retrieval"}),
    ("list_project_documents", {}),
    ("do_kb_retrieve", {"query": "q", "document_ids": [str(DOC_IN_PROJECT)]}),
    ("get_current_draft", {}),
]
PROJECT_TOOL_IDS = [name for name, _arguments in PROJECT_TOOLS]


def _workspace_context(scopes: Iterable[str] = TOOLS_READ) -> IntegrationContext:
    return IntegrationContext(
        user_id=USER,
        organization_id=ORG,
        project_id=None,
        workspace_id=WORKSPACE,
        grant_id=uuid4(),
        scopes=frozenset(scopes),
    )


def _project_context(scopes: Iterable[str] = TOOLS_READ) -> IntegrationContext:
    return IntegrationContext(
        user_id=USER,
        organization_id=ORG,
        project_id=PROJECT,
        grant_id=uuid4(),
        scopes=frozenset(scopes),
    )


@pytest.fixture
def adapters(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Registry adapters as mocks, to prove a refusal never reaches them."""
    mocks = {
        name: AsyncMock()
        for name in (
            "_tool_list_project_documents",
            "_tool_do_kb_retrieve",
            "_tool_get_current_draft",
        )
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(read_tools, name, mock)
    return mocks


@pytest.mark.parametrize(("tool", "arguments"), PROJECT_TOOLS, ids=PROJECT_TOOL_IDS)
async def test_workspace_grant_needs_project_selector_for_project_tools(
    db: AsyncSession,
    adapters: dict[str, AsyncMock],
    tool: str,
    arguments: dict[str, Any],
) -> None:
    result = await invoke_read(db, _workspace_context(), _invocation(tool, **arguments))
    assert result.is_error is True
    assert result.content == [{"error": "project_id_required"}]
    assert result.source_refs == []
    for adapter in adapters.values():
        adapter.assert_not_awaited()


async def test_workspace_grant_without_selector_never_resolves_the_word_none(
    db: AsyncSession, adapters: dict[str, AsyncMock]
) -> None:
    # str(None) used to reach the agent helper, which resolves a non-UUID as a
    # project NAME; a project called "None" in another workspace of the same
    # user (NAMED_NONE_PROJECT) is reachable that way.
    result = await invoke_read(
        db, _workspace_context(), _invocation("list_project_documents")
    )
    assert result.content == [{"error": "project_id_required"}]
    adapters["_tool_list_project_documents"].assert_not_awaited()
    searched = await invoke_read(
        db, _workspace_context(), _invocation("search_documents", query="retrieval")
    )
    assert searched.content == [{"error": "project_id_required"}]


@pytest.mark.parametrize(("tool", "arguments"), PROJECT_TOOLS, ids=PROJECT_TOOL_IDS)
async def test_workspace_grant_project_selector_outside_workspace_denied(
    db: AsyncSession,
    adapters: dict[str, AsyncMock],
    tool: str,
    arguments: dict[str, Any],
) -> None:
    # FOREIGN_PROJECT belongs to this very user, in another workspace: only the
    # grant's scope, not ownership, can refuse it.
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(
            db,
            _workspace_context(),
            _invocation(tool, project_id=str(FOREIGN_PROJECT), **arguments),
        )
    for adapter in adapters.values():
        adapter.assert_not_awaited()


@pytest.mark.parametrize(
    ("selected", "expected"),
    [(PROJECT, DOC_IN_PROJECT), (OTHER_PROJECT, DOC_OUTSIDE_PROJECT)],
    ids=["first-project", "second-project"],
)
async def test_workspace_grant_reads_only_the_selected_project(
    db: AsyncSession, selected: UUID, expected: UUID
) -> None:
    context = _workspace_context()
    listed = await invoke_read(
        db,
        context,
        _invocation("list_project_documents", project_id=str(selected)),
    )
    assert listed.is_error is False
    assert listed.source_refs == [{"document_id": str(expected)}]
    searched = await invoke_read(
        db,
        context,
        _invocation("search_documents", query="retrieval", project_id=str(selected)),
    )
    assert searched.is_error is False
    assert [doc["id"] for doc in searched.content[0]["documents"]] == [str(expected)]


async def test_workspace_grant_reads_the_draft_of_the_selected_project_only(
    db: AsyncSession,
) -> None:
    draft_id = uuid4()
    await db.execute(
        insert(GeneratedDraft).values(
            id=draft_id, project_id=PROJECT, version=1, title="Draft", content="body"
        )
    )
    await db.commit()
    context = _workspace_context()
    own = await invoke_read(
        db, context, _invocation("get_current_draft", project_id=str(PROJECT))
    )
    assert own.is_error is False
    assert own.content[0]["draft"]["id"] == str(draft_id)
    assert own.source_refs == [{"draft_id": str(draft_id)}]
    other = await invoke_read(
        db, context, _invocation("get_current_draft", project_id=str(OTHER_PROJECT))
    )
    assert other.content[0]["project_name"] == "Other"
    assert other.content[0]["draft"] is None
    assert other.source_refs == []


async def test_workspace_retrieval_scopes_document_ids_to_the_selected_project(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = AsyncMock(return_value={"chunks": [], "total": 0})
    monkeypatch.setattr(read_tools, "_tool_do_kb_retrieve", retrieval)
    context = _workspace_context()
    refused = await invoke_read(
        db,
        context,
        _invocation(
            "do_kb_retrieve",
            query="q",
            document_ids=[str(DOC_OUTSIDE_PROJECT)],
            project_id=str(PROJECT),
        ),
    )
    # The document is in this workspace, but not in the selected project.
    assert refused.is_error is True
    assert refused.content[0]["reason"] == "requested_documents_unavailable"
    retrieval.assert_not_awaited()
    allowed = await invoke_read(
        db,
        context,
        _invocation(
            "do_kb_retrieve",
            query="q",
            document_ids=[str(DOC_OUTSIDE_PROJECT)],
            project_id=str(OTHER_PROJECT),
        ),
    )
    assert allowed.is_error is False
    assert retrieval.await_args is not None
    forwarded = retrieval.await_args.args[0]
    assert forwarded["project_id"] == str(OTHER_PROJECT)
    assert forwarded["document_ids"] == [str(DOC_OUTSIDE_PROJECT)]


@pytest.mark.parametrize(
    "selector",
    ["not-a-uuid", "None", "Project", "", 123, True, ["x"], {"id": 1}],
    ids=["text", "none-word", "project-name", "empty", "int", "bool", "list", "dict"],
)
async def test_workspace_selector_must_be_a_uuid_string_never_a_name(
    db: AsyncSession, adapters: dict[str, AsyncMock], selector: Any
) -> None:
    with pytest.raises(ToolArgumentError, match="project_id must be a UUID"):
        await invoke_read(
            db,
            _workspace_context(),
            _invocation("list_project_documents", project_id=selector),
        )
    adapters["_tool_list_project_documents"].assert_not_awaited()


async def test_null_selector_is_the_same_as_an_omitted_one(
    db: AsyncSession, adapters: dict[str, AsyncMock]
) -> None:
    result = await invoke_read(
        db,
        _workspace_context(),
        _invocation("list_project_documents", project_id=None),
    )
    assert result.content == [{"error": "project_id_required"}]
    adapters["_tool_list_project_documents"].assert_not_awaited()


@pytest.mark.parametrize(
    "key",
    ["user_id", "organization_id", "thread_id", "run_id", "grant_id", "workspace_id"],
)
async def test_workspace_grant_accepts_no_identity_argument_but_the_selector(
    db: AsyncSession, adapters: dict[str, AsyncMock], key: str
) -> None:
    with pytest.raises(ToolArgumentError):
        await invoke_read(
            db,
            _workspace_context(),
            _invocation(
                "list_project_documents", project_id=str(PROJECT), **{key: str(uuid4())}
            ),
        )
    adapters["_tool_list_project_documents"].assert_not_awaited()


async def test_workspace_grant_is_denied_once_its_workspace_is_deleted(
    db: AsyncSession, adapters: dict[str, AsyncMock]
) -> None:
    await db.execute(
        update(Workspace).where(Workspace.id == WORKSPACE).values(is_deleted=True)
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(
            db,
            _workspace_context(),
            _invocation("list_project_documents", project_id=str(PROJECT)),
        )
    adapters["_tool_list_project_documents"].assert_not_awaited()


async def test_selector_naming_a_deleted_project_is_denied(
    db: AsyncSession, adapters: dict[str, AsyncMock]
) -> None:
    await db.execute(
        update(Collection).where(Collection.id == PROJECT).values(is_deleted=True)
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(
            db,
            _workspace_context(),
            _invocation("list_project_documents", project_id=str(PROJECT)),
        )
    adapters["_tool_list_project_documents"].assert_not_awaited()


# Tools that act on no single project: there is nothing for a selector to select.
PROJECTLESS_TOOLS = {
    "search_arxiv",
    "search_external_database",
    "list_external_databases",
    "get_arxiv_paper_content",
}


def test_catalog_offers_the_project_selector_only_to_workspace_grants() -> None:
    for tool in list_read_tools():
        assert "project_id" not in tool.input_schema["properties"]
    workspace_catalog = list_read_tools(workspace_bound=True)
    assert {tool.name for tool in workspace_catalog} == set(READ_TOOL_NAMES) | set(
        read_tools.LOCAL_TOOLS
    )
    selectorless = set()
    for tool in workspace_catalog:
        schema = tool.input_schema
        if "project_id" not in schema["properties"]:
            selectorless.add(tool.name)
            continue
        selector = schema["properties"]["project_id"]
        assert selector["type"] == "string" and selector["format"] == "uuid"
        assert "workspace grants" in selector["description"]
        # Optional in the schema: omitting it is a structured error, not a 422.
        assert "project_id" not in schema["required"]
        assert schema["additionalProperties"] is False
    # list_library lists the grant's whole scope, so it has no target to select.
    assert selectorless == PROJECTLESS_TOOLS | {"list_library"}


def test_catalog_selector_does_not_leak_into_the_shared_local_schemas() -> None:
    # list_read_tools hands out copies: advertising the selector to one
    # workspace grant must not change what a project grant sees afterwards.
    list_read_tools(workspace_bound=True)
    for name, (_description, schema, _scope) in read_tools.LOCAL_TOOLS.items():
        assert "project_id" not in schema["properties"], name
    for tool in list_read_tools():
        assert "project_id" not in tool.input_schema["properties"]


# --- workspace grants: the tools added by Plan 06 and Plan 07 -----------------

# Each acts on one project, so a workspace grant must name it like the others.
SELECTOR_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("list_project_artifacts", {}),
    ("get_document_content", {"document_id": str(DOC_IN_PROJECT)}),
    ("retrieve_passages", {"query": "retrieval"}),
    ("find_researchers", {"query": "a"}),
    ("get_researcher", {"researcher_id": "a"}),
]
SELECTOR_TOOL_IDS = [name for name, _arguments in SELECTOR_TOOLS]


@pytest.fixture
def project_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AsyncMock, _StubFulltext]:
    """The artifact query and the search service, to prove a refusal precedes them."""
    artifacts = AsyncMock(return_value=[])
    search = _StubFulltext([])
    monkeypatch.setattr(read_tools, "list_project_artifacts", artifacts)
    monkeypatch.setattr(read_tools, "fulltext_search_service", search)
    return artifacts, search


@pytest.mark.parametrize(("tool", "arguments"), SELECTOR_TOOLS, ids=SELECTOR_TOOL_IDS)
async def test_workspace_grant_needs_a_selector_for_every_project_tool(
    db: AsyncSession,
    project_readers: tuple[AsyncMock, _StubFulltext],
    tool: str,
    arguments: dict[str, Any],
) -> None:
    artifacts, search = project_readers
    result = await invoke_read(db, _workspace_context(), _invocation(tool, **arguments))
    assert result.is_error is True
    assert result.content == [{"error": "project_id_required"}]
    assert result.source_refs == []
    artifacts.assert_not_awaited()
    assert search.calls == []


@pytest.mark.parametrize(("tool", "arguments"), SELECTOR_TOOLS, ids=SELECTOR_TOOL_IDS)
async def test_workspace_grant_selector_outside_workspace_is_denied_for_every_tool(
    db: AsyncSession,
    project_readers: tuple[AsyncMock, _StubFulltext],
    tool: str,
    arguments: dict[str, Any],
) -> None:
    artifacts, search = project_readers
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(
            db,
            _workspace_context(),
            _invocation(tool, project_id=str(FOREIGN_PROJECT), **arguments),
        )
    artifacts.assert_not_awaited()
    assert search.calls == []


@pytest.mark.parametrize(("tool", "arguments"), SELECTOR_TOOLS, ids=SELECTOR_TOOL_IDS)
async def test_project_grant_still_refuses_the_selector_for_every_tool(
    db: AsyncSession,
    context: IntegrationContext,
    project_readers: tuple[AsyncMock, _StubFulltext],
    tool: str,
    arguments: dict[str, Any],
) -> None:
    artifacts, search = project_readers
    with pytest.raises(ToolArgumentError, match="unknown arguments"):
        await invoke_read(
            db, context, _invocation(tool, project_id=str(PROJECT), **arguments)
        )
    artifacts.assert_not_awaited()
    assert search.calls == []


@pytest.mark.parametrize(
    "invocation",
    [
        _invocation("search_arxiv", query="q", project_id=str(PROJECT)),
        _invocation("search_external_database", query="q", project_id=str(PROJECT)),
        _invocation("list_external_databases", project_id=str(PROJECT)),
        _invocation("get_arxiv_paper_content", arxiv_id="2401.00001", project_id="x"),
    ],
    ids=sorted(PROJECTLESS_TOOLS, key=lambda name: name),
)
async def test_projectless_tools_take_no_selector_from_a_workspace_grant(
    db: AsyncSession, invocation: ToolInvocation
) -> None:
    with pytest.raises(ToolArgumentError, match="unknown arguments"):
        await invoke_read(db, _workspace_context(), invocation)


async def test_projectless_tools_run_for_a_workspace_grant_without_a_selector(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    arxiv = AsyncMock(return_value={"papers": [{"id": "2401.00001"}]})
    monkeypatch.setattr(read_tools, "_tool_search_arxiv", arxiv)
    ownership = AsyncMock()
    monkeypatch.setattr(read_tools, "_verify_project_ownership", ownership)
    result = await invoke_read(
        db, _workspace_context(), _invocation("search_arxiv", query="llm")
    )
    assert result.is_error is False
    assert result.source_refs == [{"arxiv_id": "2401.00001"}]
    arxiv.assert_awaited_once()
    ownership.assert_not_awaited()


@pytest.mark.parametrize(
    ("selected", "titles"),
    [(PROJECT, ["first.md"]), (OTHER_PROJECT, ["second.md"])],
    ids=["first-project", "second-project"],
)
async def test_workspace_grant_lists_only_the_selected_projects_artifacts(
    db: AsyncSession, selected: UUID, titles: list[str]
) -> None:
    await _artifact(db, PROJECT, "first.md", 1)
    await _artifact(db, OTHER_PROJECT, "second.md", 2)
    await _artifact(db, FOREIGN_PROJECT, "foreign.md", 3)
    result = await invoke_read(
        db,
        _workspace_context(),
        _invocation("list_project_artifacts", project_id=str(selected)),
    )
    assert result.is_error is False
    assert [row["title"] for row in result.content[0]["artifacts"]] == titles


async def test_workspace_document_content_is_scoped_to_the_selected_project(
    db: AsyncSession,
) -> None:
    context = _workspace_context()
    # DOC_OUTSIDE_PROJECT is in this workspace, but not in the selected project.
    refused = await invoke_read(
        db,
        context,
        _invocation(
            "get_document_content",
            document_id=str(DOC_OUTSIDE_PROJECT),
            project_id=str(PROJECT),
        ),
    )
    assert refused.is_error is True
    assert refused.content[0]["reason"] == "requested_documents_unavailable"
    allowed = await invoke_read(
        db,
        context,
        _invocation(
            "get_document_content",
            document_id=str(DOC_OUTSIDE_PROJECT),
            project_id=str(OTHER_PROJECT),
        ),
    )
    assert allowed.is_error is False
    assert allowed.content[0]["document_id"] == str(DOC_OUTSIDE_PROJECT)
    assert allowed.source_refs == [{"document_id": str(DOC_OUTSIDE_PROJECT)}]


async def test_workspace_passages_search_only_the_selected_project(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _StubFulltext([_search_result(DOC_OUTSIDE_PROJECT, "a hit")])
    monkeypatch.setattr(read_tools, "fulltext_search_service", stub)
    context = _workspace_context()
    refused = await invoke_read(
        db,
        context,
        _invocation(
            "retrieve_passages",
            query="q",
            document_ids=[str(DOC_OUTSIDE_PROJECT)],
            project_id=str(PROJECT),
        ),
    )
    assert refused.is_error is True
    assert refused.content[0]["reason"] == "requested_documents_unavailable"
    assert stub.calls == []
    result = await invoke_read(
        db,
        context,
        _invocation("retrieve_passages", query="q", project_id=str(OTHER_PROJECT)),
    )
    assert result.is_error is False
    assert [chunk["document_id"] for chunk in result.content[0]["chunks"]] == [
        str(DOC_OUTSIDE_PROJECT)
    ]
    (call,) = stub.calls
    # Only the selected project's documents were handed to the search service.
    assert call["search_request"].filters.document_ids == [str(DOC_OUTSIDE_PROJECT)]
    assert call["organization_id"] == str(ORG)


async def test_workspace_researchers_come_from_the_selected_project_only(
    db: AsyncSession,
) -> None:
    await _seed_authors(db)
    context = _workspace_context()

    async def call(tool: str, selected: UUID, **arguments: Any) -> Any:
        return await invoke_read(
            db, context, _invocation(tool, project_id=str(selected), **arguments)
        )

    # Yann LeCun only co-wrote a paper in OTHER_PROJECT.
    in_first = await call("find_researchers", PROJECT, query="lecun")
    in_second = await call("find_researchers", OTHER_PROJECT, query="lecun")
    assert in_first.content[0]["researchers"] == []
    assert [r["name"] for r in in_second.content[0]["researchers"]] == ["Yann LeCun"]
    unknown_here = await call("get_researcher", PROJECT, researcher_id="yann lecun")
    assert unknown_here.is_error is True
    assert unknown_here.content == [{"error": "researcher_not_found"}]
    known_there = await call(
        "get_researcher", OTHER_PROJECT, researcher_id="yann lecun"
    )
    assert known_there.is_error is False
    assert known_there.content[0]["researcher"]["name"] == "Yann LeCun"


# --- workspace grants: every tool, one invariant ------------------------------

# One valid call per tool, without a project_id. A tool added to READ_TOOL_NAMES
# or LOCAL_TOOLS needs a row here, or test_every_tool_has_a_workspace_grant_case
# fails: the grant has no project of its own, so every branch of invoke_read
# must take its project from the selector or answer without one.
EVERY_TOOL_CALL: dict[str, dict[str, Any]] = {
    "search_documents": {"query": "retrieval"},
    "list_project_documents": {},
    "do_kb_retrieve": {"query": "q", "document_ids": [str(DOC_IN_PROJECT)]},
    "get_current_draft": {},
    "list_project_artifacts": {},
    "search_arxiv": {"query": "llm"},
    "search_external_database": {"query": "p53"},
    "list_external_databases": {},
    "get_document_content": {"document_id": str(DOC_IN_PROJECT)},
    "retrieve_passages": {"query": "retrieval"},
    "get_arxiv_paper_content": {"arxiv_id": "2401.00001"},
    "find_researchers": {"query": "a"},
    "get_researcher": {"researcher_id": "a"},
    "list_library": {},
}


def test_every_tool_has_a_workspace_grant_case() -> None:
    tools = set(READ_TOOL_NAMES) | set(read_tools.LOCAL_TOOLS)
    assert set(EVERY_TOOL_CALL) == tools
    # Each one names the scope a grant needs for it.
    assert set(read_tools.TOOL_SCOPES) == tools


@pytest.fixture
def offline_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tools that reach outside the database, answered locally."""
    for name, payload in (
        ("_tool_search_arxiv", {"papers": []}),
        ("_tool_search_external_database", {"results": []}),
        ("_tool_list_external_databases", {"connectors": []}),
    ):
        monkeypatch.setattr(read_tools, name, AsyncMock(return_value=payload))
    monkeypatch.setattr(read_tools, "_arxiv_cache", lambda: read_tools._NoCache())
    monkeypatch.setattr(
        read_tools.arxiv_fulltext, "fetch_text", AsyncMock(return_value="text")
    )
    monkeypatch.setattr(read_tools, "fulltext_search_service", _StubFulltext([]))


def _bound_values(parameters: Any) -> Iterable[Any]:
    if isinstance(parameters, dict):
        parameters = list(parameters.values())
    for value in parameters or ():
        if isinstance(value, (list, tuple, dict)):
            yield from _bound_values(value)
        else:
            yield value


@pytest.fixture
def executed_sql(db: AsyncSession) -> Iterator[list[tuple[str, Any]]]:
    """Every statement the session sends, with the values it binds."""
    seen: list[tuple[str, Any]] = []

    def record(
        _conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any
    ) -> None:
        seen.append((statement, parameters))

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine, "before_cursor_execute", record)


@pytest.mark.usefixtures("offline_tools")
@pytest.mark.parametrize("tool", sorted(EVERY_TOOL_CALL))
async def test_every_tool_under_a_workspace_grant_without_a_selector(
    db: AsyncSession, executed_sql: list[tuple[str, Any]], tool: str
) -> None:
    # Either the tool needs a project, and says so, or it answers from the
    # grant's own scope. Never an exception, and never the word "None": the
    # agent helpers resolve a non-UUID string as a project NAME, and the
    # user's other workspace holds a project called exactly that.
    selector_tools = {
        listed.name
        for listed in list_read_tools(workspace_bound=True)
        if "project_id" in listed.input_schema["properties"]
    }
    result = await invoke_read(
        db,
        _workspace_context(TOOLS_READ | LIBRARY_READ),
        _invocation(tool, **EVERY_TOOL_CALL[tool]),
    )
    if tool in selector_tools:
        assert result.is_error is True
        assert result.content == [{"error": "project_id_required"}]
        assert result.source_refs == []
    else:
        assert result.content != [{"error": "project_id_required"}]
    if tool == "list_library":
        assert set(_library_folders(result)) == {str(PROJECT), str(OTHER_PROJECT)}
    assert executed_sql, "the recorder saw no statement"
    for statement, parameters in executed_sql:
        assert "'None'" not in statement
        assert "None" not in _bound_values(parameters), statement
    leaked = json.dumps(result.content, default=str)
    assert str(NAMED_NONE_PROJECT) not in leaked and "retrieval-none" not in leaked


# --- list_library: a local tool behind library:read ---------------------------


def _library_folders(result: Any) -> dict[str, dict[str, Any]]:
    return {folder["id"]: folder for folder in result.content[0]["folders"]}


async def test_list_library_returns_workspace_collections_with_counts(
    db: AsyncSession,
) -> None:
    result = await invoke_read(
        db, _workspace_context(LIBRARY_READ), _invocation("list_library")
    )
    assert result.is_error is False
    folders = _library_folders(result)
    # Both projects of the grant's workspace, never a project of the user's
    # other workspace.
    assert set(folders) == {str(PROJECT), str(OTHER_PROJECT)}
    assert folders[str(PROJECT)]["name"] == "Project"
    assert folders[str(PROJECT)]["document_count"] == 1
    assert result.content[0]["next_offset"] is None
    assert result.source_refs == []


async def test_list_library_on_project_grant_lists_only_that_project(
    db: AsyncSession,
) -> None:
    result = await invoke_read(
        db, _project_context(LIBRARY_READ), _invocation("list_library")
    )
    assert result.is_error is False
    assert [f["id"] for f in result.content[0]["folders"]] == [str(PROJECT)]


@pytest.mark.parametrize(
    "make_context", [_project_context, _workspace_context], ids=["project", "workspace"]
)
async def test_list_library_requires_library_read_scope(
    db: AsyncSession, make_context: Any
) -> None:
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(db, make_context(TOOLS_READ), _invocation("list_library"))


@pytest.mark.parametrize("tool", sorted(read_tools.TOOL_SCOPES))
async def test_a_context_without_scopes_may_call_no_tool(
    db: AsyncSession, tool: str
) -> None:
    # Fail closed, and before arguments are looked at: most tools get none of
    # their required ones here, so an earlier error would be a
    # ToolArgumentError, not this. Every tool of the table, so a tool added
    # later cannot skip the check.
    bare = IntegrationContext(
        user_id=USER, organization_id=ORG, project_id=PROJECT, grant_id=uuid4()
    )
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(db, bare, _invocation(tool))


@pytest.mark.parametrize("tool", sorted(set(read_tools.TOOL_SCOPES) - {"list_library"}))
@pytest.mark.parametrize(
    "make_context", [_project_context, _workspace_context], ids=["project", "workspace"]
)
async def test_library_read_does_not_open_the_tools_read_tools(
    db: AsyncSession, make_context: Any, tool: str
) -> None:
    # The scope is per tool: everything but list_library still needs tools:read.
    only_library = make_context({"library:read"})
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(db, only_library, _invocation(tool))


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"limit": "5"}, "invalid argument types"),
        ({"limit": 5.0}, "invalid argument types"),
        ({"limit": True}, "invalid argument types"),
        ({"limit": None}, "invalid argument types"),
        ({"limit": 0}, "invalid argument types"),
        ({"limit": 51}, "invalid argument types"),
        ({"offset": "0"}, "invalid argument types"),
        ({"offset": -1}, "invalid argument types"),
        ({"offset": 2**31}, "invalid argument types"),
        ({"unexpected": 1}, "unknown arguments"),
        ({"project_id": str(PROJECT)}, "unknown arguments"),
        ({"workspace_id": str(WORKSPACE)}, "unknown arguments"),
        ({"user_id": str(USER)}, "unknown arguments"),
    ],
    ids=[
        "string-limit",
        "float-limit",
        "bool-limit",
        "null-limit",
        "zero-limit",
        "huge-limit",
        "string-offset",
        "negative-offset",
        "huge-offset",
        "extra-key",
        "project-selector",
        "workspace-identity",
        "user-identity",
    ],
)
@pytest.mark.parametrize(
    "make_context", [_project_context, _workspace_context], ids=["project", "workspace"]
)
async def test_list_library_arguments_are_strict(
    db: AsyncSession,
    make_context: Any,
    arguments: dict[str, Any],
    message: str,
) -> None:
    # Local tools validate as strictly as the registry ones, and a workspace
    # grant gets no project selector here: the tool lists the whole scope.
    with pytest.raises(ToolArgumentError, match=message):
        await invoke_read(
            db, make_context(LIBRARY_READ), _invocation("list_library", **arguments)
        )


async def test_list_library_pages_by_name_with_next_offset(db: AsyncSession) -> None:
    context = _workspace_context(LIBRARY_READ)
    first = await invoke_read(db, context, _invocation("list_library", limit=1))
    assert [f["name"] for f in first.content[0]["folders"]] == ["Other"]
    assert (first.content[0]["offset"], first.content[0]["next_offset"]) == (0, 1)
    second = await invoke_read(
        db, context, _invocation("list_library", limit=1, offset=1)
    )
    assert [f["name"] for f in second.content[0]["folders"]] == ["Project"]
    assert second.content[0]["next_offset"] is None
    past_the_end = await invoke_read(db, context, _invocation("list_library", offset=2))
    assert past_the_end.is_error is False
    assert past_the_end.content[0]["folders"] == []
    assert past_the_end.content[0]["next_offset"] is None


async def test_list_library_breaks_name_ties_by_id_across_pages(
    db: AsyncSession, executed_sql: list[tuple[str, Any]]
) -> None:
    # Two folders with one name, inserted in the opposite order of their ids:
    # the id tie-break puts them in the same order on every query, which paging
    # by offset needs so that no folder is skipped or listed twice.
    low, high = UUID(int=3), UUID(int=4)
    await db.execute(
        insert(Collection).values(
            [
                dict(id=high, name="Twin", workspace_id=WORKSPACE),
                dict(id=low, name="Twin", workspace_id=WORKSPACE),
            ]
        )
    )
    await db.commit()
    context = _workspace_context(LIBRARY_READ)
    seen: list[str] = []
    next_offset: int | None = 0
    while next_offset is not None:
        page = await invoke_read(
            db, context, _invocation("list_library", limit=1, offset=next_offset)
        )
        seen += [folder["id"] for folder in page.content[0]["folders"]]
        next_offset = page.content[0]["next_offset"]
    assert seen == [str(OTHER_PROJECT), str(PROJECT), str(low), str(high)]
    # SQLite hands equal names back in id order whatever the query says, but
    # PostgreSQL does not: so the clause itself is pinned as well.
    pages = [sql for sql, _ in executed_sql if "ORDER BY" in sql]
    assert pages and all(
        "ORDER BY collections.name, collections.id" in sql for sql in pages
    )


@pytest.mark.parametrize(
    "text",
    ["\u6587" * 500, "\U0001f600" * 500],
    ids=["cjk-description", "emoji-name-and-description"],
)
async def test_list_library_pages_by_bytes_so_a_full_page_fits_the_result_cap(
    db: AsyncSession, text: str
) -> None:
    # Names and descriptions are free text and Collection.description is
    # unbounded, while the cap counts UTF-8 bytes: 50 folders with a
    # 500-character CJK description are ~80 KB. A page that cannot fit must
    # end earlier and say where to resume, not turn into result_too_large.
    await db.execute(
        update(Collection)
        .where(Collection.workspace_id == WORKSPACE)
        .values(is_deleted=True)
    )
    await db.execute(
        insert(Collection).values(
            [
                dict(
                    id=UUID(int=100 + number),
                    name=f"{number:02d} {text[:100]}",
                    description=text,
                    workspace_id=WORKSPACE,
                )
                for number in range(60)
            ]
        )
    )
    await db.commit()
    context = _workspace_context(LIBRARY_READ)
    seen: list[str] = []
    sizes: list[int] = []
    pages = 0
    next_offset: int | None = 0
    while next_offset is not None:
        # The first page asks for the default limit: the caller does not have
        # to know it should have asked for fewer.
        arguments = {"offset": next_offset} if pages else {}
        page = await invoke_read(db, context, _invocation("list_library", **arguments))
        assert page.is_error is False, page.content
        body = page.content[0]
        folders = body["folders"]
        if not pages:
            assert 1 <= len(folders) < MAX_RESULTS  # shortened, not failed
        assert (body["offset"], body["next_offset"]) == (
            next_offset,
            None if len(seen) + len(folders) == 60 else next_offset + len(folders),
        )
        sizes.append(len(json.dumps(body, ensure_ascii=False).encode()))
        seen += [folder["id"] for folder in folders]
        next_offset = body["next_offset"]
        pages += 1
        assert pages <= 60, "paging made no progress"
    assert max(sizes) <= read_tools.MAX_RESULT_BYTES
    assert pages > 1
    # Every folder once, in name order, whatever the page boundaries were.
    assert seen == [str(UUID(int=100 + number)) for number in range(60)]


async def test_list_library_counts_what_list_project_documents_reports(
    db: AsyncSession,
) -> None:
    # Rows a project's document list never shows must not be counted either:
    # an unlinked document, a deleted document and another organization's.
    other_org = uuid4()
    await db.execute(
        insert(Organization).values(id=other_org, name="Other", storage_limit_bytes=1)
    )
    # (title, changes to the document, changes to its link into PROJECT)
    extra: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        ("live", {}, {}),
        ("unlinked", {}, {"is_deleted": True}),
        ("deleted", {"is_deleted": True}, {}),
        ("foreign", {"organization_id": other_org}, {}),
    ]
    for title, document_changes, link_changes in extra:
        document_id = uuid4()
        await db.execute(
            insert(Document).values(
                **{**_document_row(document_id, title), **document_changes}
            )
        )
        await db.execute(
            insert(CollectionDocument).values(
                collection_id=PROJECT, document_id=document_id, **link_changes
            )
        )
    await db.commit()
    listed = await invoke_read(
        db,
        _workspace_context(),
        _invocation("list_project_documents", project_id=str(PROJECT)),
    )
    library = await invoke_read(
        db, _workspace_context(LIBRARY_READ), _invocation("list_library")
    )
    assert listed.content[0]["total"] == 2  # DOC_IN_PROJECT and `live`
    assert _library_folders(library)[str(PROJECT)]["document_count"] == 2


async def test_list_library_skips_deleted_collections_and_bounds_descriptions(
    db: AsyncSession,
) -> None:
    await db.execute(
        update(Collection)
        .where(Collection.id == PROJECT)
        .values(description="d" * 1000)
    )
    retired = uuid4()
    await db.execute(
        insert(Collection).values(
            id=retired, name="Retired", workspace_id=WORKSPACE, is_deleted=True
        )
    )
    await db.commit()
    result = await invoke_read(
        db, _workspace_context(LIBRARY_READ), _invocation("list_library")
    )
    folders = _library_folders(result)
    assert str(retired) not in folders
    assert folders[str(PROJECT)]["description"] == "d" * 500
    assert folders[str(OTHER_PROJECT)]["description"] == ""  # never null


async def test_list_library_of_an_empty_workspace_is_an_empty_page(
    db: AsyncSession,
) -> None:
    await db.execute(
        update(Collection)
        .where(Collection.workspace_id == WORKSPACE)
        .values(is_deleted=True)
    )
    await db.commit()
    result = await invoke_read(
        db, _workspace_context(LIBRARY_READ), _invocation("list_library")
    )
    assert result.is_error is False
    assert result.content[0]["folders"] == []
    assert result.content[0]["next_offset"] is None


async def test_list_library_is_denied_once_its_workspace_is_deleted(
    db: AsyncSession,
) -> None:
    await db.execute(
        update(Workspace).where(Workspace.id == WORKSPACE).values(is_deleted=True)
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(
            db, _workspace_context(LIBRARY_READ), _invocation("list_library")
        )


async def _add_user(db: AsyncSession) -> UUID:
    """A live, active user of ORG who owns nothing and belongs to nothing."""
    user_id = uuid4()
    await db.execute(
        text(
            "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, :email, 'unused', 'Other', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
        ),
        {"id": str(user_id), "org": str(ORG), "email": f"{user_id}@example.test"},
    )
    await db.commit()
    return user_id


def _library_context_of(user_id: UUID) -> IntegrationContext:
    return IntegrationContext(
        user_id=user_id,
        organization_id=ORG,
        workspace_id=WORKSPACE,
        grant_id=uuid4(),
        scopes=LIBRARY_READ,
    )


async def test_list_library_for_an_unknown_user_is_denied(db: AsyncSession) -> None:
    # No such user row: refused before the library is looked at. The owner and
    # member rule is pinned by the next tests, with users that do exist.
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(db, _library_context_of(uuid4()), _invocation("list_library"))


@pytest.mark.parametrize("membership", ["none", "revoked"])
async def test_list_library_for_a_user_outside_the_workspace_is_denied(
    db: AsyncSession, membership: str
) -> None:
    from src.models.workspace import WorkspaceRole

    # A real, active user of the same organization who neither owns the
    # workspace nor holds a live membership: only authorized_workspace's
    # owner-or-member rule stands between them and the folder names.
    stranger = await _add_user(db)
    if membership == "revoked":
        await db.execute(
            insert(WorkspaceMember).values(
                workspace_id=WORKSPACE,
                user_id=stranger,
                role=WorkspaceRole.VIEWER,
                is_deleted=True,
            )
        )
        await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(
            db, _library_context_of(stranger), _invocation("list_library")
        )


async def test_list_library_for_a_live_member_is_allowed(db: AsyncSession) -> None:
    from src.models.workspace import WorkspaceRole

    # The control for the two denials above: membership is the discriminator.
    member = await _add_user(db)
    await db.execute(
        insert(WorkspaceMember).values(
            workspace_id=WORKSPACE, user_id=member, role=WorkspaceRole.VIEWER
        )
    )
    await db.commit()
    result = await invoke_read(
        db, _library_context_of(member), _invocation("list_library")
    )
    assert set(_library_folders(result)) == {str(PROJECT), str(OTHER_PROJECT)}


@pytest.mark.parametrize(
    "make_context", [_project_context, _workspace_context], ids=["project", "workspace"]
)
async def test_list_library_follows_membership_but_counts_documents_by_organization(
    db: AsyncSession, make_context: Any
) -> None:
    from src.models.workspace import WorkspaceRole

    # The tenancy contract (workspace_access.py, docs/engineering/backend.md,
    # data-isolation-matrix.md) is two-scoped: workspaces and the folders in
    # them follow membership, and a member can be invited from any
    # organization; documents follow the organization. A workspace grant must
    # not narrow the first to the second (no Workspace.organization_id filter),
    # and must not widen the second to the first.
    owning = uuid4()
    shared_workspace, shared_folder = uuid4(), uuid4()
    theirs, mine = uuid4(), uuid4()
    await db.execute(
        insert(Organization).values(
            id=owning, name="Owning organization", storage_limit_bytes=1
        )
    )
    await db.execute(
        insert(Workspace).values(
            id=shared_workspace, name="Shared", owner_id=uuid4(), organization_id=owning
        )
    )
    await db.execute(
        insert(Collection).values(
            id=shared_folder,
            name="Shared thesis",
            description="notes of the owning organization",
            workspace_id=shared_workspace,
        )
    )
    await db.execute(
        insert(Document).values(
            [
                {**_document_row(theirs, "theirs"), "organization_id": owning},
                _document_row(mine, "mine"),
            ]
        )
    )
    await db.execute(
        insert(CollectionDocument).values(
            [
                dict(collection_id=shared_folder, document_id=theirs),
                dict(collection_id=shared_folder, document_id=mine),
            ]
        )
    )
    await db.execute(
        insert(WorkspaceMember).values(
            workspace_id=shared_workspace, user_id=USER, role=WorkspaceRole.VIEWER
        )
    )
    await db.commit()
    scopes = TOOLS_READ | LIBRARY_READ
    context = (
        _workspace_context(scopes).model_copy(update={"workspace_id": shared_workspace})
        if make_context is _workspace_context
        else _project_context(scopes).model_copy(update={"project_id": shared_folder})
    )

    library = await invoke_read(db, context, _invocation("list_library"))
    # The folder is listed: its name and description are workspace metadata.
    assert library.is_error is False
    (folder,) = library.content[0]["folders"]
    assert folder["id"] == str(shared_folder)
    assert folder["name"] == "Shared thesis"
    assert folder["description"] == "notes of the owning organization"
    # Its documents are counted per the grant's organization: the owning
    # organization's document is neither counted nor named.
    assert folder["document_count"] == 1
    assert str(theirs) not in json.dumps(library.content)

    # Everything behind the folder is organization-scoped and stays refused.
    selector = {"project_id": str(shared_folder)} if context.workspace_id else {}
    for tool, arguments in (
        ("list_project_documents", {}),
        ("search_documents", {"query": "mine"}),
        ("get_current_draft", {}),
    ):
        refused = await invoke_read(
            db, context, _invocation(tool, **selector, **arguments)
        )
        assert refused.is_error is True
        assert refused.content == [{"error": "Project not found or access denied"}]
        assert refused.source_refs == []


# What ends a project grant's access. list_library resolves the scope again on
# every call, so each change must turn the next call into a denial.
LOST_ACCESS: dict[str, Any] = {
    "collection-deleted": update(Collection)
    .where(Collection.id == PROJECT)
    .values(is_deleted=True),
    "workspace-deleted": update(Workspace).values(is_deleted=True),
    "owner-changed-without-membership": update(Workspace).values(owner_id=uuid4()),
    "user-inactive": update(User).values(is_active=False),
    "organization-inactive": update(Organization).values(is_active=False),
}


@pytest.mark.parametrize("change", sorted(LOST_ACCESS))
async def test_list_library_of_a_project_grant_is_denied_once_access_is_lost(
    db: AsyncSession, change: str
) -> None:
    context = _project_context(LIBRARY_READ)
    allowed = await invoke_read(db, context, _invocation("list_library"))
    assert [f["id"] for f in allowed.content[0]["folders"]] == [str(PROJECT)]
    await db.execute(LOST_ACCESS[change])
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(db, context, _invocation("list_library"))


async def test_list_library_of_a_project_grant_follows_membership(
    db: AsyncSession,
) -> None:
    from src.models.workspace import WorkspaceRole

    context = _project_context(LIBRARY_READ)
    # The owner leaves; a live membership alone keeps the folder listed...
    await db.execute(update(Workspace).values(owner_id=uuid4()))
    await db.execute(
        insert(WorkspaceMember).values(
            workspace_id=WORKSPACE, user_id=USER, role=WorkspaceRole.VIEWER
        )
    )
    await db.commit()
    kept = await invoke_read(db, context, _invocation("list_library"))
    assert [f["id"] for f in kept.content[0]["folders"]] == [str(PROJECT)]
    # ...and revoking it must not leave the name and description readable.
    await db.execute(update(WorkspaceMember).values(is_deleted=True))
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(db, context, _invocation("list_library"))


@pytest.mark.parametrize(
    "change",
    [
        "user-inactive",
        "user-moved-to-another-organization",
        "grant-organization-inactive",
        "grant-organization-deleted",
    ],
)
async def test_list_library_is_denied_once_the_user_or_grant_organization_lapses(
    db: AsyncSession, change: str
) -> None:
    from src.models.workspace import WorkspaceRole

    # An invited member of a workspace another live organization owns. The
    # owner's organization is not the grant's, so the grant organization's own
    # liveness is a fact of its own: were they one, the owning-organization
    # clause would refuse first. (invoke_read refuses a user of another
    # organization itself, so that case is pinned in test_context.py too, at
    # the level of authorized_workspace.)
    owning = uuid4()
    await db.execute(
        insert(Organization).values(
            id=owning, name="Owning organization", storage_limit_bytes=1
        )
    )
    await db.execute(
        update(Workspace)
        .where(Workspace.id == WORKSPACE)
        .values(owner_id=uuid4(), organization_id=owning)
    )
    await db.execute(
        insert(WorkspaceMember).values(
            workspace_id=WORKSPACE, user_id=USER, role=WorkspaceRole.VIEWER
        )
    )
    await db.commit()
    context = _workspace_context(LIBRARY_READ)
    listed = await invoke_read(db, context, _invocation("list_library"))
    assert set(_library_folders(listed)) == {str(PROJECT), str(OTHER_PROJECT)}

    lapse = {
        "user-inactive": update(User).values(is_active=False),
        "user-moved-to-another-organization": update(User).values(
            organization_id=owning
        ),
        "grant-organization-inactive": update(Organization)
        .where(Organization.id == ORG)
        .values(is_active=False),
        "grant-organization-deleted": update(Organization)
        .where(Organization.id == ORG)
        .values(is_deleted=True),
    }[change]
    await db.execute(lapse)
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(db, context, _invocation("list_library"))


@pytest.mark.parametrize("model", [Collection, Workspace])
async def test_library_query_itself_rechecks_ancestors(
    db: AsyncSession, model: Any
) -> None:
    # The scope's access check ran in a separate statement; the page query
    # alone must return nothing once a Collection or its Workspace is
    # soft-deleted.
    scope = Collection.workspace_id == WORKSPACE
    assert len(await read_tools._library_folders(db, scope, ORG, 0, 10)) == 2
    await db.execute(update(model).values(is_deleted=True))
    await db.commit()
    assert await read_tools._library_folders(db, scope, ORG, 0, 10) == []


async def test_list_library_binds_no_value_per_collection(
    db: AsyncSession, executed_sql: list[tuple[str, Any]]
) -> None:
    # asyncpg refuses a statement that binds more than 32,767 values, and the
    # collection routes put no cap on a workspace: one editor can reach that
    # many folders and make list_library answer 500 for every member. SQLite
    # would take that many ids, so count what is bound instead. The scope must
    # reach the query as a condition, and what the query binds must not grow
    # with the number of Collections in the workspace.
    async def most_bound() -> int:
        executed_sql.clear()
        result = await invoke_read(
            db, _workspace_context(LIBRARY_READ), _invocation("list_library")
        )
        assert result.is_error is False
        assert executed_sql, "the recorder saw no statement"
        return max(
            len(list(_bound_values(parameters))) for _sql, parameters in executed_sql
        )

    before = await most_bound()
    await db.execute(
        insert(Collection).values(
            [
                dict(id=uuid4(), name=f"Folder {n:03}", workspace_id=WORKSPACE)
                for n in range(60)
            ]
        )
    )
    await db.commit()
    assert await most_bound() == before
    # The scope still selects the whole workspace, and only it: the first page
    # of 62 folders by name, none of them from the user's other workspace.
    grown = await invoke_read(
        db, _workspace_context(LIBRARY_READ), _invocation("list_library")
    )
    assert [f["name"] for f in grown.content[0]["folders"]] == [
        f"Folder {n:03}" for n in range(MAX_RESULTS)
    ]
    assert grown.content[0]["next_offset"] == MAX_RESULTS


def test_catalog_offers_a_tool_only_to_grants_holding_its_scope() -> None:
    def offered(scopes: Iterable[str] | None) -> set[str]:
        return {tool.name for tool in list_read_tools(scopes)}

    # Everything callable with tools:read: the registry tools and every local
    # tool but the library listing.
    tools_read = set(READ_TOOL_NAMES) | set(read_tools.LOCAL_TOOLS) - {"list_library"}
    assert offered(TOOLS_READ) == tools_read
    assert offered(LIBRARY_READ) == tools_read | {"list_library"}
    assert offered({"library:read"}) == {"list_library"}
    assert offered(frozenset()) == set()
    assert offered(None) == tools_read | {"list_library"}


@pytest.mark.parametrize("workspace_bound", [False, True])
def test_list_library_advertises_no_project_selector(workspace_bound: bool) -> None:
    catalog = list_read_tools(LIBRARY_READ, workspace_bound=workspace_bound)
    tool = next(tool for tool in catalog if tool.name == "list_library")
    assert tool.description
    schema = tool.input_schema
    assert set(schema["properties"]) == {"limit", "offset"}
    assert schema["additionalProperties"] is False
    assert schema["required"] == []
    assert schema["properties"]["limit"]["maximum"] == 50


def test_local_tools_are_scoped_and_well_formed() -> None:
    # Every later local tool must satisfy this, or it fails.
    for name, (description, schema, scope) in read_tools.LOCAL_TOOLS.items():
        assert description and read_tools.TOOL_SCOPES[name] == scope
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])
        # The project selector is added per grant; no identity is ever a
        # property of the shared table.
        assert not set(schema["properties"]) & read_tools.IDENTITY_ARGUMENTS


WRONGLY_TYPED: dict[str, list[Any]] = {
    "integer": ["5", 5.0, True, None, [1], {"a": 1}],
    "string": [5, 5.0, True, None, ["x"], {"a": 1}],
    "array": ["x", 5, None, {"a": 1}],
}


def test_local_tool_arguments_are_checked_without_coercion() -> None:
    # The registry tools validate with pydantic strict=True; a local tool must
    # not be weaker: no "5" -> 5, 5.0 -> 5, True -> 1 or None -> default.
    for name, (_description, schema, _scope) in read_tools.LOCAL_TOOLS.items():
        for prop, spec in schema["properties"].items():
            for bad in WRONGLY_TYPED[spec["type"]]:
                with pytest.raises(ToolArgumentError, match="invalid argument types"):
                    read_tools._check_local_arguments(schema, {prop: bad})


def _edge_values(spec: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """Values on and just past each bound a property's schema declares."""
    accepted: list[Any] = []
    rejected: list[Any] = []
    if spec["type"] == "integer":
        for bound, step in (("minimum", -1), ("maximum", 1)):
            if bound in spec:
                accepted.append(spec[bound])
                rejected.append(spec[bound] + step)
    elif spec["type"] == "string":
        if "enum" in spec:
            accepted += spec["enum"]
            rejected.append("not-a-member")
        for bound, step in (("minLength", -1), ("maxLength", 1)):
            if bound in spec:
                accepted.append("x" * spec[bound])
                rejected.append("x" * (spec[bound] + step))
    else:  # arrays of strings, in every local schema
        for bound, step in (("minItems", -1), ("maxItems", 1)):
            if bound in spec:
                accepted.append(["x"] * spec[bound])
                rejected.append(["x"] * (spec[bound] + step))
    return accepted, rejected


def test_local_tool_arguments_are_held_to_every_bound_of_their_schema() -> None:
    # The advertised schema is the one enforced, so nothing a local tool
    # declares (a range, a length, an item count, an enum) is advisory. This
    # is how the tools develop added after list_library (get_document_content,
    # retrieve_passages, get_arxiv_paper_content, find_researchers,
    # get_researcher) are validated as strictly as the registry tools'
    # pydantic models, and a tool added later is held to it with no new test.
    checked = 0
    for name, (_description, schema, _scope) in read_tools.LOCAL_TOOLS.items():
        for prop, spec in schema["properties"].items():
            accepted, rejected = _edge_values(spec)
            for value in accepted:
                read_tools._check_local_arguments(schema, {prop: value})
            for value in rejected:
                with pytest.raises(ToolArgumentError, match="invalid argument types"):
                    read_tools._check_local_arguments(schema, {prop: value})
            checked += len(accepted) + len(rejected)
    assert checked >= 20, "the local schemas should declare bounds to check"


@pytest.mark.parametrize("name", sorted(read_tools.LOCAL_TOOLS))
def test_local_tools_refuse_unknown_and_missing_arguments(name: str) -> None:
    context = _project_context(TOOLS_READ | LIBRARY_READ)
    with pytest.raises(ToolArgumentError, match="unknown arguments"):
        read_tools._validate_arguments(context, _invocation(name, unexpected=1))
    if read_tools.LOCAL_TOOLS[name][1]["required"]:
        with pytest.raises(ToolArgumentError, match="missing required arguments"):
            read_tools._validate_arguments(context, _invocation(name))


def test_registry_tools_need_tools_read_and_library_tools_need_library_read() -> None:
    assert {read_tools.TOOL_SCOPES[name] for name in READ_TOOL_NAMES} == {"tools:read"}
    assert read_tools.TOOL_SCOPES["list_library"] == "library:read"


def test_catalog_schemas_are_copies_of_the_local_table() -> None:
    def advertised() -> dict[str, Any]:
        catalog = list_read_tools(LIBRARY_READ)
        return next(
            tool for tool in catalog if tool.name == "list_library"
        ).input_schema

    advertised()["properties"]["limit"]["maximum"] = 10**6
    assert advertised()["properties"]["limit"]["maximum"] == 50
    assert (
        read_tools.LOCAL_TOOLS["list_library"][1]["properties"]["limit"]["maximum"]
        == 50
    )
