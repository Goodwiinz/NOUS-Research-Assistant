"""Read-only NOUS tool gateway for external harnesses.

Identity (actor, organization, project) comes from the resolved
``IntegrationContext``; model-supplied arguments never choose it. The
allowlist is deliberately narrow: the absence of a ``DESTRUCTIVE`` tag is not
evidence that a registry tool is safe to expose outside the agent graph.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from starlette.concurrency import run_in_threadpool

from src.models.collection import Collection, CollectionDocument
from src.models.document import Document
from src.models.user import User
from src.models.workspace import Workspace
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_tools import ToolDescriptorDTO, ToolInvocation, ToolResult
from src.services.agent.tool_helpers import _escape_like, _verify_project_ownership
from src.services.agent.tools_impl import (
    _tool_do_kb_retrieve,
    _tool_get_current_draft,
    _tool_list_external_databases,
    _tool_list_project_documents,
    _tool_search_arxiv,
    _tool_search_external_database,
)
from src.services.integrations import arxiv_fulltext
from src.services.integrations.arxiv_fulltext import MAX_PAGE_CHARS
from src.services.integrations.context import IntegrationAccessDenied
from src.services.search.fulltext_search_service import fulltext_search_service

logger = logging.getLogger(__name__)

# Tools with an agent-registry twin; the catalog schema derives from it.
READ_TOOL_NAMES: tuple[str, ...] = (
    "search_documents",
    "list_project_documents",
    "do_kb_retrieve",
    "get_current_draft",
    "search_arxiv",
    "search_external_database",
    "list_external_databases",
)
# Args-only tools: no project scoping, dispatched before the ownership check.
_ARGS_ONLY_TOOLS = frozenset(
    {"search_arxiv", "search_external_database", "list_external_databases"}
)
IDENTITY_ARGUMENTS = frozenset(
    {"project_id", "user_id", "organization_id", "thread_id", "run_id", "grant_id"}
)
MAX_RESULTS = 50
MAX_DOCUMENT_IDS = 20
MAX_EXTERNAL_RESULTS = 20
MAX_RESULT_BYTES = 64 * 1024
# Bytes a content page may occupy once JSON-encoded; leaves headroom under
# MAX_RESULT_BYTES for the envelope.
MAX_PAGE_BYTES = 48 * 1024
# Keeps a content-bearing draft under MAX_RESULT_BYTES after JSON escaping.
MAX_DRAFT_CONTENT_CHARS = 32_000
# Researcher tools read the author lists of the grant's newest documents.
MAX_RESEARCHERS = 25
MAX_SCANNED_DOCUMENTS = 2000
MAX_RESEARCHER_PAPERS = 100
MAX_COAUTHORS = 100
_NAME_CHARS = 200
# The advertised schema must match what the gateway enforces; the agent-facing
# registry descriptions promise wider limits (e.g. list limit 500).
_SCHEMA_OVERRIDES: dict[str, dict[str, dict[str, Any]]] = {
    "search_documents": {"max_results": {"maximum": MAX_RESULTS}},
    "list_project_documents": {
        "limit": {"maximum": MAX_RESULTS, "default": MAX_RESULTS}
    },
    "do_kb_retrieve": {
        "top_k": {"maximum": MAX_DOCUMENT_IDS},
        "document_ids": {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "minItems": 1,
            "maxItems": MAX_DOCUMENT_IDS,
            "description": "Project document UUIDs to retrieve from (required).",
        },
    },
    "search_arxiv": {"max_results": {"maximum": MAX_EXTERNAL_RESULTS}},
    "search_external_database": {"max_results": {"maximum": MAX_EXTERNAL_RESULTS}},
}
_EXTRA_REQUIRED = {"do_kb_retrieve": ["document_ids"]}

_UUID_ITEMS = {"type": "string", "format": "uuid"}
_OFFSET = {"type": "integer", "minimum": 0, "default": 0}
_PAGE_LIMIT = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_PAGE_CHARS,
    "default": MAX_PAGE_CHARS,
    "description": "Characters per page.",
}
# Tools without a registry twin: name -> (description, input_schema, scope).
LOCAL_TOOLS: dict[str, tuple[str, dict[str, Any], str]] = {
    "get_document_content": (
        "Read a project document's summary or full text. Paginated by "
        "characters; follow next_offset until it is null.",
        {
            "type": "object",
            "properties": {
                "document_id": _UUID_ITEMS,
                "mode": {
                    "type": "string",
                    "enum": ["summary", "full"],
                    "default": "summary",
                },
                "offset": _OFFSET,
                "limit": _PAGE_LIMIT,
            },
            "required": ["document_id"],
            "additionalProperties": False,
        },
        "tools:read",
    ),
    "retrieve_passages": (
        "Full-text passage retrieval over the granted project's documents "
        "(PostgreSQL ranking; works without a semantic knowledge base). "
        "Omit document_ids to search the whole project.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "document_ids": {
                    "type": "array",
                    "items": _UUID_ITEMS,
                    "minItems": 1,
                    "maxItems": MAX_DOCUMENT_IDS,
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_DOCUMENT_IDS,
                    "default": 8,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "tools:read",
    ),
    "get_arxiv_paper_content": (
        "Read the full text of an arXiv paper by id without ingesting it. "
        "Paginated by characters; follow next_offset until it is null.",
        {
            "type": "object",
            "properties": {
                "arxiv_id": {"type": "string", "maxLength": 32},
                "offset": _OFFSET,
                "limit": _PAGE_LIMIT,
            },
            "required": ["arxiv_id"],
            "additionalProperties": False,
        },
        "tools:read",
    ),
    "find_researchers": (
        "Find researchers by name among the author lists of papers ingested "
        "into this project (arXiv metadata). Case-insensitive substring match; "
        "most papers first, at most 25. Documents without author metadata are "
        "invisible, and there are no affiliations or career history. Only the "
        "2,000 newest documents are scanned (truncated=true when more exist). "
        "Pass a researcher_id to get_researcher.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_RESEARCHERS,
                    "default": 10,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "tools:read",
    ),
    "get_researcher": (
        "Read one researcher by researcher_id (from find_researchers or a "
        "co-author entry): their papers in the granted project (up to 100, "
        "newest published first, with arxiv_id and published when known) and "
        "co-authors with shared-paper counts (up to 100). Coverage is author "
        "lists of papers ingested into this project (arXiv metadata); no "
        "affiliations or career history. researcher_not_found means no "
        "document of this project lists that author.",
        {
            "type": "object",
            "properties": {
                "researcher_id": {"type": "string", "minLength": 1, "maxLength": 200}
            },
            "required": ["researcher_id"],
            "additionalProperties": False,
        },
        "tools:read",
    ),
}
TOOL_SCOPES: dict[str, str] = {name: "tools:read" for name in READ_TOOL_NAMES} | {
    name: scope for name, (_d, _s, scope) in LOCAL_TOOLS.items()
}
_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "array": list,
}


class ToolArgumentError(ValueError):
    """Invocation shape rejected before any adapter runs (HTTP 422)."""


def _registry_schema(name: str) -> tuple[str, dict[str, Any], type[BaseModel]]:
    from src.services.agent.tools import TOOL_REGISTRY

    descriptor = TOOL_REGISTRY.descriptor(name)
    assert descriptor is not None, name
    raw_schema = descriptor.tool.tool_call_schema
    # tool_call_schema is typed as a v2/v1 model class or dict; the registry
    # only holds decorated @tool wrappers, which always yield a v2 class.
    if not (isinstance(raw_schema, type) and issubclass(raw_schema, BaseModel)):
        raise TypeError(f"{name} has no pydantic v2 call schema")
    model = cast(type[BaseModel], raw_schema)
    schema = dict(model.model_json_schema())
    properties = {
        key: value
        for key, value in schema.get("properties", {}).items()
        if key not in IDENTITY_ARGUMENTS
    }
    for key, override in _SCHEMA_OVERRIDES.get(name, {}).items():
        properties[key] = (
            override if "type" in override else {**properties[key], **override}
        )
    schema["properties"] = properties
    schema["required"] = [
        key for key in schema.get("required", []) if key in properties
    ] + _EXTRA_REQUIRED.get(name, [])
    schema["additionalProperties"] = False
    return descriptor.tool.description, schema, model


def list_read_tools() -> list[ToolDescriptorDTO]:
    return [
        ToolDescriptorDTO(name=name, description=desc_, input_schema=schema)
        for name in READ_TOOL_NAMES
        for desc_, schema, _model in (_registry_schema(name),)
    ] + [
        ToolDescriptorDTO(name=name, description=desc_, input_schema=schema)
        for name, (desc_, schema, _scope) in LOCAL_TOOLS.items()
    ]


def _check_local_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Plain key/type/bounds check against a LOCAL_TOOLS schema (no coercion)."""

    def check(spec: dict[str, Any], value: Any) -> bool:
        expected = _JSON_TYPES[spec["type"]]
        if isinstance(value, bool) or not isinstance(value, expected):
            return False
        if "enum" in spec and value not in spec["enum"]:
            return False
        if isinstance(value, int):
            return spec.get("minimum", value) <= value <= spec.get("maximum", value)
        if isinstance(value, str):
            return (
                spec.get("minLength", 0) <= len(value) <= spec.get("maxLength", 2**31)
            )
        return spec.get("minItems", 0) <= len(value) <= spec.get(
            "maxItems", 2**31
        ) and all(check(spec["items"], item) for item in value)

    for key, value in arguments.items():
        if not check(schema["properties"][key], value):
            raise ToolArgumentError("invalid argument types")


