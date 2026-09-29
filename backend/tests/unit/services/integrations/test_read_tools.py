"""Scoped integration read gateway, using a local SQLite database."""

from typing import Any, AsyncIterator
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.collection import Collection, CollectionDocument
from src.models.document import Document, DocumentType
from src.models.generated_draft import GeneratedDraft
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
    assert names == set(READ_TOOL_NAMES)
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
    result = await invoke_read(db, context, _invocation("search_documents", query="retrieval"))
    assert result.is_error is False
    assert [doc["id"] for doc in result.content[0]["documents"]] == [str(DOC_IN_PROJECT)]
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


async def test_valid_retrieval_preserves_source_identity(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = AsyncMock(
        return_value={
            "chunks": [
                {"document_id": str(DOC_IN_PROJECT), "chunk_id": "c1", "text": "hit"}
            ],
            "total": 1,
        }
    )
    monkeypatch.setattr(read_tools, "_tool_do_kb_retrieve", retrieval)
    result = await invoke_read(
        db,
        context,
        _invocation("do_kb_retrieve", query="q", document_ids=[str(DOC_IN_PROJECT)]),
    )
    assert result.is_error is False
    assert result.source_refs == [{"document_id": str(DOC_IN_PROJECT), "chunk_id": "c1"}]
    args = retrieval.await_args.args[0]
    assert args["project_id"] == str(PROJECT)
    assert args["document_ids"] == [str(DOC_IN_PROJECT)]


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
