"""Regression tests for deterministic arXiv search routing."""

import pytest
from langchain_core.messages import HumanMessage, ToolMessage


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_arxiv_search_emits_tool_call_without_llm(monkeypatch):
    """A direct arXiv search request should run search_arxiv, not prose-only."""
    import src.services.agent.llm_factory as factory
    from src.services.agent.subgraphs.research_agent import research_llm_node

    def _boom(*_args, **_kwargs):
        raise AssertionError("LLM must not be built before direct arXiv search")

    monkeypatch.setattr(factory, "build_lightweight_llm", _boom)
    monkeypatch.setattr(factory, "build_synthesis_llm", _boom)

    result = await research_llm_node(
        {
            "messages": [
                HumanMessage(
                    content="Search arXiv for retrieval-augmented generation papers"
                )
            ],
            "plan": [],
            "tool_loop_count": 0,
        },
        {},
    )

    message = result["messages"][0]

    assert message.content == ""
    assert len(message.tool_calls) == 1
    tool_call = message.tool_calls[0]
    assert tool_call["name"] == "search_arxiv"
    assert tool_call["args"] == {
        "query": "retrieval-augmented generation papers",
        "max_results": 5,
        "recency_days": 365,
    }
    assert tool_call["id"].startswith("direct_search_arxiv_")
    assert tool_call["type"] == "tool_call"


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [
        "Search arXiv for papers from the last five years",
        "search arxiv for transformer papers since 2015",
        "find arxiv papers on RAG over the past decade",
        "search arxiv for the earliest work on neural nets",
        "look up arxiv papers 2015-2020",
        "search arxiv for papers in 2020",
        "search arxiv for papers during 2020",
    ],
)
def test_explicit_time_window_skips_fast_path(content: str) -> None:
    """Turns that state their own window must fall through to the LLM.

    The fast path hard-codes search_arxiv's 365-day default, so a declared
    multi-year window would otherwise silently return only the last year.
    """
    from src.services.agent.subgraphs.research_agent import _direct_arxiv_search_query

    assert _direct_arxiv_search_query(content) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [
        "find papers on arxiv about RAG",
        "please search arxiv for retrieval augmented generation.",
        "search arxiv for graph neural networks",
        "search arxiv for 15-layer transformer architectures",
        "search arxiv for eleven-dimensional transformer embeddings",
    ],
)
def test_narrow_standalone_forms_take_fast_path(content: str) -> None:
    """Simple affirmative forms use the bounded default search arguments."""
    from src.services.agent.subgraphs.research_agent import _direct_arxiv_search_query

    assert _direct_arxiv_search_query(content) is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [
        "do not search arxiv for transformers",
        "search arxiv for transformers and save a draft",
        "search arxiv for transformers then ingest them",
        'search arxiv for "transformers"',
        "search arxiv for transformers; then create a note",
        "search arxiv for transformers—attention",
        "search arxiv for transformers from 2020",
        "search arxiv for transformers in the last five years",
        "search arxiv for transformers last-week",
        "search arxiv for five-year studies",
        "search arxiv for the top 10 transformer papers",
        "search arxiv for up to 5 transformer papers",
        "search arxiv for transformers at least 5 papers",
        "search arxiv for 15 transformer papers",
        "search arxiv for eleven transformer papers",
        "search arxiv for 15 transformer based language model optimization papers",
        "search arxiv for eleven transformer based language model optimization papers",
        "search arxiv for 15 a b c d e f g h i j k l m n o p q r papers",
        "search arxiv for transformer papers published yesterday",
        "search arxiv for those papers",
        "search arxiv for the same topic",
        "search arxiv for transformers?",
        "search arxiv for transformers\nthen save a draft",
        "\nsearch arxiv for transformers",
        "search arxiv for transformers\n",
        "search arxiv for one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty one",
    ],
)
def test_ambiguous_or_compound_requests_decline_direct_shortcut(content: str) -> None:
    from src.services.agent.subgraphs.research_agent import _direct_arxiv_search_query

    assert _direct_arxiv_search_query(content) is None


