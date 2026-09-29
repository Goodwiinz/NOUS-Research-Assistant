"""The role prompts must stay executable against the decorated tool schemas."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from pydantic import TypeAdapter

from src.services.agent.tools import TOOL_REGISTRY

BACKEND_ROOT = Path(__file__).parents[3]
ASSET_ROOT = BACKEND_ROOT / "src/services/agent/subgraphs"
ASSET_NAMES = ("research", "writing", "data")


def _assert_supported_workflow_limits(text: str) -> None:
    assert "one selected branch: main, research, writing, or data" in text
    assert "does not promise to detect every request that combines workflows" in text
    assert "Research can execute code but cannot save a writing draft" in text
    assert "writing can save drafts but cannot execute code" in text
    assert "blueprint workflows are separate from this chat graph" in text
    assert "Entity extraction returns extracted entities, not durable graph IDs" in text
    assert "Search the knowledge graph for canonical entity IDs" in text


@pytest.mark.unit
def test_supported_workflow_limits_match_live_prompts_and_operations_guide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The current guide and every live/fallback prompt state real branch limits."""
    from src.services.agent import _prompts
    from src.services.agent.subgraphs import (
        agents_md_loader,
        data_agent,
        research_agent,
        writing_agent,
    )
    from src.services.agent.tools import TOOL_REGISTRY

    normal_prompts = (
        _prompts._LLM_NODE_STATIC_PROMPT,
        research_agent._build_research_system_prompt(),
        writing_agent._build_writing_system_prompt(),
        data_agent._build_data_system_prompt(),
    )
    for prompt in normal_prompts:
        _assert_supported_workflow_limits(prompt)

    monkeypatch.setattr(agents_md_loader, "load_agents_md", lambda _branch: "")
    fallback_prompts = (
        research_agent._build_research_system_prompt(),
        writing_agent._build_writing_system_prompt(),
        data_agent._build_data_system_prompt(),
    )
    for prompt in fallback_prompts:
        _assert_supported_workflow_limits(prompt)

    research_tools = {
        descriptor.name
        for descriptor in TOOL_REGISTRY.descriptors_for_subgraph("research")
    }
    writing_tools = {
        descriptor.name
        for descriptor in TOOL_REGISTRY.descriptors_for_subgraph("writing")
    }
    data_tools = {
        descriptor.name for descriptor in TOOL_REGISTRY.descriptors_for_subgraph("data")
    }
    assert "execute_code" in research_tools and "create_draft" not in research_tools
    assert "create_draft" in writing_tools and "execute_code" not in writing_tools
    assert {"extract_entities", "search_knowledge_graph"} <= data_tools

    guide_path = BACKEND_ROOT.parent / "docs/operations/agent-supported-workflows.md"
    guide = guide_path.read_text(encoding="utf-8")
    normalized_guide = " ".join(guide.split())
    for phrase in (
        "one routed branch per user turn",
        "Research cannot save a writing draft in the same turn",
        "Writing cannot execute code",
        "not durable graph IDs",
        "separate from agent chat",
        "do not promise that arbitrary natural-language requests",
    ):
        assert phrase in normalized_guide

    relative_links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", guide)
    for target in relative_links:
        if target.startswith(("http://", "https://", "#")):
            continue
        assert (guide_path.parent / target.split("#", 1)[0]).resolve().is_file(), target


def _json_examples(asset: str, key: str) -> list[dict[str, Any]]:
    text = (ASSET_ROOT / f"AGENTS_{asset}.md").read_text(encoding="utf-8")
    examples: list[dict[str, Any]] = []
    in_json = False
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "```json" and not in_json:
            in_json = True
            current = []
        elif line.strip() == "```" and in_json:
            in_json = False
            try:
                value = json.loads("\n".join(current))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and key in value:
                examples.append(value)
        elif in_json:
            current.append(line)
    return examples


