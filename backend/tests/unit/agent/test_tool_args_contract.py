"""Agent audit round 8, Partition B: tool arguments on the production path.

Production dispatch is ``_nodes_tools._execute_single_tool`` ->
``graph._get_execute_tool()`` -> ``tools_impl.execute_tool`` -> ``_tool_*``.
The ``@tool`` wrappers in ``tools.py`` only supply the LLM schema. #1716 added
schema validation on that path, which injected ``None`` for every omitted
optional (B1: ``do_kb_retrieve`` read ``document_ids: None`` as an invalid
scope) and dropped server-injected keys the schema lacks (B2: the page
project never reached retrieval). Nothing caught it because every tool test
called an impl directly. The parametrised class test below drives each
registered tool through the real entry point instead.

Mutation checks (each test was observed RED with the guard removed):
- B1 ``tools_impl._dispatch_tool`` drops ``None`` args -> remove it and the
  class test fails for 11 tools. ``_tool_do_kb_retrieve`` keys scoped intent
  on ``document_ids is not None`` -> revert to ``"document_ids" in args`` and
  ``test_unscoped_do_kb_retrieve_returns_chunks[impl_with_explicit_none]``
  fails; revert both and the ``[execute_tool]`` case fails too.
- B2 ``_dispatch_tool`` forwards ``project_id`` into ``_tool_do_kb_retrieve``.
  Remove it -> ``test_page_project_scopes_do_kb_retrieve`` fails.
- B4 ``_tool_list_project_documents`` trims its page to the result cap.
  Remove it -> ``test_list_project_documents_page_survives_the_result_cap`` fails.
- B6 ``_nodes_tools._execute_single_tool`` catches ``ValidationError`` first.
  Remove it -> ``test_mistyped_mutation_argument_is_retryable_invalid_arguments``
  fails with ``operation_context_invalid``.

    pytest -q backend/tests/unit/agent/test_tool_args_contract.py
"""

from __future__ import annotations

import contextlib
import json
import uuid
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from src.services.agent import tool_operations, tool_session, tools_impl
from src.services.agent._nodes_tools import _execute_single_tool
from src.services.agent.tools import TOOL_REGISTRY

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

USER_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ORG_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PROJECT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
DOCUMENT_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")


def _schema(name: str) -> dict[str, Any]:
    descriptor = TOOL_REGISTRY.descriptor(name)
    assert descriptor is not None
    return cast(type[BaseModel], descriptor.tool.args_schema).model_json_schema()


_IMPL_NAMES = {"ingest_arxiv_papers": "_tool_ingest_arxiv"}
_ENABLED_TOOLS = sorted(d.name for d in TOOL_REGISTRY.descriptors if d.enabled)
# Tools whose impl must see the page project: every schema that declares
# ``project_id`` (filled by ``_with_injected_project_id``) plus the two that
# take it as server-owned context.
_PROJECT_SCOPED = {
    name for name in _ENABLED_TOOLS if "project_id" in _schema(name)["properties"]
} | {"do_kb_retrieve", "load_project_skill"}


class _FakeSession:
    def in_transaction(self) -> bool:
        return False

    def begin(self) -> contextlib.nullcontext[None]:
        return contextlib.nullcontext()

    def begin_nested(self) -> contextlib.nullcontext[None]:
        return contextlib.nullcontext()

    async def commit(self) -> None:
        return None


@contextlib.asynccontextmanager
async def _fake_tool_session() -> AsyncIterator[_FakeSession]:
    yield _FakeSession()