def _validate_arguments(invocation: ToolInvocation) -> dict[str, Any]:
    from pydantic import ValidationError

    name = invocation.tool_name
    if name not in READ_TOOL_NAMES and name not in LOCAL_TOOLS:
        raise ToolArgumentError("tool is not available to integrations")
    arguments = invocation.arguments
    if name in LOCAL_TOOLS:
        _, schema, _scope = LOCAL_TOOLS[name]
        if set(arguments) - set(schema["properties"]):
            raise ToolArgumentError("unknown arguments")
        if set(schema["required"]) - set(arguments):
            raise ToolArgumentError("missing required arguments")
        _check_local_arguments(schema, arguments)
        return arguments
    _, schema, model = _registry_schema(name)
    # Identity keys were stripped from the advertised schema, so a caller
    # supplying project_id/user_id/... is rejected here as an unknown argument.
    allowed = set(schema["properties"])
    if set(arguments) - allowed:
        raise ToolArgumentError("unknown arguments")
    if set(schema["required"]) - set(arguments):
        raise ToolArgumentError("missing required arguments")
    try:
        # strict: no "false"->bool, float->int or dict->str coercion.
        model.model_validate(arguments, strict=True)
    except ValidationError as error:
        raise ToolArgumentError("invalid argument types") from error
    return arguments


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError, OverflowError):
        return default


