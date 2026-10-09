"""LangGraph tool declarations for the agent.

Each ``@tool`` below is a schema, not an implementation: its signature and
docstring are the LLM binding and the argument contract that
``tools_impl.execute_tool`` validates. Production dispatch is
``_nodes_tools._execute_single_tool`` -> ``tools_impl.execute_tool`` ->
``tools_impl._tool_*``, so these bodies never run. They used to carry caps,
allowlists and page-project resolution that protected nothing in production
(agent audit round 8); every guard belongs in the ``_tool_*`` impl.
Server-owned context (user, organization, thread, page project, runtime
snapshot) is added by ``_nodes_tools`` and ``execute_tool``;
``tests/unit/agent/test_tool_args_contract.py`` drives every registered tool
through that path.
"""

import functools
import inspect
from typing import Any, Dict, List, Literal, NoReturn, Optional

from langchain_core.runnables import RunnableConfig
from pydantic import Field
from typing_extensions import Annotated

from src.services.agent.tool_registry import (
    AgentIntent,
    AgentSubgraph,
    ToolDescriptor,
    ToolEffectMode,
    ToolPolicyTag,
    ToolRegistry,
)

try:
    from langchain_core.tools import tool
except Exception:  # pragma: no cover - exercised in tests via module stubs

    class _FallbackTool:
        """Small StructuredTool-like wrapper for test environments."""

        def __init__(self, fn):
            functools.update_wrapper(self, fn)
            self.func = fn
            self.coroutine = fn
            self.name = fn.__name__
            self.description = (fn.__doc__ or "").strip()
            self._is_coroutine = inspect.iscoroutinefunction(fn)

        def __call__(self, *args, **kwargs):
            # Async functions must be awaited — calling them synchronously
            # would otherwise return a coroutine object (unawaited).
            return self.func(*args, **kwargs)

        async def ainvoke(self, *args, **kwargs):
            if self._is_coroutine:
                return await self.func(*args, **kwargs)
            return self.func(*args, **kwargs)

    def tool(func=None, **_kwargs):
        def decorator(fn):
            return _FallbackTool(fn)

        if func is None:
            return decorator
        return decorator(func)


def _schema_only() -> NoReturn:
    """Fail loudly if anything treats a declaration as an executor."""
    raise NotImplementedError(
        "Agent tools run through tools_impl.execute_tool; this @tool is a schema."
    )


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