@pytest.fixture
def production_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Real executor; only the DB session, actor load and claim store are faked."""
    user = SimpleNamespace(id=USER_ID, organization_id=ORG_ID)
    monkeypatch.setattr(tool_session, "tool_session", _fake_tool_session)
    monkeypatch.setattr(tool_session, "resolve_tool_user", AsyncMock(return_value=user))
    monkeypatch.setattr(
        tool_operations,
        "claim_operation",
        AsyncMock(
            return_value=tool_operations.OperationClaim(
                status="claimed", operation_id="op-1", owner_token=uuid.uuid4()
            )
        ),
    )
    monkeypatch.setattr(tool_operations, "complete_operation", AsyncMock())
    with patch(
        "src.services.agent.graph._get_execute_tool",
        return_value=tools_impl.execute_tool,
    ):
        yield


def _config() -> RunnableConfig:
    return {
        "configurable": {
            "user_id": str(USER_ID),
            "organization_id": str(ORG_ID),
            "thread_id": "thread-1",
            "project_id": PROJECT_ID,
            "runtime_snapshot_id": "snapshot-1",
            "page_context": _PAGE,
        }
    }


_PAGE = {"type": "project", "project_id": PROJECT_ID}
_OPERATION_CONTEXT = {
    "tool_operation_protocol_version": 1,
    "tool_operation_turn_id": "turn-1",
}


async def _run(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return await _execute_single_tool(
        {"name": name, "args": args, "id": f"call-{name}"},
        _config(),
        _PAGE,
        _OPERATION_CONTEXT,
    )


def _sample(prop: dict[str, Any]) -> Any:
    if "enum" in prop:
        return prop["enum"][0]
    if "const" in prop:
        return prop["const"]
    for option in prop.get("anyOf", []):
        if option.get("type") != "null":
            return _sample(option)
    kind = str(prop.get("type"))
    if kind == "array":
        return [_sample(prop.get("items", {"type": "string"}))]
    return {
        "string": "value",
        "integer": 1,
        "number": 1.0,
        "boolean": True,
        "object": {},
    }[kind]


def _llm_minimal_args(name: str) -> dict[str, Any]:
    """Required fields only — what a model emits when it omits every optional."""
    schema = _schema(name)
    return {
        field: _sample(schema["properties"][field])
        for field in schema.get("required", [])
    }


@pytest.mark.usefixtures("production_path")
@pytest.mark.parametrize("name", _ENABLED_TOOLS)
async def test_every_tool_gets_llm_minimal_args_without_injected_nones(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    impl = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(tools_impl, _IMPL_NAMES.get(name, f"_tool_{name}"), impl)

    result = await _run(name, _llm_minimal_args(name))

    assert impl.await_args is not None, result["execution"]
    call = impl.await_args
    received = call.args[0] if call.args else {"query": call.kwargs["query"]}
    injected_nones = sorted(key for key, value in received.items() if value is None)
    assert injected_nones == [], f"{name} impl received omitted optionals as None"
    if name in _PROJECT_SCOPED:
        scope = received.get("project_id") or call.kwargs.get("project_id")
        assert scope == PROJECT_ID, f"{name} lost the server-injected project scope"


def _chunk_patches(
    resolve: AsyncMock,
) -> contextlib.ExitStack:
    from src.services.do_kb.models import Chunk, RetrieveResult
    from src.services.do_kb.retrieval import DOKBRetrieveOutcome, DOKBRetrieveStatus

    chunk = Chunk(
        text="METR found tasks double every seven months.",
        score=0.9,
        document_id="storage-key-1",
        metadata={"score_source": "upstream"},
    )
    resolve.return_value = (
        {"storage-key-1": (str(DOCUMENT_ID), "METR study")},
        [chunk],
    )
    settings = SimpleNamespace(
        DO_KB_ENABLED=True,
        DO_KB_DEFAULT_TOP_K=8,
        AGENT_DOKB_COHERE_RERANK=False,
        AGENT_ITERATIVE_RETRIEVAL=False,
    )
    stack = contextlib.ExitStack()
    stack.enter_context(patch("src.core.config.settings", settings))
    stack.enter_context(
        patch(
            "src.services.do_kb.retrieval.resolve_org_kb_uuid",
            AsyncMock(return_value="kb-1"),
        )
    )
    stack.enter_context(
        patch(
            "src.services.do_kb.retrieval.retrieve_kb_chunks",
            AsyncMock(
                return_value=DOKBRetrieveOutcome(
                    status=DOKBRetrieveStatus.SUCCESS,
                    result=RetrieveResult(chunks=[chunk], total=1),
                )
            ),
        )
    )
    stack.enter_context(
        patch("src.services.do_kb.resolve.resolve_and_filter_chunks", resolve)
    )
    return stack


@pytest.mark.parametrize("path", ["execute_tool", "impl_with_explicit_none"])
async def test_unscoped_do_kb_retrieve_returns_chunks(path: str) -> None:
    """B1: ``{"query": ...}`` used to come back as ``invalid_document_scope``."""
    resolve = AsyncMock()
    user = SimpleNamespace(id=USER_ID, organization_id=ORG_ID)
    with _chunk_patches(resolve):
        if path == "execute_tool":
            result = await tools_impl.execute_tool(
                "do_kb_retrieve",
                {"query": "METR study findings"},
                db=MagicMock(),
                current_user=user,  # type: ignore[arg-type]
            )
        else:
            # Direct callers (integrations) bypass the dispatcher's None drop.
            result = await tools_impl._tool_do_kb_retrieve(
                {"query": "METR study findings", "document_ids": None},
                MagicMock(),
                user,  # type: ignore[arg-type]
            )

    assert result.get("reason") != "invalid_document_scope", result
    assert [chunk["document_id"] for chunk in result["chunks"]] == [str(DOCUMENT_ID)]


async def test_explicit_empty_document_scope_still_refuses() -> None:
    """The fix must not widen an explicit (invalid) empty scope to org-wide."""
    user = SimpleNamespace(id=USER_ID, organization_id=ORG_ID)
    result = await tools_impl.execute_tool(
        "do_kb_retrieve",
        {"query": "METR", "document_ids": []},
        db=MagicMock(),
        current_user=user,  # type: ignore[arg-type]
    )

    assert result["reason"] == "invalid_document_scope"


@pytest.mark.usefixtures("production_path")
async def test_page_project_scopes_do_kb_retrieve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2: the page project was validated away and retrieval ran org-wide."""
    project = SimpleNamespace(id=uuid.UUID(PROJECT_ID), name="P")
    ownership = AsyncMock(return_value=project)
    monkeypatch.setattr(tools_impl, "_verify_project_ownership", ownership)
    resolve = AsyncMock()

    with _chunk_patches(resolve):
        out = await _run("do_kb_retrieve", {"query": "METR study findings"})

    assert out["execution"]["status"] == "completed", out["execution"]
    assert ownership.await_args is not None and resolve.await_args is not None
    assert ownership.await_args.args[0] == PROJECT_ID
    assert resolve.await_args.kwargs["project_id"] == PROJECT_ID