def _live_project_documents(context: IntegrationContext) -> Any:
    """Documents in the grant's project with every ancestor still live.

    Joined in the same statement so a soft-delete landing between the
    ownership check and this fetch cannot return revoked rows.
    """
    return (
        select(Document)
        .join(CollectionDocument, CollectionDocument.document_id == Document.id)
        .join(Collection, Collection.id == CollectionDocument.collection_id)
        .join(Workspace, Workspace.id == Collection.workspace_id)
        .where(
            Collection.id == context.project_id,
            Collection.is_deleted == False,  # noqa: E712
            Workspace.is_deleted == False,  # noqa: E712
            Workspace.organization_id == context.organization_id,
            CollectionDocument.is_deleted == False,  # noqa: E712
            Document.organization_id == context.organization_id,
            Document.is_deleted == False,  # noqa: E712
        )
    )


async def _search_project_documents(
    db: AsyncSession, context: IntegrationContext, query: str, limit: int
) -> dict[str, Any]:
    """Title/filename search joined to the grant's project, never org-wide."""
    from src.services.agent._pii_redact import redact_pii

    pattern = f"%{_escape_like(query)}%"
    rows = await db.execute(
        _live_project_documents(context)
        .where(Document.title.ilike(pattern) | Document.filename.ilike(pattern))
        .order_by(desc(Document.created_at))
        .limit(limit)
    )
    docs = rows.scalars().all()
    return {
        "documents": [
            {
                "id": str(d.id),
                "title": redact_pii(d.title) if d.title else d.title,
                "type": d.document_type.value if d.document_type else None,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in docs
        ],
        "total": len(docs),
        "query": query,
    }


async def _project_document_ids(
    db: AsyncSession, context: IntegrationContext, raw_ids: Any
) -> list[str] | None:
    """Require 1–20 UUIDs; None when any id is outside the grant's project."""
    if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= MAX_DOCUMENT_IDS:
        raise ToolArgumentError("document_ids must contain 1-20 UUIDs")
    try:
        requested = {UUID(str(value)) for value in raw_ids}
    except ValueError as error:
        raise ToolArgumentError("document_ids must be UUIDs") from error
    rows = await db.execute(
        _live_project_documents(context)
        .with_only_columns(Document.id)
        .where(Document.id.in_(requested))
    )
    members = {UUID(str(value)) for value in rows.scalars().all()}
    if members != requested:
        return None
    # Canonical spelling so later equality checks match DB-emitted ids.
    return [str(UUID(str(value))) for value in raw_ids]


async def _collection_document_ids(
    db: AsyncSession, context: IntegrationContext
) -> list[str]:
    """Every live document in the grant's Collection."""
    # ponytail: unbounded id list -> one bind param per document; add a cap
    # or a join-based filter in the search service if collections grow large.
    rows = await db.execute(
        _live_project_documents(context).with_only_columns(Document.id)
    )
    return [str(value) for value in rows.scalars().all()]


def _unavailable() -> ToolResult:
    """Foreign and unknown ids look identical: no membership probing."""
    return ToolResult(
        content=[
            {
                "error": "requested_documents_unavailable",
                "reason": "requested_documents_unavailable",
                "chunks": [],
                "total": 0,
            }
        ],
        is_error=True,
        source_refs=[],
    )


def _page(text: str, offset: int, limit: int) -> tuple[str, int | None]:
    """Character page whose JSON encoding stays under MAX_PAGE_BYTES."""
    chunk = text[offset : offset + limit]
    while chunk:
        wire = len(json.dumps(chunk, ensure_ascii=False).encode())
        if wire <= MAX_PAGE_BYTES:
            break
        chunk = chunk[: max(1, int(len(chunk) * MAX_PAGE_BYTES / wire))]
    end = offset + len(chunk)
    return chunk, end if end < len(text) else None


async def _document_content(
    db: AsyncSession, context: IntegrationContext, arguments: dict[str, Any]
) -> ToolResult:
    try:
        document_id = UUID(str(arguments["document_id"]))
    except ValueError as error:
        raise ToolArgumentError("document_id must be a UUID") from error
    mode = str(arguments.get("mode", "summary"))
    statement = _live_project_documents(context).where(Document.id == document_id)
    if mode != "full":
        statement = statement.options(defer(Document.content_text))
    rows = await db.execute(statement)
    document = rows.scalars().first()
    if document is None:
        return _unavailable()
    text = (
        document.content_text or ""
        if mode == "full"
        else document.content_summary or document.get_content_preview(500) or ""
    )
    offset = _clamp(arguments.get("offset", 0), 0, 2**31 - 1, 0)
    chunk, next_offset = _page(
        text,
        offset,
        _clamp(
            arguments.get("limit", MAX_PAGE_CHARS), 1, MAX_PAGE_CHARS, MAX_PAGE_CHARS
        ),
    )
    ref = {"document_id": str(document.id)}
    return ToolResult(
        content=[
            {
                "document_id": str(document.id),
                "title": document.title,
                "mode": mode,
                "offset": offset,
                "next_offset": next_offset,
                "total_chars": len(text),
                "text": chunk,
                "source_refs": [ref],
            }
        ],
        is_error=False,
        source_refs=[ref],
    )


_MARK_TAGS = ("<mark>", "</mark>")


async def _retrieve_passages(
    db: AsyncSession, context: IntegrationContext, arguments: dict[str, Any]
) -> dict[str, Any]:
    from src.models.search_schemas import SearchFilter, SearchQuery, SearchType
    from src.services.agent._pii_redact import redact_pii

    query = str(arguments["query"])
    top_k = _clamp(arguments.get("top_k", 8), 1, MAX_DOCUMENT_IDS, 8)
    if "document_ids" in arguments:
        document_ids = await _project_document_ids(
            db, context, arguments["document_ids"]
        )
        if document_ids is None:
            return _unavailable().content[0]
    else:
        document_ids = await _collection_document_ids(db, context)
    empty = {"chunks": [], "total": 0, "query": query}
    if not document_ids:
        return empty
    request = SearchQuery(
        query=query,
        search_type=SearchType.FULLTEXT,
        limit=top_k,
        filters=SearchFilter(document_ids=document_ids),
    )
    # The live /documents/search path: synchronous PostgreSQL ts_rank_cd, so
    # it runs off the event loop; the service opens its own sync session.
    try:
        response = await run_in_threadpool(
            fulltext_search_service.search,
            search_request=request,
            user_id=str(context.user_id),
            organization_id=str(context.organization_id),
        )
    except Exception:
        logger.warning("retrieve_passages failed", exc_info=True)
        return {"error": "retrieval_unavailable"}
    allowed = set(document_ids)
    chunks = []
    for hit in response.results:
        if hit.document_id not in allowed:
            continue  # belt and braces: the filter already scoped the SQL
        text = " ".join(s.text for s in hit.snippets) or hit.content_preview
        for tag in _MARK_TAGS:
            text = text.replace(tag, "")
        chunks.append(
            {
                "document_id": hit.document_id,
                # Redacted like search_documents (do_kb_retrieve passes the
                # provider title through unchanged; this path owns its rows).
                "title": redact_pii(hit.title) if hit.title else hit.title,
                "text": text,
                "score": hit.relevance_score,
            }
        )
    return {"chunks": chunks, "total": len(chunks), "query": query}


def _search_arxiv_budget_seconds() -> float:
    # search_arxiv is SLOW / NO_OUTER_RETRY in the agent graph; same backstop.
    from src.services.agent._nodes_tools import _SLOW_TOOL_TIMEOUT_SECONDS

    return float(_SLOW_TOOL_TIMEOUT_SECONDS)


class _NoCache:
    async def get(self, _key: str) -> None:
        return None

    async def set(self, _key: str, _value: str, ex: int | None = None) -> None:
        return None


_arxiv_cache_client: Any = None


def _arxiv_cache() -> Any:
    """Lazy module-level Redis pool; _NoCache when REDIS_URL is unset."""
    global _arxiv_cache_client
    if _arxiv_cache_client is None:
        try:
            import redis.asyncio as redis_async
            from redis.asyncio.retry import Retry
            from redis.backoff import NoBackoff

            from src.core.config import settings

            _arxiv_cache_client = (
                redis_async.from_url(
                    settings.REDIS_URL,
                    decode_responses=True,
                    # Same bounds as core/rate_limit.py: a slow cache must not
                    # stall the read; get_page treats errors as misses.
                    socket_connect_timeout=0.25,
                    socket_timeout=0.25,
                    retry=Retry(NoBackoff(), 0),
                )
                if settings.REDIS_URL
                else _NoCache()
            )
        except Exception:
            logger.debug("arxiv fulltext cache unavailable", exc_info=True)
            _arxiv_cache_client = _NoCache()
    return _arxiv_cache_client


async def _arxiv_paper_content(arguments: dict[str, Any]) -> ToolResult:
    arxiv_id = str(arguments["arxiv_id"])
    offset = _clamp(arguments.get("offset", 0), 0, 2**31 - 1, 0)
    limit = _clamp(
        arguments.get("limit", MAX_PAGE_CHARS), 1, MAX_PAGE_CHARS, MAX_PAGE_CHARS
    )
    try:
        page = await asyncio.wait_for(
            arxiv_fulltext.get_page(
                arxiv_id, offset=offset, limit=limit, redis=_arxiv_cache()
            ),
            timeout=_search_arxiv_budget_seconds(),
        )
    except ValueError as error:
        raise ToolArgumentError("invalid arxiv id") from error
    except asyncio.TimeoutError:
        return _finish({"error": "upstream_timeout"})
    except LookupError:
        return _finish({"error": "arxiv_unavailable"})
    except Exception:
        logger.warning("get_arxiv_paper_content failed", exc_info=True)
        return _finish({"error": "arxiv_unavailable"})
    if not page["total_chars"]:
        return _finish({"error": "arxiv_no_text"})
    # get_page slices by characters; re-page by bytes so the 64 KiB cap holds.
    text, next_offset = _page(page["text"], 0, limit)
    page["text"] = text
    page["next_offset"] = (
        offset + len(text) if offset + len(text) < page["total_chars"] else None
    )
    return _finish({**page, "source_refs": [{"arxiv_id": arxiv_id}]})


@dataclass(frozen=True)
class _AuthoredPaper:
    document_id: str
    title: str
    arxiv_id: str | None
    published: str | None
    created_at: str
    authors: tuple[tuple[str, str], ...]  # (researcher_id, display name)


def _researcher_key(name: Any) -> str:
    """researcher_id: casefolded, whitespace-collapsed, at most 200 characters."""
    return " ".join(str(name).split()).casefold()[:_NAME_CHARS]


def _author_names(raw: Any) -> tuple[tuple[str, str], ...]:
    """(researcher_id, display name) per distinct author of one document.

    Tolerates what ingest has persisted: a list of strings (arXiv), legacy
    ``{"name": ...}`` objects, a lone string, or no authors at all.
    """
    authors: dict[str, str] = {}
    for item in raw if isinstance(raw, list) else [raw]:
        name = item.get("name") if isinstance(item, dict) else item
        key = _researcher_key(name) if isinstance(name, str) else ""
        if key:
            authors.setdefault(key, " ".join(name.split())[:_NAME_CHARS])
    return tuple(authors.items())


def _published(metadata: dict[str, Any]) -> str | None:
    # arXiv ingest stores publication_date; the change tracker stores published.
    for field in ("publication_date", "published"):
        value = metadata.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:40]
    return None


