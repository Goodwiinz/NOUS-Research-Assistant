"""LangGraph tool wrappers for agent tools.

Each tool delegates to the existing implementation in
``src.services.agent.tools_impl``. The LangGraph ``RunnableConfig.configurable``
dict carries **scalar identifiers only** (``user_id`` / ``organization_id`` /
``thread_id`` / ``page_context``) — never a live ``AsyncSession`` or ORM
``User`` (audit B8). Wrappers that need database access open a fresh
tool-call-scoped session via :func:`~src.services.agent.tool_session.tool_session`
and re-load the acting user org-scoped with ``resolve_tool_user``.
"""

import asyncio
import functools
import inspect
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from langchain_core.runnables import RunnableConfig
from pydantic import Field
from typing_extensions import Annotated

from src.services.agent.tool_helpers import _reject_invalid_arxiv_ids
from src.services.agent.tool_registry import (
    AgentIntent,
    AgentSubgraph,
    ToolDescriptor,
    ToolPolicyTag,
    ToolRegistry,
)

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# Input bounds and validation
# ---------------------------------------------------------------------------

# Conservative caps for numeric LLM-controlled tool arguments. Without these,
# an LLM hallucination of ``max_depth=999`` or ``limit=10000`` can DoS the
# Neo4j / Qdrant / external connectors by triggering an unbounded fan-out.
_MAX_RESULTS_CAP = 50
_MAX_RESULTS_EXTERNAL_CAP = 100
_MAX_GRAPH_DEPTH = 5
_MAX_GRAPH_LIMIT = 100
_MAX_INGEST_BATCH = 10
# Must match the impl's own limit (tools_impl `_tool_compare_documents`), which
# rejects >5. Two independent constants drifted, so 6-10 documents were a
# guaranteed error loop: the wrapper let them through, the impl always refused.
_MAX_COMPARE_DOCUMENTS = 5
_MAX_EXPORT_DOCUMENTS = 50
_MAX_PROJECT_LIMIT = 100

# External database connector / domain identifiers must be alphanumeric +
# underscore + dash; rejecting anything else stops path-traversal-style
# inputs from bleeding into the dynamic dispatch in the implementation.
_CONNECTOR_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