async def test_list_project_documents_page_survives_the_result_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B4: past 32 KiB the cap stripped ``documents`` but kept ``returned``."""
    project = SimpleNamespace(id=uuid.UUID(PROJECT_ID), name="Big project")
    monkeypatch.setattr(
        tools_impl, "_verify_project_ownership", AsyncMock(return_value=project)
    )
    docs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            title=f"Document {index} " + "long title words " * 10,
            document_type=None,
            processing_status=None,
        )
        for index in range(300)
    ]
    count = MagicMock()
    count.scalar_one.return_value = len(docs)
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = docs
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[count, rows])
    user = SimpleNamespace(id=USER_ID, organization_id=ORG_ID)

    raw = await tools_impl._tool_list_project_documents(
        {"project_id": PROJECT_ID, "limit": 500}, db, user  # type: ignore[arg-type]
    )
    capped = tools_impl._cap_tool_result(raw, tool_name="list_project_documents")

    assert "documents" in capped, "the cap stripped the page"
    assert capped["returned"] == len(capped["documents"])
    assert 0 < capped["returned"] < len(docs)
    assert capped["has_more"] is True
    assert [row["id"] for row in capped["documents"]] == [
        str(doc.id) for doc in docs[: capped["returned"]]
    ]
    assert len(json.dumps(capped)) <= tools_impl._MAX_TOOL_RESULT_BYTES


async def test_mistyped_mutation_argument_is_retryable_invalid_arguments() -> None:
    """B6: a bad ``tags`` type said "Start a new turn" instead of "fix tags"."""
    executor = AsyncMock(return_value={"status": "ok"})
    with patch("src.services.agent.graph._get_execute_tool", return_value=executor):
        out = await _run(
            "create_project_note",
            {"title": "Notes", "content": "Body", "tags": "ml"},
        )

    executor.assert_not_awaited()
    payload = json.loads(out["message"].content)
    assert payload["error_category"] == "invalid_tool_arguments"
    assert payload["automatic_retry_allowed"] is True
    assert "new turn" not in payload.get("retry_guidance", "").lower()