async def _scan_authored_papers(
    db: AsyncSession, context: IntegrationContext
) -> tuple[list[_AuthoredPaper], bool]:
    """The grant's newest live documents that carry an author list.

    Returns ``(papers, truncated)``. This is the only data source of the
    researcher tools and it is project-scoped by construction:
    ``_live_project_documents`` joins the grant's project and every ancestor.
    """
    from src.services.agent._pii_redact import redact_pii

    # ponytail: O(documents x authors) in Python over at most
    # MAX_SCANNED_DOCUMENTS (2000) newest documents, in one narrow-column
    # query. Fine up to a few thousand documents; older ones are invisible and
    # reported as truncated. Move the aggregation into SQL or a per-project
    # author index if projects outgrow that.
    rows = (
        await db.execute(
            _live_project_documents(context)
            .with_only_columns(
                Document.id,
                Document.title,
                Document.arxiv_id,
                Document.created_at,
                Document.document_metadata,
            )
            .order_by(desc(Document.created_at), Document.id)
            .limit(MAX_SCANNED_DOCUMENTS + 1)
        )
    ).all()
    papers: list[_AuthoredPaper] = []
    for document_id, title, arxiv_id, created_at, metadata in rows[
        :MAX_SCANNED_DOCUMENTS
    ]:
        meta = metadata if isinstance(metadata, dict) else {}
        authors = _author_names(meta.get("authors"))
        if not authors:
            continue  # no author metadata: invisible to the researcher tools
        arxiv = arxiv_id or meta.get("arxiv_id")
        papers.append(
            _AuthoredPaper(
                document_id=str(document_id),
                title=redact_pii(title)[:500],
                arxiv_id=str(arxiv).strip()[:64] if arxiv else None,
                published=_published(meta),
                created_at=created_at.isoformat() if created_at else "",
                authors=authors,
            )
        )
    return papers, len(rows) > MAX_SCANNED_DOCUMENTS