def _clamp_int(value: int, *, lo: int, hi: int) -> int:
    """Clamp ``value`` into the inclusive range ``[lo, hi]``."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _reject_over_cap(
    values: Optional[List[str]], *, cap: int, noun: str
) -> Optional[Dict[str, Any]]:
    """Return an error payload when *values* exceeds *cap*, else ``None``.

    Truncating instead would drop ids the model asked for while still
    reporting success against the truncated list — the caller never learns
    anything went missing. Refusing lets the model split the batch.
    """
    if values is not None and len(values) > cap:
        return {
            "error": (
                f"Maximum {cap} {noun} per request; {len(values)} were "
                f"requested. Split them into batches of {cap} or fewer."
            )
        }
    return None


def _reject_invalid_identifier(
    value: Optional[str], *, kind: str
) -> Optional[Dict[str, Any]]:
    """Return an error payload when *value* was supplied but is not allowlisted.

    Omitting a connector/domain means "search everything" — a legitimate
    fan-out. Silently downgrading a REJECTED name to that same "unspecified"
    state turned one typo into a ~250-connector burst, so a supplied-but-bad
    identifier must fail loudly instead.
    """
    if value and _validate_connector_name(value) is None:
        return {
            "error": f"Unknown {kind} {value!r}; call list_external_databases first."
        }
    return None


def _validate_connector_name(value: Optional[str]) -> Optional[str]:
    """Return ``value`` if it matches the allowlist regex, else ``None``.

    Treating an invalid value as "unspecified" matches the original
    behaviour for missing connectors (fan-out across all) without ever
    forwarding adversarial input downstream.
    """
    if not value:
        return None
    if not _CONNECTOR_NAME_RE.match(value):
        logger.warning("Rejecting invalid external-database identifier: %r", value)
        return None
    return value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_ids(config: RunnableConfig) -> Tuple[str, str, dict]:
    """Extract (user_id, organization_id, page_context) from config.

    The configurable carries scalar ids only (audit B8) — never a live
    session or ORM user.
    """
    configurable = config.get("configurable", {})
    return (
        str(configurable.get("user_id", "") or ""),
        str(configurable.get("organization_id", "") or ""),
        configurable.get("page_context", {}) or {},
    )


@asynccontextmanager
async def _tool_context(config: RunnableConfig) -> AsyncIterator[tuple]:
    """Yield ``(session, current_user, page_context)`` for one tool call.

    Opens a fresh tool-call-scoped session and re-loads the acting user
    org-scoped from the ids in ``configurable`` (audit B8). ``current_user``
    is ``None`` when the ids are missing/invalid — implementations already
    fail closed with "Authentication required" in that case.
    """
    from src.services.agent.tool_session import resolve_tool_user, tool_session

    user_id, organization_id, page_ctx = _get_ids(config)
    async with tool_session() as session:
        current_user = await resolve_tool_user(session, user_id, organization_id)
        # Release the resolve transaction's connection back to the pool while
        # the (possibly slow) tool body runs; expire_on_commit=False keeps
        # the loaded User usable.
        await session.commit()
        yield session, current_user, page_ctx


def _resolve_project_id(
    explicit_project_id: Optional[str],
    page_context: dict,
) -> Optional[str]:
    """Return a valid project UUID, preferring explicit input.

    Discards an explicit value that isn't a UUID (LLMs occasionally
    hallucinate IDs like ``"proj_12345"``); in that case we fall back to
    the active project from ``page_context`` so the user's intent of
    "add this to the project I'm viewing" still wins.
    """
    from src.services.agent._uuid import UUID_STRICT_RE

    if explicit_project_id and UUID_STRICT_RE.match(explicit_project_id.strip()):
        return explicit_project_id.strip()
    if page_context.get("type") == "project" and page_context.get("project_id"):
        return page_context["project_id"]
    return None


def _missing_project_error(tool_name: str) -> Dict[str, Any]:
    """Standard error payload when no project_id can be resolved.

    Returning a structured error keeps the LLM aware that it must either
    supply a project_id explicitly or call list_projects to discover one,
    instead of forwarding ``""`` downstream where it surfaces as an
    opaque ``invalid UUID`` error.
    """
    return {
        "error": (
            f"{tool_name} requires a project_id but none was provided and "
            "no project page is active. Call list_projects to choose one, "
            "or include project_id explicitly."
        ),
        # Declared recoverable, and it now takes effect. The old
        # "user_fixable" label never applied to anything: this wording
        # ("requires a project_id") matches no TOOL_ERROR_HINTS key and no
        # step-5 keyword, so the payload reached the model as *fatal* —
        # telling the agent not to recover from a situation the message
        # itself explains how to fix. list_projects is bound to every
        # subgraph that can raise this, so the suggestion is followable.
        #
        # Careful: rewording this to "project_id is required" would hand
        # create_draft / create_project_note back to TOOL_ERROR_HINTS and
        # drop the suggestion below. test_missing_project_error_is_recoverable
        # pins the delivered payload.
        "error_type": "recoverable",
        "suggestion": "list_projects",
    }


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_search_arxiv

    args: Dict[str, Any] = {
        "query": query,
        "max_results": _clamp_int(max_results, lo=1, hi=_MAX_RESULTS_CAP),
        "recency_days": _clamp_int(recency_days, lo=0, hi=36500),
        "chronological": bool(chronological),
    }
    if categories:
        args["categories"] = categories
    return await _tool_search_arxiv(args)


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_ingest_arxiv

    # Bound the batch — ingestion is heavy and a hallucinated 100-paper batch
    # will saturate the worker pool and trip downstream timeouts. Refuse rather
    # than truncate: the old slice dropped ids 11+ and still reported success
    # over the ten that survived.
    over_cap = _reject_over_cap(paper_ids, cap=_MAX_INGEST_BATCH, noun="papers")
    if over_cap:
        return over_cap

    invalid = _reject_invalid_arxiv_ids(paper_ids)
    if invalid:
        return invalid

    async with _tool_context(config) as (db, current_user, page_ctx):
        user_id = str(current_user.id) if current_user else ""
        resolved_project_id = _resolve_project_id(project_id, page_ctx)
        return await _tool_ingest_arxiv(
            {
                "paper_ids": [str(p).strip() for p in (paper_ids or [])],
                "project_id": resolved_project_id,
            },
            user_id,
            db,
            current_user,
        )


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_search_documents

    async with _tool_context(config) as (db, current_user, _page_ctx):
        return await _tool_search_documents(
            {
                "query": query,
                "max_results": _clamp_int(max_results, lo=1, hi=_MAX_RESULTS_CAP),
            },
            db,
            current_user,
        )


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_do_kb_retrieve

    async with _tool_context(config) as (db, current_user, page_ctx):
        # Forward active project_id so the retrieval result is scoped to the
        # current project and does not leak sibling-project documents.
        project_id = _resolve_project_id(None, page_ctx)
        args: Dict[str, Any] = {
            "query": query,
            "top_k": _clamp_int(top_k, lo=1, hi=20),
        }
        if project_id:
            args["project_id"] = project_id
        if document_ids is not None:
            # Presence is meaningful: an explicit empty list is invalid scoped
            # intent and must reach the dispatcher instead of broadening.
            args["document_ids"] = document_ids
        return await _tool_do_kb_retrieve(args, db, current_user)


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_add_document_to_project

    async with _tool_context(config) as (db, current_user, page_ctx):
        resolved_pid = _resolve_project_id(project_id, page_ctx)
        if not resolved_pid:
            return _missing_project_error("add_document_to_project")
        return await _tool_add_document_to_project(
            {"document_id": document_id, "project_id": resolved_pid},
            db,
            current_user,
        )


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_create_project

    args: Dict[str, Any] = {"name": name}
    if description:
        args["description"] = description
    if research_goals:
        args["research_goals"] = research_goals
    if tags:
        args["tags"] = tags
    if workspace_id:
        args["workspace_id"] = workspace_id
    async with _tool_context(config) as (db, current_user, _page_ctx):
        return await _tool_create_project(args, db, current_user)


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_create_project_note

    async with _tool_context(config) as (db, current_user, page_ctx):
        resolved_pid = _resolve_project_id(project_id, page_ctx)
        if not resolved_pid:
            return _missing_project_error("create_project_note")
        args: Dict[str, Any] = {
            "title": title,
            "content": content,
            "project_id": resolved_pid,
        }
        if tags:
            args["tags"] = tags
        return await _tool_create_project_note(args, db, current_user)


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_list_projects

    args: Dict[str, Any] = {
        "limit": _clamp_int(limit, lo=1, hi=_MAX_PROJECT_LIMIT),
    }
    if status:
        args["status"] = status
    if tag:
        args["tag"] = tag
    if search:
        args["search"] = search
    async with _tool_context(config) as (db, current_user, _page_ctx):
        return await _tool_list_projects(args, db, current_user)


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_list_project_documents

    async with _tool_context(config) as (db, current_user, page_ctx):
        resolved_pid = _resolve_project_id(project_id, page_ctx)
        if not resolved_pid:
            return _missing_project_error("list_project_documents")
        return await _tool_list_project_documents(
            {
                "project_id": resolved_pid,
                "limit": _clamp_int(limit, lo=1, hi=500),
                "offset": _clamp_int(offset, lo=0, hi=2**31 - 1),
            },
            db,
            current_user,
        )


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_get_current_draft

    async with _tool_context(config) as (db, current_user, page_ctx):
        resolved_pid = _resolve_project_id(project_id, page_ctx)
        if not resolved_pid:
            return _missing_project_error("get_current_draft")
        return await _tool_get_current_draft(
            {"project_id": resolved_pid, "include_content": include_content},
            db,
            current_user,
        )


@tool
async def summarize_document(
    document_id: str,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Summarize a document's content.

    Use when the user asks for a summary or overview of a specific document.
    """
    config = config or {}
    from src.services.agent.tools_impl import _tool_summarize_document

    async with _tool_context(config) as (db, current_user, _page_ctx):
        return await _tool_summarize_document(
            {"document_id": document_id}, db, current_user
        )


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_compare_documents

    _COMPARISON_TYPES = {"general", "methodology", "findings", "themes"}
    safe_type = type if type in _COMPARISON_TYPES else "general"
    over_cap = _reject_over_cap(
        document_ids, cap=_MAX_COMPARE_DOCUMENTS, noun="documents"
    )
    if over_cap:
        return over_cap
    async with _tool_context(config) as (db, current_user, _page_ctx):
        return await _tool_compare_documents(
            {"document_ids": list(document_ids or []), "type": safe_type},
            db,
            current_user,
        )