def test_direct_shortcut_requires_fresh_ordinary_projected_search() -> None:
    from src.services.agent.subgraphs.research_agent import _direct_arxiv_search_message
    from src.services.agent.tools import TOOL_REGISTRY

    messages = [HumanMessage(content="search arxiv for graph neural networks")]
    ordinary = {
        "messages": messages,
        "plan": [],
        "capability_limitation": {},
        "tool_loop_count": 0,
        "tool_executions": [],
        "loaded_skill_versions": [],
        "project_skill_catalog": [],
    }
    assert _direct_arxiv_search_message(messages, ordinary) is not None

    cases = [
        {**ordinary, "plan": [{"step": 1, "tool": "search_arxiv"}]},
        {**ordinary, "capability_limitation": {"branch": "research"}},
        {**ordinary, "tool_loop_count": 1},
        {**ordinary, "tool_executions": [{"tool_name": "search_arxiv"}]},
        {**ordinary, "loaded_skill_versions": [{"name": "paper-workflow"}]},
        {
            **ordinary,
            "project_skill_catalog": [{"name": "paper-workflow"}],
            "messages": [
                HumanMessage(content="search arxiv for paper-workflow transformers")
            ],
        },
        {
            **ordinary,
            "runtime_snapshot_id": "snapshot-1",
            "runtime_tool_names": [
                name
                for name in TOOL_REGISTRY.available_descriptor_names()
                if name != "search_arxiv"
            ],
            "tool_registry_hash": TOOL_REGISTRY.metadata_snapshot()["hash"],
            "tool_registry_version": TOOL_REGISTRY.metadata_snapshot()["version"],
        },
        {**ordinary, "runtime_snapshot_id": "snapshot-1"},
    ]
    for state in cases:
        assert _direct_arxiv_search_message(state["messages"], state) is None


def test_prior_tool_result_after_current_human_disables_shortcut() -> None:
    from src.services.agent.subgraphs.research_agent import _direct_arxiv_search_message

    messages = [
        HumanMessage(content="search arxiv for graph neural networks"),
        ToolMessage(content="{}", tool_call_id="prior-result"),
    ]
    assert _direct_arxiv_search_message(messages, {"tool_loop_count": 0}) is None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "search arxiv for transformers and save a draft",
        "search arxiv for 15 transformer papers",
        "search arxiv for eleven transformer papers",
        "search arxiv for 15 transformer based language model optimization papers",
        "search arxiv for eleven transformer based language model optimization papers",
        "search arxiv for transformer papers published yesterday",
    ],
)
async def test_declined_shortcut_uses_ordinary_research_model(monkeypatch, content):
    from langchain_core.messages import AIMessage

    import src.services.agent.graph as graph
    import src.services.agent.llm_factory as factory
    from src.services.agent.subgraphs.research_agent import research_llm_node

    observed: dict[str, object] = {}

    class FakeLLM:
        def bind_tools(self, tools, **_kwargs):
            observed["tools"] = {tool.name for tool in tools}
            return self

        async def ainvoke(self, messages, **_kwargs):
            observed["ainvoke_calls"] = int(observed.get("ainvoke_calls", 0)) + 1
            observed["messages"] = messages
            return AIMessage(content="I will search for that topic.")

    monkeypatch.setattr(graph, "_build_llm", lambda **_kwargs: FakeLLM())
    monkeypatch.setattr(factory, "resolve_chat_deployment", lambda *_args: "test-main")

    result = await research_llm_node(
        {
            "messages": [HumanMessage(content=content)],
            "plan": [],
            "capability_limitation": {},
            "tool_loop_count": 0,
            "tool_executions": [],
            "runtime_tool_names": [],
        },
        {},
    )

    assert not result["messages"][0].tool_calls
    assert observed["ainvoke_calls"] == 1
    assert "search_arxiv" in observed["tools"]
    model_messages = observed["messages"]
    assert isinstance(model_messages, list)
    assert any(
        isinstance(message, HumanMessage) and message.content == content
        for message in model_messages
    )