async def _find_researchers(
    db: AsyncSession, context: IntegrationContext, arguments: dict[str, Any]
) -> ToolResult:
    needle = _researcher_key(arguments["query"])
    if not needle:
        raise ToolArgumentError("query must not be blank")
    limit = _clamp(arguments.get("limit", 10), 1, MAX_RESEARCHERS, 10)
    papers, truncated = await _scan_authored_papers(db, context)
    found: dict[str, list[Any]] = {}  # researcher_id -> [first-seen name, papers]
    for paper in papers:  # newest first, so the display name is the newest one
        for key, name in paper.authors:
            if needle in key:
                found.setdefault(key, [name, 0])[1] += 1
    ranked = sorted(found.items(), key=lambda item: (-item[1][1], item[0]))
    payload: dict[str, Any] = {
        "researchers": [
            {"researcher_id": key, "name": name, "paper_count": count}
            for key, (name, count) in ranked[:limit]
        ]
    }
    if truncated:
        payload["truncated"] = True
    # A researcher is not a source document: there is nothing to cite.
    return _finish(payload)


def _fit_to_result_cap(payload: dict[str, Any]) -> None:
    """Halve co-authors, then papers, until the payload fits MAX_RESULT_BYTES."""

    def wire() -> int:
        return len(json.dumps(payload, default=str, ensure_ascii=False).encode())

    for key in ("coauthors", "papers"):
        while wire() > MAX_RESULT_BYTES and payload[key]:
            payload[key] = payload[key][: len(payload[key]) // 2]
            payload["truncated"] = True


async def _get_researcher(
    db: AsyncSession, context: IntegrationContext, arguments: dict[str, Any]
) -> ToolResult:
    key = _researcher_key(arguments["researcher_id"])
    if not key:
        raise ToolArgumentError("researcher_id must not be blank")
    scanned, truncated = await _scan_authored_papers(db, context)
    papers = [paper for paper in scanned if any(k == key for k, _ in paper.authors)]
    if not papers:
        # Unknown id and an author who only wrote for another project are the
        # same answer: nothing here says which.
        return ToolResult(
            content=[{"error": "researcher_not_found"}], is_error=True, source_refs=[]
        )
    name = next(n for k, n in papers[0].authors if k == key)  # newest spelling
    coauthors: dict[str, list[Any]] = {}  # researcher_id -> [name, shared papers]
    for paper in papers:
        for other, other_name in paper.authors:
            if other != key:
                coauthors.setdefault(other, [other_name, 0])[1] += 1
    ranked = sorted(coauthors.items(), key=lambda item: (-item[1][1], item[0]))
    papers.sort(
        key=lambda paper: (paper.published or "", paper.created_at, paper.document_id),
        reverse=True,
    )
    payload: dict[str, Any] = {
        "researcher": {"researcher_id": key, "name": name},
        "papers": [
            {
                "document_id": paper.document_id,
                "title": paper.title,
                **({"arxiv_id": paper.arxiv_id} if paper.arxiv_id else {}),
                **({"published": paper.published} if paper.published else {}),
            }
            for paper in papers[:MAX_RESEARCHER_PAPERS]
        ],
        "coauthors": [
            {"researcher_id": other, "name": other_name, "paper_count": count}
            for other, (other_name, count) in ranked[:MAX_COAUTHORS]
        ],
    }
    if truncated or len(papers) > MAX_RESEARCHER_PAPERS or len(ranked) > MAX_COAUTHORS:
        payload["truncated"] = True
    _fit_to_result_cap(payload)
    result = _finish(payload)
    if result.is_error:
        return result
    return ToolResult(
        content=result.content,
        is_error=False,
        source_refs=[{"document_id": p["document_id"]} for p in payload["papers"]],
    )


async def _args_only(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tool that takes no identity; exception text never leaves."""
    args = dict(arguments)
    try:
        if name == "search_arxiv":
            args["max_results"] = _clamp(
                args.get("max_results", 5), 1, MAX_EXTERNAL_RESULTS, 5
            )
            return await asyncio.wait_for(
                _tool_search_arxiv(args), timeout=_search_arxiv_budget_seconds()
            )
        if name == "search_external_database":
            args["max_results"] = _clamp(
                args.get("max_results", 10), 1, MAX_EXTERNAL_RESULTS, 10
            )
            return await _tool_search_external_database(args)
        return await _tool_list_external_databases(args)
    except asyncio.TimeoutError:
        return {"error": "upstream_timeout"}
    except Exception:
        logger.warning("%s failed", name, exc_info=True)
        return {"error": "upstream_unavailable"}


def _source_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if "error" in payload:
        return []
    refs: list[dict[str, Any]] = []
    for ref in payload.get("source_refs") or []:
        if isinstance(ref, dict):
            refs.append(dict(ref))
    for paper in payload.get("papers") or []:
        if paper.get("id"):
            refs.append({"arxiv_id": str(paper["id"])})
    for hit in payload.get("results") or []:
        if hit.get("id"):
            # Per-result "source" is the connector that produced it.
            refs.append(
                {
                    "external_id": str(hit["id"]),
                    "connector": hit.get("source") or payload.get("connector"),
                }
            )
    for doc in payload.get("documents") or []:
        if doc.get("id"):
            refs.append({"document_id": str(doc["id"])})
    for chunk in payload.get("chunks") or []:
        # do_kb leaves document_id None when a chunk cannot be resolved to a
        # Document row; an unresolved chunk is not an observed identity.
        if chunk.get("document_id"):
            refs.append({"document_id": str(chunk["document_id"])})
    draft = payload.get("draft")
    if isinstance(draft, dict) and draft.get("id"):
        refs.append({"draft_id": str(draft["id"])})
    return refs[:MAX_RESULTS]


async def invoke_read(
    db: AsyncSession, context: IntegrationContext, invocation: ToolInvocation
) -> ToolResult:
    arguments = _validate_arguments(invocation)
    user = await db.get(User, context.user_id)
    if user is None or user.organization_id != context.organization_id:
        raise IntegrationAccessDenied()
    name = invocation.tool_name
    if name in _ARGS_ONLY_TOOLS:
        return _finish(await _args_only(name, arguments))
    if name == "get_arxiv_paper_content":
        return await _arxiv_paper_content(arguments)
    project_id = str(context.project_id)
    # Recheck project ownership and live ancestors on every call.
    if await _verify_project_ownership(project_id, db, user) is None:
        return ToolResult(
            content=[{"error": "Project not found or access denied"}],
            is_error=True,
            source_refs=[],
        )

    if name == "get_document_content":
        return await _document_content(db, context, arguments)
    if name == "retrieve_passages":
        return _finish(await _retrieve_passages(db, context, arguments))
    if name == "search_documents":
        payload = await _search_project_documents(
            db,
            context,
            str(arguments["query"]),
            _clamp(arguments.get("max_results", 10), 1, MAX_RESULTS, 10),
        )
    elif name == "list_project_documents":
        payload = await _tool_list_project_documents(
            {
                "project_id": project_id,
                "limit": _clamp(
                    arguments.get("limit", MAX_RESULTS), 1, MAX_RESULTS, MAX_RESULTS
                ),
                "offset": _clamp(arguments.get("offset", 0), 0, 2**31 - 1, 0),
            },
            db,
            user,
        )
    elif name == "do_kb_retrieve":
        _, kb_schema, _ = _registry_schema(name)
        top_k_default = int(kb_schema["properties"]["top_k"].get("default", 8))
        document_ids = await _project_document_ids(
            db, context, arguments.get("document_ids")
        )
        if document_ids is None:
            return _unavailable()
        payload = await _tool_do_kb_retrieve(
            {
                "query": str(arguments["query"]),
                # Default comes from the advertised schema (registry default).
                "top_k": _clamp(
                    arguments.get("top_k", top_k_default),
                    1,
                    MAX_DOCUMENT_IDS,
                    top_k_default,
                ),
                "document_ids": document_ids,
                "project_id": project_id,
            },
            db,
            user,
        )
        # Provider metadata carries storage item names; only excerpts and
        # observed identities leave the gateway.
        payload["chunks"] = [
            {key: chunk.get(key) for key in ("document_id", "title", "text", "score")}
            for chunk in payload.get("chunks") or []
        ]
    elif name == "find_researchers":
        return await _find_researchers(db, context, arguments)
    elif name == "get_researcher":
        return await _get_researcher(db, context, arguments)
    else:
        payload = await _tool_get_current_draft(
            {
                "project_id": project_id,
                "include_content": bool(arguments.get("include_content", False)),
            },
            db,
            user,
        )
        draft = payload.get("draft")
        if isinstance(draft, dict) and isinstance(draft.get("content"), str):
            draft["content_truncated"] = len(draft["content"]) > MAX_DRAFT_CONTENT_CHARS
            draft["content"] = draft["content"][:MAX_DRAFT_CONTENT_CHARS]
    return _finish(payload)


def _finish(payload: dict[str, Any]) -> ToolResult:
    # Measure as FastAPI emits it (UTF-8, no ASCII escaping).
    wire = json.dumps(payload, default=str, ensure_ascii=False).encode()
    if len(wire) > MAX_RESULT_BYTES:
        return ToolResult(
            content=[{"error": "result_too_large"}], is_error=True, source_refs=[]
        )
    # do_kb reports scoped failures as a "reason" with no chunks rather than
    # an "error" key; both are failures to the caller.
    is_error = "error" in payload or "reason" in payload
    return ToolResult(
        content=[payload],
        is_error=is_error,
        source_refs=[] if is_error else _source_refs(payload),
    )