@tool
async def extract_entities(
    document_id: str,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Extract named entities (people, organizations, concepts, etc.) from a document.

    Use when the user wants to identify key entities, people, organizations,
    or concepts mentioned in a document.
    """
    config = config or {}
    from src.services.agent.tools_impl import _tool_extract_entities

    async with _tool_context(config) as (db, current_user, _page_ctx):
        return await _tool_extract_entities(
            {"document_id": document_id}, db, current_user
        )


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_search_knowledge_graph

    args: Dict[str, Any] = {"query": query}
    if entity_types:
        args["entity_types"] = entity_types
    async with _tool_context(config) as (_db, current_user, _page_ctx):
        return await _tool_search_knowledge_graph(args, current_user)


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_explore_entity_neighborhood

    async with _tool_context(config) as (_db, current_user, _page_ctx):
        return await _tool_explore_entity_neighborhood(
            {
                "entity_id": entity_id,
                "max_depth": _clamp_int(max_depth, lo=1, hi=_MAX_GRAPH_DEPTH),
                "limit": _clamp_int(limit, lo=1, hi=_MAX_GRAPH_LIMIT),
            },
            current_user,
        )


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_find_entity_paths

    async with _tool_context(config) as (_db, current_user, _page_ctx):
        return await _tool_find_entity_paths(
            {
                "source_entity_id": source_entity_id,
                "target_entity_id": target_entity_id,
                "max_depth": _clamp_int(max_depth, lo=1, hi=_MAX_GRAPH_DEPTH),
            },
            current_user,
        )


@tool
async def get_graph_stats(
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Get statistics about the knowledge graph.

    Returns total entities, relationships, type distributions, and
    connectivity metrics. Use when the user asks about the size or shape
    of the knowledge base, or wants an overview of what's in the graph.
    """
    config = config or {}
    from src.services.agent.tools_impl import _tool_get_graph_stats

    async with _tool_context(config) as (_db, current_user, _page_ctx):
        return await _tool_get_graph_stats({}, current_user)


@tool
async def create_draft(
    themes: List[str],
    project_id: Optional[str] = None,
    style: str = "academic",
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Generate a literature review draft for a project based on themes.

    Use when the user wants to create a draft, write a review, or synthesize
    research around specific themes.
    """
    config = config or {}
    from src.services.agent.tools_impl import _tool_create_draft

    _DRAFT_STYLES = {"academic", "technical", "summary"}
    safe_style = style if style in _DRAFT_STYLES else "academic"
    async with _tool_context(config) as (db, current_user, page_ctx):
        resolved_pid = _resolve_project_id(project_id, page_ctx)
        if not resolved_pid:
            return _missing_project_error("create_draft")
        return await _tool_create_draft(
            {"project_id": resolved_pid, "themes": themes, "style": safe_style},
            db,
            current_user,
        )


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_export_bibliography

    over_cap = _reject_over_cap(
        document_ids, cap=_MAX_EXPORT_DOCUMENTS, noun="documents"
    )
    if over_cap:
        return over_cap
    async with _tool_context(config) as (db, current_user, _page_ctx):
        return await _tool_export_bibliography(
            {"document_ids": document_ids, "format": format}, db, current_user
        )


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_execute_code

    configurable = config.get("configurable", {})

    async with _tool_context(config) as (_db, current_user, _page_ctx):
        # The sandbox is keyed by this id and is stateful, so a shared literal
        # fallback ("default") put every thread that arrived without a
        # thread_id into ONE sandbox — variables and files leaking across
        # conversations and users. Fall back to the caller's own id, and fail
        # closed when there isn't one rather than joining the shared box.
        thread_id = configurable.get("thread_id") or (
            f"user-{current_user.id}" if current_user else None
        )
        if not thread_id:
            return {"error": "Authentication required"}

        return await _tool_execute_code(
            {
                "code": code,
                "description": description,
                "language": language,
                "packages": packages,
            },
            thread_id=thread_id,
            current_user=current_user,
        )


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

    Provides a single entry point to 250+ external scientific and financial
    data sources. Supply ``connector`` to target one (e.g. ``"pubmed"``),
    ``domain`` to fan out across a category (``biomedical``, ``chemistry``,
    ``finance``, ``clinical``, ``genomics``, ``economic``, ``literature``),
    or omit both to search every available connector concurrently.

    Use when the user asks for proteins, compounds, mutations, clinical trials,
    economic time series, SEC filings, or any other domain-specific data not
    available in the local document store.
    """
    config = config or {}
    from src.services.agent.tools_impl import _tool_search_external_database

    bad_identifier = _reject_invalid_identifier(
        connector, kind="connector"
    ) or _reject_invalid_identifier(domain, kind="domain")
    if bad_identifier:
        return bad_identifier

    args: Dict[str, Any] = {
        "query": query,
        "max_results": _clamp_int(max_results, lo=1, hi=_MAX_RESULTS_EXTERNAL_CAP),
    }
    safe_connector = _validate_connector_name(connector)
    if safe_connector:
        args["connector"] = safe_connector
    safe_domain = _validate_connector_name(domain)
    if safe_domain:
        args["domain"] = safe_domain
    # Filters are an opaque mapping; only pass through scalar values to
    # keep the dispatch surface small and prevent nested-payload abuse.
    if filters and isinstance(filters, dict):
        scalar_filters = {
            k: v
            for k, v in filters.items()
            if isinstance(k, str) and isinstance(v, (str, int, float, bool))
        }
        if scalar_filters:
            args["filters"] = scalar_filters
    return await _tool_search_external_database(args)


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_list_external_databases

    bad_identifier = _reject_invalid_identifier(domain, kind="domain")
    if bad_identifier:
        return bad_identifier

    args: Dict[str, Any] = {}
    safe_domain = _validate_connector_name(domain)
    if safe_domain:
        args["domain"] = safe_domain
    return await _tool_list_external_databases(args)


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
    config = config or {}
    from src.services.agent.tools_impl import _tool_forget_memory

    # Memory is keyed by the scalar user_id — no session or ORM user needed.
    user_id, org_id, page_ctx = _get_ids(config)
    return await _tool_forget_memory(
        query=query, user_id=user_id, organization_id=org_id, page_context=page_ctx
    )


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
    config = config or {}
    configurable = config.get("configurable", {})
    snapshot_id = str(configurable.get("runtime_snapshot_id", "") or "")
    project_id = str(configurable.get("project_id", "") or "")
    if not snapshot_id or not project_id:
        return {
            "error_type": "runtime_snapshot_required",
            "error": "Project skill loading requires server runtime snapshot context.",
        }

    from src.services.agent.tools_impl import _tool_load_project_skill

    async with _tool_context(config) as (db, current_user, _page_ctx):
        user_id = str(current_user.id) if current_user is not None else ""
        return await _tool_load_project_skill(
            {"skill_name": skill_name},
            user_id=user_id,
            project_id=project_id,
            runtime_snapshot_id=snapshot_id,
            db=db,
        )


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
            subgraphs=frozenset({AgentSubgraph.RESEARCH, AgentSubgraph.DATA}),
            subgraph_positions=((AgentSubgraph.RESEARCH, 2), (AgentSubgraph.DATA, 5)),
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
            subgraphs=frozenset({AgentSubgraph.RESEARCH, AgentSubgraph.DATA}),
            subgraph_positions=((AgentSubgraph.RESEARCH, 3), (AgentSubgraph.DATA, 8)),
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
            subgraph_positions=((AgentSubgraph.WRITING, 13),),
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
            policy_tags=frozenset(
                {ToolPolicyTag.DESTRUCTIVE, ToolPolicyTag.NO_OUTER_RETRY}
            ),
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
        ),
    ]
)

# Compatibility import for existing callers.  This view intentionally omits
# the research-only do_kb_retrieve wrapper, preserving the prior ALL_TOOLS API.
ALL_TOOLS = list(TOOL_REGISTRY.all_tools())