@pytest.mark.unit
@pytest.mark.parametrize(
    ("asset", "expected_tools"),
    [
        ("research", {"search_arxiv", "ingest_arxiv_papers"}),
        ("writing", {"search_documents", "do_kb_retrieve", "add_document_to_project"}),
        (
            "data",
            {
                "search_knowledge_graph",
                "explore_entity_neighborhood",
                "extract_entities",
            },
        ),
    ],
)
def test_role_tool_call_examples_validate_real_decorated_schemas(
    asset: str, expected_tools: set[str]
) -> None:
    examples = _json_examples(asset, "arguments")
    calls = {example["tool"]: example["arguments"] for example in examples}

    assert expected_tools <= calls.keys()
    for tool_name, arguments in calls.items():
        descriptor = TOOL_REGISTRY.descriptor(tool_name)
        assert descriptor is not None
        args_schema = descriptor.tool.args_schema
        assert args_schema is not None
        TypeAdapter(args_schema).validate_python(arguments)


async def _invoke_tool_coroutine(tool: Any, *args: Any, **kwargs: Any) -> Any:
    """Call the async wrapper exposed by the decorated LangChain tool."""
    coroutine = getattr(tool, "coroutine", None)
    assert callable(coroutine)
    return await coroutine(*args, **kwargs)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_role_result_examples_match_actual_result_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Role result JSON is checked against real wrappers and implementations."""
    from src.services.agent import tools as tools_module
    from src.services.agent import tools_impl
    from src.services.agent.tools import (
        add_document_to_project,
        do_kb_retrieve,
        explore_entity_neighborhood,
        extract_entities,
        ingest_arxiv_papers,
        search_documents,
        search_knowledge_graph,
    )

    document_id = UUID("11111111-1111-4111-8111-111111111111")
    project_id = UUID("22222222-2222-4222-8222-222222222222")
    user = SimpleNamespace(id=UUID(int=1), organization_id=UUID(int=10))
    config = {
        "configurable": {
            "user_id": str(user.id),
            "organization_id": str(user.organization_id),
        }
    }

    def install_context(db: Any) -> None:
        @asynccontextmanager
        async def fake_context(
            _config: Any,
        ) -> AsyncIterator[tuple[Any, Any, dict[str, Any]]]:
            yield db, user, {}

        monkeypatch.setattr(tools_module, "_tool_context", fake_context)

    research_results = {
        example["tool"]: example["result"]
        for example in _json_examples("research", "result")
    }
    assert "ingest_arxiv_papers" in research_results

    from src.services.arxiv import arxiv_service, persistence

    class FakeArxivIngestionService:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def get_papers_by_ids(self, paper_ids: list[str]) -> list[dict[str, Any]]:
            assert paper_ids == ["2401.12345"]
            return [{"id": "2401.12345", "title": "Example paper title"}]

        async def ingest_papers(self, **_kwargs: Any) -> list[Any]:
            return [SimpleNamespace(document_metadata={"arxiv_id": "2401.12345"})]

    persisted = SimpleNamespace(
        document_ids=[str(document_id)],
        reused_document_ids=set(),
        kb_sync_failed=False,
        failed_papers={},
    )
    monkeypatch.setattr(
        arxiv_service, "ArXivIngestionService", FakeArxivIngestionService
    )
    monkeypatch.setattr(
        persistence, "persist_arxiv_documents", AsyncMock(return_value=persisted)
    )
    ingest_db = MagicMock()
    install_context(ingest_db)
    ingest_actual = await _invoke_tool_coroutine(
        ingest_arxiv_papers, ["2401.12345"], config=config
    )
    assert ingest_actual == research_results["ingest_arxiv_papers"]

    writing_results = {
        example["tool"]: example["result"]
        for example in _json_examples("writing", "result")
    }
    assert {
        "search_documents",
        "do_kb_retrieve",
        "add_document_to_project",
    } <= writing_results.keys()

    search_doc = SimpleNamespace(
        id=document_id,
        title="Attention Is All You Need",
        filename="attention.pdf",
        document_type=SimpleNamespace(value="pdf"),
        processing_status=None,
        created_at=None,
    )
    search_result = MagicMock()
    search_result.scalars.return_value.all.return_value = [search_doc]
    search_db = MagicMock()
    search_db.execute = AsyncMock(return_value=search_result)
    install_context(search_db)
    search_actual = await _invoke_tool_coroutine(
        search_documents, "Attention Is All You Need", max_results=10, config=config
    )
    assert search_actual == writing_results["search_documents"]

    from src.core import config as core_config
    from src.services.do_kb import retrieval as retrieval_module
    from src.services.do_kb.models import Chunk, RetrieveResult
    from src.services.do_kb.retrieval import DOKBRetrieveOutcome, DOKBRetrieveStatus

    original_settings = core_config.settings
    kb_settings = SimpleNamespace(
        DO_KB_ENABLED=True,
        DO_KB_DEFAULT_TOP_K=8,
        BEDROCK_KB_ID="",
        AGENT_DOKB_COHERE_RERANK=False,
        AGENT_ITERATIVE_RETRIEVAL=False,
    )
    monkeypatch.setattr(core_config, "settings", kb_settings)

    class RowsResult:
        def __init__(self, rows: list[Any]) -> None:
            self.rows = rows

        def all(self) -> list[Any]:
            return self.rows

        def __iter__(self) -> Iterator[Any]:
            return iter(self.rows)

    authorized_rows = RowsResult([(document_id, None, "local")])
    canonical_rows = RowsResult([(document_id, None, "Attention Is All You Need")])
    retrieve_result = RetrieveResult(
        chunks=[
            Chunk(
                text="The attention mechanism ...",
                score=0.91,
                document_id=f"{document_id}.txt",
                metadata={"score_source": "upstream"},
            )
        ],
        total=1,
    )
    monkeypatch.setattr(
        retrieval_module,
        "resolve_org_kb_uuid",
        AsyncMock(return_value="kb-1"),
    )
    monkeypatch.setattr(
        retrieval_module,
        "retrieve_kb_chunks",
        AsyncMock(
            return_value=DOKBRetrieveOutcome(
                status=DOKBRetrieveStatus.SUCCESS,
                result=retrieve_result,
            )
        ),
    )
    kb_db = MagicMock()
    kb_db.execute = AsyncMock(side_effect=[authorized_rows, canonical_rows])
    install_context(kb_db)
    retrieval_actual = await _invoke_tool_coroutine(
        do_kb_retrieve,
        "attention mechanism",
        8,
        config,
        document_ids=[str(document_id)],
    )
    assert retrieval_actual == writing_results["do_kb_retrieve"]
    monkeypatch.setattr(core_config, "settings", original_settings)

    project = SimpleNamespace(id=project_id, name="Example project")
    document = SimpleNamespace(id=document_id, title="Attention Is All You Need")
    monkeypatch.setattr(
        tools_impl, "_resolve_document_id", AsyncMock(return_value=document)
    )
    monkeypatch.setattr(
        tools_impl, "_verify_project_ownership", AsyncMock(return_value=project)
    )
    from src.services.agent import tool_helpers

    monkeypatch.setattr(
        tool_helpers,
        "_link_documents_to_project",
        AsyncMock(return_value={"linked": 1, "already_linked": 0}),
    )
    attach_db = MagicMock()
    attach_db.commit = AsyncMock()
    install_context(attach_db)
    attach_actual = await _invoke_tool_coroutine(
        add_document_to_project, str(document_id), str(project_id), config
    )
    assert attach_actual == writing_results["add_document_to_project"]

    data_results = {
        example["tool"]: example["result"]
        for example in _json_examples("data", "result")
    }
    assert {
        "search_knowledge_graph",
        "explore_entity_neighborhood",
        "extract_entities",
    } <= data_results.keys()

    from src.services.knowledge_graph.knowledge_graph_service import (
        knowledge_graph_service,
    )

    monkeypatch.setattr(
        knowledge_graph_service, "search_entities", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        knowledge_graph_service,
        "get_neighborhood",
        lambda **_kwargs: {"entities": [], "relationships": []},
    )
    graph_db = MagicMock()
    install_context(graph_db)
    graph_search_actual = await _invoke_tool_coroutine(
        search_knowledge_graph, "attention", config=config
    )
    assert graph_search_actual == data_results["search_knowledge_graph"]

    entity_id = str(document_id)
    neighborhood_actual = await _invoke_tool_coroutine(
        explore_entity_neighborhood, entity_id, 2, 30, config
    )
    assert neighborhood_actual == data_results["explore_entity_neighborhood"]

    extraction_document = SimpleNamespace(content_text="an attention paper", title=None)
    monkeypatch.setattr(
        tools_impl,
        "_resolve_document_id",
        AsyncMock(return_value=extraction_document),
    )

    class FakeEntityExtractionService:
        async def extract_entities(self, _text: str, entity_types: Any = None) -> Any:
            assert entity_types is None
            return SimpleNamespace(entities=[], error=None)

    from src.services.processing import llm_entity_extraction

    monkeypatch.setattr(
        llm_entity_extraction,
        "LLMEntityExtractionService",
        FakeEntityExtractionService,
    )
    extraction_actual = await _invoke_tool_coroutine(
        extract_entities, entity_id, config=config
    )
    assert extraction_actual == data_results["extract_entities"]


@pytest.mark.unit
def test_role_assets_match_existing_tool_names_and_loop_budget() -> None:
    research = (ASSET_ROOT / "AGENTS_research.md").read_text(encoding="utf-8")
    writing = (ASSET_ROOT / "AGENTS_writing.md").read_text(encoding="utf-8")
    data = (ASSET_ROOT / "AGENTS_data.md").read_text(encoding="utf-8")

    assert "search_documents" in writing
    assert "do_kb_retrieve" in writing
    assert "returned `document_id`" not in writing
    assert "accessible document library" in writing
    assert (
        "Do not search arXiv or ingest while an accessible local match remains unresolved."
        in writing
    )
    title_resolution = writing[
        writing.index("3. **Resolve specifically requested papers") : writing.index(
            "4. **Generate the artifact"
        )
    ]
    assert title_resolution.index("search_documents") < title_resolution.index(
        "search_arxiv"
    )
    assert "document_ids" in research
    assert "ingested_count" in research
    assert "search_memory" not in research + writing + data
    assert "analyze_document" not in research + writing + data

    hard_rule = writing.split(
        "## Hard rule — resolve the requested source before writing", 1
    )[1].split("## Constraints", 1)[0]
    assert "accessible document library" in hard_rule
    assert "matching local source" in hard_rule
    assert (
        "run the `search_arxiv`/`ingest_arxiv_papers` resolution steps first anyway"
        not in hard_rule
    )

    from src.services.agent.subgraphs.data_agent import MAX_DATA_TOOL_LOOPS

    assert f"max {MAX_DATA_TOOL_LOOPS} tool loops" in data.lower()


def _checker_module() -> Any:
    script = BACKEND_ROOT / "scripts/check_agent_role_assets.py"
    spec = importlib.util.spec_from_file_location("check_agent_role_assets", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_role_asset_checker_accepts_complete_copy_and_rejects_each_missing_asset(
    tmp_path: Path,
) -> None:
    source = BACKEND_ROOT / "src/services/agent/subgraphs"
    copied_root = tmp_path / "backend"
    copied_assets = copied_root / "src/services/agent/subgraphs"
    copied_assets.mkdir(parents=True)
    for name in ASSET_NAMES:
        (copied_assets / f"AGENTS_{name}.md").write_bytes(
            (source / f"AGENTS_{name}.md").read_bytes()
        )

    checker = _checker_module()
    assert checker.check_assets(copied_root) == []
    for name in ASSET_NAMES:
        missing = copied_assets / f"AGENTS_{name}.md"
        contents = missing.read_bytes()
        missing.unlink()
        errors = checker.check_assets(copied_root)
        assert any(missing.name in error for error in errors)
        missing.write_bytes(contents)


@pytest.mark.unit
def test_role_asset_checker_rejects_empty_and_unreadable_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _checker_module()
    assets = tmp_path / "src/services/agent/subgraphs"
    assets.mkdir(parents=True)
    for name in ASSET_NAMES:
        (assets / f"AGENTS_{name}.md").write_text(
            f"# {name.title()} subgraph — driver protocol\n\n## Constraints\n",
            encoding="utf-8",
        )

    empty = assets / "AGENTS_data.md"
    empty.write_text("", encoding="utf-8")
    assert any("empty" in error.lower() for error in checker.check_assets(tmp_path))
    empty.write_text("# Data subgraph — driver protocol\n", encoding="utf-8")

    original_read_text = Path.read_text

    def unreadable(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.name == "AGENTS_writing.md":
            raise PermissionError("fixture unreadable")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert any(
        "unreadable" in error.lower() for error in checker.check_assets(tmp_path)
    )


@pytest.mark.unit
def test_role_asset_checker_is_runnable_as_a_source_check() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(BACKEND_ROOT / "scripts/check_agent_role_assets.py"),
            "--root",
            str(BACKEND_ROOT),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