@tool
async def search_arxiv(
    query: str,
    max_results: int = 5,
    categories: Optional[List[str]] = None,
    recency_days: int = 365,
    chronological: bool = False,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Search arXiv for academic papers.

    Use when the user asks to find, search, or look up research papers.
    Pass clean topic KEYWORDS in query — not filler like 'recent' or 'latest';
    recency is controlled by recency_days. arXiv has NO venue field, so never
    put conference names (NeurIPS, ICML, ICLR, ACL, etc.) in query — they
    match almost nothing and empty the whole result. Do not repeat a term
    (quoted or not) more than once. By default only papers from the
    last 365 days are returned. Pass recency_days=0 to disable the date
    filter for historical or all-time searches (e.g. papers from 2022-2024),
    or a larger N to widen the window. Set chronological=true to sort
    newest-first instead of by relevance.
    """
    _schema_only()


@tool
async def ingest_arxiv_papers(
    paper_ids: List[str],
    project_id: Annotated[
        Optional[str],
        Field(
            description=(
                "UUID of an existing project, as returned by list_projects. "
                "NOT the project name — a name is rejected."
            )
        ),
    ] = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Ingest arXiv papers into the RAG system for indexing and search.

    Use when the user wants to add, import, download, or ingest arXiv papers
    (IDs like '2401.12345'). Omit ``project_id`` when the user is viewing a
    project page — the tool auto-attaches. Pass an explicit UUID only to
    target a different project.
    """
    _schema_only()


@tool
async def search_documents(
    query: str,
    max_results: int = 10,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Look up documents by title/filename substring match.

    Does NOT search document content — for content-level questions
    ("what do my documents say about X", comparisons, quotes) use
    do_kb_retrieve instead. A multi-word query here must appear verbatim
    in a single title/filename to match.
    """
    _schema_only()


@tool
async def do_kb_retrieve(
    query: str,
    top_k: int = 8,
    config: RunnableConfig = None,  # type: ignore[assignment]
    *,
    document_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Semantic retrieval over the organization's DigitalOcean Knowledge Base.

    When evidence mode is on, each chunk includes 'relevance' (0-10), a
    'summary' of how it bears on the query, and a verbatim 'quote'. Cite
    using the quote. If top relevance is below 5, call this tool again
    with a narrower or broader reformulation instead of settling for weak
    evidence."""
    _schema_only()


@tool
async def add_document_to_project(
    document_id: str,
    project_id: Optional[str] = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Add an existing document to a research project.

    If *project_id* is omitted and the user is on a project page, the
    project is inferred from the page context.
    """
    _schema_only()


@tool
async def create_project(
    name: str,
    description: Optional[str] = None,
    research_goals: Optional[str] = None,
    tags: Optional[List[str]] = None,
    workspace_id: Optional[str] = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Create a new research project (folder) for organizing papers, documents, and notes.

    If *workspace_id* is omitted, the user's first workspace is used.
    """
    _schema_only()


@tool
async def create_project_note(
    title: str,
    content: str,
    project_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Create a markdown note in a research project.

    When a project page is active, OMIT *project_id* — the note is written to
    the project the user is viewing. Pass *project_id* (a real UUID from
    list_projects) ONLY when the user explicitly asks to write into a
    different project. Never infer the project from the note's topic, title,
    or contents: topic-matching a project silently files the note in the
    wrong place.
    """
    _schema_only()


@tool
async def list_projects(
    status: Optional[str] = None,
    tag: Optional[str] = None,
    search: Annotated[
        Optional[str],
        Field(
            description=(
                "Substring match against project NAMES only. Do not pass the "
                "user's phrasing — 'my library', 'my notes' and similar are "
                "not project names. Omit this to list everything."
            )
        ),
    ] = None,
    limit: int = 20,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """List the user's research projects.

    Use when the user asks "what projects do I have", "list my projects",
    or wants to discover existing projects before choosing one. Prefer this
    over asking the user to provide a project_id.
    """
    _schema_only()


@tool
async def list_project_documents(
    project_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """List documents in a research project, ordered newest first.

    Args:
        project_id: Project UUID. If omitted and the user is on a project
            page, the project is inferred from the page context.
        limit: Maximum number of documents to return (default 100, max 500).
        offset: Number of documents to skip for pagination (default 0).

    Returns pagination fields (``total``/``returned``/``has_more``).
    """
    _schema_only()


@tool
async def get_current_draft(
    project_id: Optional[str] = None,
    include_content: bool = False,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Get the latest completed draft for a research project.

    Use this before answering follow-ups about a draft being ready, missing,
    or available to show. Set ``include_content`` only when the user asks to
    read or continue the draft in chat.
    """
    _schema_only()


@tool
async def summarize_document(
    document_id: str,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Summarize a document's content.

    Use when the user asks for a summary or overview of a specific document.
    """
    _schema_only()


@tool
async def compare_documents(
    document_ids: List[str],
    type: str = "general",
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Compare multiple documents to find similarities, differences, and shared themes.

    Use when the user wants to compare, contrast, or analyze differences
    between two or more documents.
    """
    _schema_only()


@tool
async def extract_entities(
    document_id: str,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Extract named entities (people, organizations, concepts, etc.) from a document.

    Use when the user wants to identify key entities, people, organizations,
    or concepts mentioned in a document.
    """
    _schema_only()


@tool
async def search_knowledge_graph(
    query: str,
    entity_types: Optional[List[str]] = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Search the knowledge graph for entities and their relationships.

    Use when the user asks about concepts, people, or organizations
    in the research corpus, or wants to explore entity relationships.
    """
    _schema_only()


@tool
async def explore_entity_neighborhood(
    entity_id: str,
    max_depth: int = 2,
    limit: int = 30,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Explore an entity's neighborhood in the knowledge graph.

    Find all connected entities and the relationships between them.
    Use when the user asks "what is connected to X", "show me everything
    related to X", or wants to understand how an entity fits in the graph.
    First use search_knowledge_graph to find the entity_id.
    """
    _schema_only()


@tool
async def find_entity_paths(
    source_entity_id: str,
    target_entity_id: str,
    max_depth: int = 3,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Find relationship paths between two entities in the knowledge graph.

    Use when the user asks "how is X related to Y", "what connects X and Y",
    or wants to understand the chain of relationships between two concepts.
    First use search_knowledge_graph to find both entity IDs.
    """
    _schema_only()


@tool
async def get_graph_stats(
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Get statistics about the knowledge graph.

    Returns total entities, relationships, type distributions, and
    connectivity metrics. Use when the user asks about the size or shape
    of the knowledge base, or wants an overview of what's in the graph.
    """
    _schema_only()


@tool
async def create_draft(
    themes: List[str],
    project_id: Optional[str] = None,
    style: str = "academic",
    document_ids: Optional[List[str]] = None,
    instructions: Optional[str] = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Generate a literature review draft for a project based on themes.

    Use when the user wants to create a draft, write a review, or synthesize
    research around specific themes.
    """
    _schema_only()


@tool
async def revise_draft(
    instructions: str,
    project_id: Optional[str] = None,
    base_version: Optional[int] = None,
    mode: Literal["revise", "citations_only"] = "revise",
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Revise a saved draft version using its server-loaded durable content."""
    _schema_only()


@tool
async def export_bibliography(
    document_ids: List[str],
    format: str = "bibtex",
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Export bibliography/references for documents in a specific citation format.

    Use when the user wants to export citations, references, or a bibliography
    for one or more documents. Supports bibtex, apa, ieee, and mla formats.
    """
    _schema_only()


# ---------------------------------------------------------------------------
# Code Execution
# ---------------------------------------------------------------------------


@tool
async def execute_code(
    code: str,
    description: str,
    language: str = "python",
    packages: Optional[List[str]] = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Execute Python code in a sandboxed E2B environment.

    Use this tool when the user asks you to run code, perform data analysis,
    create visualizations, train models, or do any computation that requires
    executing Python. The sandbox has numpy, pandas, matplotlib, scipy,
    scikit-learn, and seaborn pre-installed. You can install additional
    packages via the ``packages`` parameter.

    The sandbox is stateful within a conversation — variables and files
    persist between executions, so you can build on previous results.
    """
    _schema_only()


# ---------------------------------------------------------------------------
# External database connectors
# ---------------------------------------------------------------------------


@tool
async def search_external_database(
    query: str,
    connector: Optional[str] = None,
    domain: Optional[str] = None,
    max_results: int = 10,
    filters: Optional[Dict[str, Any]] = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Search external databases (PubMed, UniProt, ChEMBL, PubChem, FRED, SEC EDGAR, etc.).

    Provides a single entry point to the registered scientific and financial
    connector adapters. Supply ``connector`` to target one (e.g. ``"pubmed"``),
    ``domain`` to fan out across a category (``biomedical``, ``chemistry``,
    ``finance``, ``clinical``, ``genomics``, ``economic``, ``literature``),
    or omit both to search every available connector concurrently.

    Use when the user asks for proteins, compounds, mutations, clinical trials,
    economic time series, SEC filings, or any other domain-specific data not
    available in the local document store.
    """
    _schema_only()


@tool
async def list_external_databases(
    domain: Optional[str] = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """List the external database connectors available to the agent.

    Use when the user asks "what databases can you search", or before invoking
    ``search_external_database`` to discover the right connector name. Pass
    ``domain`` to filter by category.
    """
    _schema_only()


# ---------------------------------------------------------------------------
# Memory management
# ---------------------------------------------------------------------------


@tool
async def forget_memory(
    query: str,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Forget previously-saved memories that match *query*.

    Use when the user explicitly asks you to forget, delete, or wipe a
    memory ("forget what I said about X", "stop remembering Y"). Returns
    a summary of which memories were deleted.
    """
    _schema_only()


@tool
async def load_project_skill(
    skill_name: str,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Load the frozen instructions for one relevant project skill.

    Use only for a skill listed in this run's project-skill catalog.  The
    server supplies the snapshot, user, and project context; never ask for or
    invent identifiers.
    """
    _schema_only()


# ---------------------------------------------------------------------------
# Code-owned registry and compatibility view
# ---------------------------------------------------------------------------

TOOL_REGISTRY = ToolRegistry(
    [
        ToolDescriptor(
            name="search_arxiv",
            tool=search_arxiv,
            intents=frozenset({AgentIntent.RESEARCH, AgentIntent.GENERAL}),
            subgraphs=frozenset({AgentSubgraph.RESEARCH, AgentSubgraph.WRITING}),
            subgraph_positions=(
                (AgentSubgraph.RESEARCH, 0),
                (AgentSubgraph.WRITING, 5),
            ),
            policy_tags=frozenset(
                {
                    ToolPolicyTag.SLOW,
                    ToolPolicyTag.NO_OUTER_RETRY,
                    ToolPolicyTag.CONTEXT_FREE,
                }
            ),
        ),
        ToolDescriptor(
            name="ingest_arxiv_papers",
            tool=ingest_arxiv_papers,
            intents=frozenset({AgentIntent.RESEARCH, AgentIntent.GENERAL}),
            subgraphs=frozenset({AgentSubgraph.RESEARCH, AgentSubgraph.WRITING}),
            subgraph_positions=(
                (AgentSubgraph.RESEARCH, 1),
                (AgentSubgraph.WRITING, 6),
            ),
            policy_tags=frozenset(
                {
                    ToolPolicyTag.DESTRUCTIVE,
                    ToolPolicyTag.SLOW,
                    ToolPolicyTag.NO_OUTER_RETRY,
                }
            ),
            effect_mode=ToolEffectMode.EXTERNAL,
        ),
        ToolDescriptor(
            name="search_documents",
            tool=search_documents,
            intents=frozenset(
                {
                    AgentIntent.RESEARCH,
                    AgentIntent.KNOWLEDGE_GRAPH,
                    AgentIntent.GENERAL,
                }
            ),
            subgraphs=frozenset(
                {AgentSubgraph.RESEARCH, AgentSubgraph.WRITING, AgentSubgraph.DATA}
            ),
            subgraph_positions=(
                (AgentSubgraph.RESEARCH, 2),
                (AgentSubgraph.WRITING, 15),
                (AgentSubgraph.DATA, 5),
            ),
            policy_tags=frozenset(),
        ),
        ToolDescriptor(
            name="do_kb_retrieve",
            tool=do_kb_retrieve,
            # GENERAL: the classifier demotes weak-evidence turns to general on
            # the premise that general is a superset of the specialist lanes
            # (classifier.py). Without this binding, a content-level question
            # landing in general had only title search — live miss 2026-08-12:
            # "Compare the METR and MIT studies" → search_documents → 0 hits →
            # "no documents found" with both docs indexed.
            intents=frozenset({AgentIntent.GENERAL}),
            # DATA: search_documents is bound to research + data, and its
            # zero-hit path tells the model to escalate here. Bound to research
            # only, that advice was unfollowable from the data subgraph —
            # make_filtered_tool_node answers "not available in this context"
            # (guarded by test_recovery_suggestions_are_callable).
            subgraphs=frozenset(
                {AgentSubgraph.RESEARCH, AgentSubgraph.WRITING, AgentSubgraph.DATA}
            ),
            subgraph_positions=(
                (AgentSubgraph.RESEARCH, 3),
                (AgentSubgraph.WRITING, 16),
                (AgentSubgraph.DATA, 8),
            ),
            policy_tags=frozenset(),
            exposed_in_all_tools=False,
        ),
        ToolDescriptor(
            name="create_project",
            tool=create_project,
            intents=frozenset({AgentIntent.RESEARCH, AgentIntent.GENERAL}),
            # Writing too: a note/draft needs a project that may not exist
            # yet. HITL unchanged — the DESTRUCTIVE tag still fires the
            # confirm gate regardless of which subgraph binds the tool.
            subgraphs=frozenset({AgentSubgraph.RESEARCH, AgentSubgraph.WRITING}),
            subgraph_positions=(
                (AgentSubgraph.RESEARCH, 4),
                (AgentSubgraph.WRITING, 9),
            ),
            policy_tags=frozenset(
                {ToolPolicyTag.DESTRUCTIVE, ToolPolicyTag.NO_OUTER_RETRY}
            ),
            effect_mode=ToolEffectMode.LOCAL_TRANSACTION,
        ),
        ToolDescriptor(
            name="list_projects",
            tool=list_projects,
            intents=frozenset({AgentIntent.RESEARCH, AgentIntent.GENERAL}),
            # Writing needs it too: create_project_note / create_draft require a
            # project_id, and without a way to look one up the writing executor
            # can only interrogate the user ("which project should I put this
            # in?") — measured on dev at 5 runs out of 5. Read-only and
            # untagged, so binding it adds no destructive surface.
            # DATA too: list_project_documents is bound there and its
            # _missing_project_error tells the model to "call list_projects".
            subgraphs=frozenset(
                {AgentSubgraph.RESEARCH, AgentSubgraph.WRITING, AgentSubgraph.DATA}
            ),
            subgraph_positions=(
                (AgentSubgraph.RESEARCH, 5),
                (AgentSubgraph.WRITING, 7),
                (AgentSubgraph.DATA, 7),
            ),
            policy_tags=frozenset(),
        ),
        ToolDescriptor(
            name="add_document_to_project",
            tool=add_document_to_project,
            intents=frozenset({AgentIntent.RESEARCH, AgentIntent.GENERAL}),
            # Writing too: same "create project -> add document -> note"
            # flow needs this step reachable without a subgraph hop.
            subgraphs=frozenset({AgentSubgraph.RESEARCH, AgentSubgraph.WRITING}),
            subgraph_positions=(
                (AgentSubgraph.RESEARCH, 6),
                (AgentSubgraph.WRITING, 10),
            ),
            policy_tags=frozenset(
                {ToolPolicyTag.DESTRUCTIVE, ToolPolicyTag.NO_OUTER_RETRY}
            ),
            effect_mode=ToolEffectMode.LOCAL_TRANSACTION,
        ),
        ToolDescriptor(
            name="create_project_note",
            tool=create_project_note,
            intents=frozenset({AgentIntent.WRITING, AgentIntent.GENERAL}),
            # Research too: ACTION_INTENT_OVERRIDES routes "create a project"
            # to research at confidence 1.0, so the note step of the same
            # flow must be reachable there without a subgraph hop.
            subgraphs=frozenset({AgentSubgraph.WRITING, AgentSubgraph.RESEARCH}),
            subgraph_positions=(
                (AgentSubgraph.WRITING, 1),
                (AgentSubgraph.RESEARCH, 8),
            ),
            policy_tags=frozenset(
                {ToolPolicyTag.DESTRUCTIVE, ToolPolicyTag.NO_OUTER_RETRY}
            ),
            effect_mode=ToolEffectMode.LOCAL_TRANSACTION,
        ),
        ToolDescriptor(
            name="list_project_documents",
            tool=list_project_documents,
            intents=frozenset({AgentIntent.RESEARCH, AgentIntent.GENERAL}),
            # Writing needs it too: summarize_document is bound *only* to
            # writing, and when it is handed a project id (trace 019f4386) its
            # error *message* tells the model to call list_project_documents —
            # a tool writing did not have, so make_filtered_tool_node answered
            # that call with "not available in this context".
            # (Its "suggestion" field reaches the model as of the change that
            # made classify_error_from_payload honour tool declarations; the
            # binding is what makes the advice actionable.)
            # Read-only and untagged.
            subgraphs=frozenset(
                {AgentSubgraph.RESEARCH, AgentSubgraph.DATA, AgentSubgraph.WRITING}
            ),
            subgraph_positions=(
                (AgentSubgraph.RESEARCH, 7),
                (AgentSubgraph.DATA, 6),
                (AgentSubgraph.WRITING, 8),
            ),
            policy_tags=frozenset(),
        ),
        ToolDescriptor(
            name="get_current_draft",
            tool=get_current_draft,
            intents=frozenset({AgentIntent.WRITING}),
            subgraphs=frozenset({AgentSubgraph.WRITING}),
            subgraph_positions=((AgentSubgraph.WRITING, 14),),
            policy_tags=frozenset(),
            exposed_in_all_tools=False,
        ),
        ToolDescriptor(
            name="summarize_document",
            tool=summarize_document,
            intents=frozenset({AgentIntent.WRITING, AgentIntent.GENERAL}),
            subgraphs=frozenset({AgentSubgraph.WRITING}),
            subgraph_positions=((AgentSubgraph.WRITING, 3),),
            policy_tags=frozenset(),
        ),
        ToolDescriptor(
            name="compare_documents",
            tool=compare_documents,
            # GENERAL: comparison phrasings rarely carry classifier signal
            # (only two literal override phrases match), so they routinely land
            # in general — where this tool must be callable (see do_kb_retrieve
            # note above).
            intents=frozenset({AgentIntent.WRITING, AgentIntent.GENERAL}),
            subgraphs=frozenset({AgentSubgraph.WRITING}),
            subgraph_positions=((AgentSubgraph.WRITING, 4),),
            policy_tags=frozenset({ToolPolicyTag.SLOW}),
        ),
        ToolDescriptor(
            name="extract_entities",
            tool=extract_entities,
            intents=frozenset({AgentIntent.KNOWLEDGE_GRAPH}),
            subgraphs=frozenset({AgentSubgraph.DATA}),
            subgraph_positions=((AgentSubgraph.DATA, 0),),
            policy_tags=frozenset(),
        ),
        ToolDescriptor(
            name="search_knowledge_graph",
            tool=search_knowledge_graph,
            intents=frozenset({AgentIntent.KNOWLEDGE_GRAPH, AgentIntent.GENERAL}),
            subgraphs=frozenset({AgentSubgraph.DATA}),
            subgraph_positions=((AgentSubgraph.DATA, 1),),
            policy_tags=frozenset(),
        ),
        ToolDescriptor(
            name="explore_entity_neighborhood",
            tool=explore_entity_neighborhood,
            intents=frozenset({AgentIntent.KNOWLEDGE_GRAPH}),
            subgraphs=frozenset({AgentSubgraph.DATA}),
            subgraph_positions=((AgentSubgraph.DATA, 2),),
            policy_tags=frozenset(),
        ),
        ToolDescriptor(
            name="find_entity_paths",
            tool=find_entity_paths,
            intents=frozenset({AgentIntent.KNOWLEDGE_GRAPH}),
            subgraphs=frozenset({AgentSubgraph.DATA}),
            subgraph_positions=((AgentSubgraph.DATA, 3),),
            policy_tags=frozenset(),
        ),
        ToolDescriptor(
            name="get_graph_stats",
            tool=get_graph_stats,
            intents=frozenset({AgentIntent.KNOWLEDGE_GRAPH}),
            subgraphs=frozenset({AgentSubgraph.DATA}),
            subgraph_positions=((AgentSubgraph.DATA, 4),),
            policy_tags=frozenset(),
        ),
        ToolDescriptor(
            name="create_draft",
            tool=create_draft,
            intents=frozenset({AgentIntent.WRITING}),
            subgraphs=frozenset({AgentSubgraph.WRITING}),
            subgraph_positions=((AgentSubgraph.WRITING, 0),),
            policy_tags=frozenset(
                {
                    ToolPolicyTag.DESTRUCTIVE,
                    ToolPolicyTag.SLOW,
                    ToolPolicyTag.NO_OUTER_RETRY,
                }
            ),
            effect_mode=ToolEffectMode.EXTERNAL,
        ),
        ToolDescriptor(
            name="revise_draft",
            tool=revise_draft,
            intents=frozenset({AgentIntent.WRITING}),
            subgraphs=frozenset({AgentSubgraph.WRITING}),
            subgraph_positions=((AgentSubgraph.WRITING, 13),),
            policy_tags=frozenset(
                {
                    ToolPolicyTag.DESTRUCTIVE,
                    ToolPolicyTag.SLOW,
                    ToolPolicyTag.NO_OUTER_RETRY,
                }
            ),
            effect_mode=ToolEffectMode.EXTERNAL,
        ),
        ToolDescriptor(
            name="export_bibliography",
            tool=export_bibliography,
            intents=frozenset({AgentIntent.WRITING}),
            subgraphs=frozenset({AgentSubgraph.WRITING}),
            subgraph_positions=((AgentSubgraph.WRITING, 2),),
            policy_tags=frozenset(),
        ),
        ToolDescriptor(
            name="execute_code",
            # RESEARCH subgraph only, not DATA. intents={RESEARCH, KNOWLEDGE_GRAPH}
            # were dead metadata: research/data subgraphs bind tools by
            # descriptors_for_subgraph (subgraph membership), never by intent —
            # only the general path's llm_node consults intents, and it's only
            # reached for GENERAL (route_by_intent sends research/writing/
            # knowledge_graph straight to their subgraphs). With subgraphs=∅ this
            # tool was in no subgraph and thus uncallable in production. DATA is
            # not an option: it has has_interrupt=False (no destructive tools by
            # design — see data_agent.py docstring), so a DESTRUCTIVE tool bound
            # there would execute code with no HITL confirmation. RESEARCH has
            # has_interrupt=True, and should_continue's interrupt check
            # (has_policy_in_subgraph(..., DESTRUCTIVE, "research")) fires for any
            # tool call in that subgraph's set — so binding here keeps execute_code
            # behind the confirmation gate.
            tool=execute_code,
            intents=frozenset({AgentIntent.RESEARCH}),
            subgraphs=frozenset({AgentSubgraph.RESEARCH}),
            subgraph_positions=((AgentSubgraph.RESEARCH, 9),),
            # SLOW (IN-2): the sandbox interrupts a cell after
            # AGENT_CELL_TIMEOUT_SECONDS and keeps the box; the 30 s default
            # tier cancelled the call first, and cancellation kills the box.
            policy_tags=frozenset(
                {
                    ToolPolicyTag.DESTRUCTIVE,
                    ToolPolicyTag.NO_OUTER_RETRY,
                    ToolPolicyTag.SLOW,
                }
            ),
            effect_mode=ToolEffectMode.EXTERNAL,
        ),
        ToolDescriptor(
            name="search_external_database",
            # GENERAL so the tool is reachable — same dead-metadata pattern as
            # forget_memory (51fd5adc): intents=∅ + subgraphs=∅ meant this was
            # bound only to the unknown-intent ALL_TOOLS path the live graph
            # never takes. A connector lookup carries no research/writing/KG
            # signal, so GENERAL is its natural home. CONTEXT_FREE is unchanged.
            # Writing too: the literature-review project skill instructs a
            # multi-database search, and the classifier routes "conduct a
            # literature review" to writing — without the binding the
            # instruction is dead (make_filtered_tool_node answers the call
            # with "not available in this context"). Read-only, untagged.
            tool=search_external_database,
            intents=frozenset({AgentIntent.GENERAL}),
            subgraphs=frozenset({AgentSubgraph.WRITING}),
            subgraph_positions=((AgentSubgraph.WRITING, 11),),
            policy_tags=frozenset({ToolPolicyTag.CONTEXT_FREE}),
        ),
        ToolDescriptor(
            name="list_external_databases",
            # Same fix, same reasoning as search_external_database above:
            # GENERAL makes it reachable; read-only, so no destructive tag needed.
            # Writing too, for the same literature-review flow: the model has
            # to be able to discover which connectors exist before searching.
            tool=list_external_databases,
            intents=frozenset({AgentIntent.GENERAL}),
            subgraphs=frozenset({AgentSubgraph.WRITING}),
            subgraph_positions=((AgentSubgraph.WRITING, 12),),
            policy_tags=frozenset({ToolPolicyTag.CONTEXT_FREE}),
        ),
        ToolDescriptor(
            name="load_project_skill",
            tool=load_project_skill,
            intents=frozenset(),
            subgraphs=frozenset(),
            policy_tags=frozenset({ToolPolicyTag.CONTEXT_REQUIRED}),
            exposed_in_all_tools=False,
            availability_condition="project_skill_catalog",
        ),
        ToolDescriptor(
            name="forget_memory",
            # GENERAL so the tool is actually reachable. With intents=∅ it was
            # bound only to the unknown-intent ALL_TOOLS path, which the live
            # graph never takes: classify_intent_with_fallback is Literal-typed
            # to the four real intents and _get_tools_for_intent returns
            # ALL_TOOLS only for an intent OUTSIDE AgentIntent — so forget_memory
            # was uncallable in production. GENERAL is its natural home ("forget
            # what I told you about X" carries no research/writing/KG signal);
            # DESTRUCTIVE keeps it behind the HITL interrupt.
            tool=forget_memory,
            intents=frozenset({AgentIntent.GENERAL}),
            subgraphs=frozenset(),
            # Audit R7-L9: CONTEXT_FREE dropped — that fast path in
            # execute_tool skips resolve_tool_user entirely, so a destructive
            # delete ran for a user nobody had checked was active, undeleted
            # and in the asserted org. No tool may be CONTEXT_FREE+DESTRUCTIVE.
            policy_tags=frozenset(
                {
                    ToolPolicyTag.DESTRUCTIVE,
                    ToolPolicyTag.NO_OUTER_RETRY,
                }
            ),
            effect_mode=ToolEffectMode.EXTERNAL,
        ),
    ]
)

# Compatibility import for existing callers.  This view intentionally omits
# the research-only do_kb_retrieve wrapper, preserving the prior ALL_TOOLS API.
ALL_TOOLS = list(TOOL_REGISTRY.all_tools())
