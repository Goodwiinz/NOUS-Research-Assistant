"""Read-only NOUS tool gateway for external harnesses.

Identity (actor, organization, project) comes from the resolved
``IntegrationContext``; model-supplied arguments never choose it. The one
exception is the ``project_id`` selector of a workspace grant, which picks one
of the workspace's live projects and is checked against ``authorized_scope``
on every call. The allowlist is deliberately narrow: the absence of a
``DESTRUCTIVE`` tag is not evidence that a registry tool is safe to expose
outside the agent graph. Each tool names the scope a grant must hold to call
it (``TOOL_SCOPES``): ``tools:read`` for the registry tools, and the scope in
``LOCAL_TOOLS`` for the tools that have no agent-registry twin.
"""

from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Sequence, cast
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, desc, func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from starlette.concurrency import run_in_threadpool

from src.models.collection import Collection, CollectionDocument
from src.models.document import Document
from src.models.user import User
from src.models.workspace import Workspace
from src.schemas.artifact import ArtifactError
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
from src.services.artifacts.service import list_project_artifacts
from src.services.integrations import arxiv_fulltext
from src.services.integrations.arxiv_fulltext import MAX_PAGE_CHARS
from src.services.integrations.context import (
    IntegrationAccessDenied,
    authorized_scope,
    authorized_scope_filter,
    workspace_in_org,
)
from src.services.search.fulltext_search_service import fulltext_search_service

logger = logging.getLogger(__name__)

# Tools with an agent-registry twin; the catalog schema derives from it.
READ_TOOL_NAMES: tuple[str, ...] = (
    "search_documents",
    "list_project_documents",
    "do_kb_retrieve",
    "get_current_draft",
    "list_project_artifacts",
    "search_arxiv",
    "search_external_database",
    "list_external_databases",
)
# Args-only tools: no project scoping, dispatched before the ownership check.
_ARGS_ONLY_TOOLS = frozenset(
    {"search_arxiv", "search_external_database", "list_external_databases"}
)
# Tools that act on no single project. A workspace grant gives them no project
# selector: there is nothing for it to select. list_library lists the grant's
# whole scope.
_PROJECTLESS_TOOLS = _ARGS_ONLY_TOOLS | {"get_arxiv_paper_content", "list_library"}
IDENTITY_ARGUMENTS = frozenset(
    {"project_id", "user_id", "organization_id", "thread_id", "run_id", "grant_id"}
)
MAX_RESULTS = 50
# Matches the 2**31 - 1 clamp on the registry tools' offset: a larger value
# would only reach the database as an out-of-range bind.
MAX_OFFSET = 2**31 - 1
MAX_DOCUMENT_IDS = 20
# Every returned artifact row must carry a source ref, and _source_refs keeps
# at most MAX_RESULTS, so the artifact page can never be larger.
MAX_ARTIFACTS = MAX_RESULTS
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
MAX_FOLDER_DESCRIPTION_CHARS = 500
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
# A workspace grant spans several projects, so the caller names the one to act
# on. Project grants never see it: their project is the grant's own.
_PROJECT_SELECTOR: dict[str, Any] = {
    "type": "string",
    "format": "uuid",
    "description": "Required for workspace grants: which project to act on.",
}

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
        "most papers first, at most 25 (limit). Documents without author "
        "metadata are invisible, and there are no affiliations or career "
        "history. Only the 2,000 newest documents are scanned. truncated=true "
        "means the list is incomplete: it was cut by limit, or older documents "
        "were not scanned. Pass a researcher_id to get_researcher.",
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
        "affiliations or career history. Only the 2,000 newest documents are "
        "scanned. researcher_not_found means no scanned document of this "
        "project lists that author; with truncated=true older documents were "
        "not scanned, so the author may exist there.",
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
    "list_library": (
        "List the folders (NOUS projects) this connection may use, with document "
        "counts. A page holds at most limit folders, fewer when their text is "
        "long. Pass next_offset as offset to read the next page; it is null on "
        "the last one.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_RESULTS,
                    "default": MAX_RESULTS,
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_OFFSET,
                    "default": 0,
                },
            },
        },
        "library:read",
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


class _ListProjectArtifactsArgs(BaseModel):
    limit: int = Field(MAX_ARTIFACTS, ge=1, le=MAX_ARTIFACTS)


# Gateway-native tools: not in the agent TOOL_REGISTRY, so they carry their own schema.
_GATEWAY_TOOLS: dict[str, tuple[str, type[BaseModel]]] = {
    "list_project_artifacts": (
        "List the current version of each artifact published to this project "
        "(newest first), with sha256, size and the chat it came from, if any.",
        _ListProjectArtifactsArgs,
    )
}


class ToolArgumentError(ValueError):
    """Invocation shape rejected before any adapter runs (HTTP 422)."""


def _registry_schema(
    name: str, *, workspace_bound: bool = False
) -> tuple[str, dict[str, Any], type[BaseModel]]:
    if name in _GATEWAY_TOOLS:
        description, model = _GATEWAY_TOOLS[name]
    else:
        from src.services.agent.tools import TOOL_REGISTRY

        descriptor = TOOL_REGISTRY.descriptor(name)
        assert descriptor is not None, name
        raw_schema = descriptor.tool.tool_call_schema
        # tool_call_schema is typed as a v2/v1 model class or dict; the registry
        # only holds decorated @tool wrappers, which always yield a v2 class.
        if not (isinstance(raw_schema, type) and issubclass(raw_schema, BaseModel)):
            raise TypeError(f"{name} has no pydantic v2 call schema")
        model = cast(type[BaseModel], raw_schema)
        description = descriptor.tool.description
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
    if workspace_bound and name not in _PROJECTLESS_TOOLS:
        # Optional in the schema: omitting it is a structured error, not a 422.
        properties["project_id"] = dict(_PROJECT_SELECTOR)
    schema["properties"] = properties
    schema["required"] = [
        key for key in schema.get("required", []) if key in properties
    ] + _EXTRA_REQUIRED.get(name, [])
    schema["additionalProperties"] = False
    return description, schema, model


def _local_schema(
    name: str, *, workspace_bound: bool = False
) -> tuple[str, dict[str, Any]]:
    """Description and advertised schema of a LOCAL_TOOLS entry (a private copy)."""
    description, schema, _scope = LOCAL_TOOLS[name]
    schema = deepcopy(schema)
    if workspace_bound and name not in _PROJECTLESS_TOOLS:
        schema["properties"]["project_id"] = dict(_PROJECT_SELECTOR)
    return description, schema


def list_read_tools(
    scopes: Iterable[str] | None = None, *, workspace_bound: bool = False
) -> list[ToolDescriptorDTO]:
    """The registry tools plus the local ones.

    Given a grant's ``scopes``, only the tools those scopes may call are
    listed; without them, every tool. A workspace grant also sees the optional
    project selector on the tools that act on one project.
    """
    held = None if scopes is None else frozenset(scopes)
    catalog: list[ToolDescriptorDTO] = []
    for name in (*READ_TOOL_NAMES, *LOCAL_TOOLS):
        if held is not None and TOOL_SCOPES[name] not in held:
            continue
        if name in LOCAL_TOOLS:
            description, schema = _local_schema(name, workspace_bound=workspace_bound)
        else:
            description, schema, _model = _registry_schema(
                name, workspace_bound=workspace_bound
            )
        catalog.append(
            ToolDescriptorDTO(name=name, description=description, input_schema=schema)
        )
    return catalog


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


def _validate_arguments(
    context: IntegrationContext, invocation: ToolInvocation
) -> dict[str, Any]:
    from pydantic import ValidationError

    name = invocation.tool_name
    if name not in TOOL_SCOPES:
        raise ToolArgumentError("tool is not available to integrations")
    arguments = invocation.arguments
    workspace_bound = context.workspace_id is not None
    if name in LOCAL_TOOLS:
        _, schema = _local_schema(name, workspace_bound=workspace_bound)
        if set(arguments) - set(schema["properties"]):
            raise ToolArgumentError("unknown arguments")
        if set(schema["required"]) - set(arguments):
            raise ToolArgumentError("missing required arguments")
        # The selector, when advertised, is a plain string here;
        # _target_project checks it is a UUID inside the grant's scope.
        _check_local_arguments(schema, arguments)
        return arguments
    _, schema, model = _registry_schema(name, workspace_bound=workspace_bound)
    # Identity keys were stripped from the advertised schema, so a caller
    # supplying user_id/organization_id/... is rejected here as an unknown
    # argument, and so is project_id on a project grant. Only a workspace
    # grant advertises (and so accepts) the project_id selector, and only on
    # the tools that act on one project.
    allowed = set(schema["properties"])
    if set(arguments) - allowed:
        raise ToolArgumentError("unknown arguments")
    if set(schema["required"]) - set(arguments):
        raise ToolArgumentError("missing required arguments")
    try:
        # strict: no "false"->bool, float->int or dict->str coercion. The
        # selector is not a tool argument; _target_project validates it.
        model.model_validate(
            {key: value for key, value in arguments.items() if key != "project_id"},
            strict=True,
        )
    except ValidationError as error:
        raise ToolArgumentError("invalid argument types") from error
    return arguments


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError, OverflowError):
        return default


def _live_project_documents(project_id: UUID, organization_id: UUID) -> Any:
    """Documents in one project with every ancestor still live.

    Joined in the same statement so a soft-delete landing between the
    ownership check and this fetch cannot return revoked rows.
    """
    return (
        select(Document)
        .join(CollectionDocument, CollectionDocument.document_id == Document.id)
        .join(Collection, Collection.id == CollectionDocument.collection_id)
        .join(Workspace, Workspace.id == Collection.workspace_id)
        .where(
            Collection.id == project_id,
            Collection.is_deleted == False,  # noqa: E712
            Workspace.is_deleted == False,  # noqa: E712
            # A legacy workspace (organization_id NULL) is its owner's.
            workspace_in_org(organization_id),
            CollectionDocument.is_deleted == False,  # noqa: E712
            Document.organization_id == organization_id,
            Document.is_deleted == False,  # noqa: E712
        )
    )


async def _search_project_documents(
    db: AsyncSession, project_id: UUID, organization_id: UUID, query: str, limit: int
) -> dict[str, Any]:
    """Title/filename search joined to one project, never org-wide."""
    from src.services.agent._pii_redact import redact_pii

    pattern = f"%{_escape_like(query)}%"
    rows = await db.execute(
        _live_project_documents(project_id, organization_id)
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
    db: AsyncSession, project_id: UUID, organization_id: UUID, raw_ids: Any
) -> list[str] | None:
    """Require 1–20 UUIDs; None when any id is outside the given project."""
    if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= MAX_DOCUMENT_IDS:
        raise ToolArgumentError("document_ids must contain 1-20 UUIDs")
    try:
        requested = {UUID(str(value)) for value in raw_ids}
    except ValueError as error:
        raise ToolArgumentError("document_ids must be UUIDs") from error
    rows = await db.execute(
        _live_project_documents(project_id, organization_id)
        .with_only_columns(Document.id)
        .where(Document.id.in_(requested))
    )
    members = {UUID(str(value)) for value in rows.scalars().all()}
    if members != requested:
        return None
    # Canonical spelling so later equality checks match DB-emitted ids.
    return [str(UUID(str(value))) for value in raw_ids]


async def _collection_document_ids(
    db: AsyncSession, project_id: UUID, organization_id: UUID
) -> list[str]:
    """Every live document in one Collection."""
    # ponytail: unbounded id list -> one bind param per document; add a cap
    # or a join-based filter in the search service if collections grow large.
    rows = await db.execute(
        _live_project_documents(project_id, organization_id).with_only_columns(
            Document.id
        )
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
    db: AsyncSession, project_id: UUID, organization_id: UUID, arguments: dict[str, Any]
) -> ToolResult:
    try:
        document_id = UUID(str(arguments["document_id"]))
    except ValueError as error:
        raise ToolArgumentError("document_id must be a UUID") from error
    mode = str(arguments.get("mode", "summary"))
    # The preview is computed in SQL so summary mode never touches the
    # deferred content_text (an ORM fallback would lazy-load -> MissingGreenlet).
    statement = (
        _live_project_documents(project_id, organization_id)
        .where(Document.id == document_id)
        .add_columns(func.substr(Document.content_text, 1, 500))
    )
    if mode != "full":
        statement = statement.options(defer(Document.content_text))
    row = (await db.execute(statement)).first()
    if row is None:
        return _unavailable()
    document, preview = row
    text = (
        document.content_text or ""
        if mode == "full"
        else document.content_summary or preview or ""
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
    db: AsyncSession,
    context: IntegrationContext,
    project_id: UUID,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    from src.models.search_schemas import SearchFilter, SearchQuery, SearchType
    from src.services.agent._pii_redact import redact_pii

    query = str(arguments["query"])
    top_k = _clamp(arguments.get("top_k", 8), 1, MAX_DOCUMENT_IDS, 8)
    if "document_ids" in arguments:
        document_ids = await _project_document_ids(
            db, project_id, context.organization_id, arguments["document_ids"]
        )
        if document_ids is None:
            return _unavailable().content[0]
    else:
        document_ids = await _collection_document_ids(
            db, project_id, context.organization_id
        )
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
    # Re-check liveness after the search: an ancestor soft-deleted while the
    # threadpool query ran must not leak a hit. The FTS service filters by
    # ids + org only, so the Collection/Workspace check is ours.
    allowed = set(document_ids) & set(
        await _collection_document_ids(db, project_id, context.organization_id)
    )
    chunks = []
    for hit in response.results:
        if hit.document_id not in allowed:
            continue
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
    budget = _search_arxiv_budget_seconds()
    try:
        # get_page bounds the fetch itself (so a stale entry can still be
        # served when a refresh times out); this is only the outer guard.
        page = await asyncio.wait_for(
            arxiv_fulltext.get_page(
                arxiv_id,
                offset=offset,
                limit=limit,
                redis=_arxiv_cache(),
                budget=budget,
            ),
            timeout=budget + 5,
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
    """researcher_id: casefolded, whitespace-collapsed, at most 200 characters.

    Cutting at the cap can leave a trailing space; stripping it keeps the key
    idempotent, so an id handed out by find_researchers resolves again here.
    """
    return " ".join(str(name).split()).casefold()[:_NAME_CHARS].strip()


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
            authors.setdefault(key, " ".join(name.split())[:_NAME_CHARS].strip())
    return tuple(authors.items())


def _published(metadata: dict[str, Any]) -> str | None:
    # arXiv ingest stores publication_date; the change tracker stores published.
    for field in ("publication_date", "published"):
        value = metadata.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:40]
    return None


async def _scan_authored_papers(
    db: AsyncSession, project_id: UUID, organization_id: UUID
) -> tuple[list[_AuthoredPaper], bool]:
    """One project's newest live documents that carry an author list.

    Returns ``(papers, truncated)``. This is the only data source of the
    researcher tools and it is project-scoped by construction:
    ``_live_project_documents`` joins the selected project and every ancestor.
    """
    # ponytail: O(documents x authors) in Python over at most
    # MAX_SCANNED_DOCUMENTS (2000) newest documents, in one narrow-column
    # query. Fine up to a few thousand documents; older ones are invisible and
    # reported as truncated. Move the aggregation into SQL or a per-project
    # author index if projects outgrow that.
    rows = (
        await db.execute(
            _live_project_documents(project_id, organization_id)
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
                title=str(title or ""),
                arxiv_id=str(arxiv).strip()[:64] if arxiv else None,
                published=_published(meta),
                created_at=created_at.isoformat() if created_at else "",
                authors=authors,
            )
        )
    return papers, len(rows) > MAX_SCANNED_DOCUMENTS


async def _find_researchers(
    db: AsyncSession, project_id: UUID, organization_id: UUID, arguments: dict[str, Any]
) -> ToolResult:
    needle = _researcher_key(arguments["query"])
    if not needle:
        raise ToolArgumentError("query must not be blank")
    limit = _clamp(arguments.get("limit", 10), 1, MAX_RESEARCHERS, 10)
    papers, truncated = await _scan_authored_papers(db, project_id, organization_id)
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
    if truncated or len(ranked) > limit:  # cut by the scan window or by limit
        payload["truncated"] = True
    # A researcher is not a source document: there is nothing to cite.
    return _finish(payload)


def _paper_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"document_id": paper["document_id"]} for paper in payload["papers"]]


def _fit_to_result_cap(payload: dict[str, Any]) -> None:
    """Halve co-authors, then papers, until the whole response fits the cap.

    Measured as the ToolResult envelope (content plus source_refs), so the
    refs derived from the papers count against MAX_RESULT_BYTES too.
    """

    def wire() -> int:
        envelope = {
            "content": [payload],
            "is_error": False,
            "source_refs": _paper_refs(payload),
        }
        return len(json.dumps(envelope, default=str, ensure_ascii=False).encode())

    for key in ("coauthors", "papers"):
        while wire() > MAX_RESULT_BYTES and payload[key]:
            payload[key] = payload[key][: len(payload[key]) // 2]
            payload["truncated"] = True


async def _get_researcher(
    db: AsyncSession, project_id: UUID, organization_id: UUID, arguments: dict[str, Any]
) -> ToolResult:
    key = _researcher_key(arguments["researcher_id"])
    if not key:
        raise ToolArgumentError("researcher_id must not be blank")
    scanned, truncated = await _scan_authored_papers(db, project_id, organization_id)
    papers = [paper for paper in scanned if any(k == key for k, _ in paper.authors)]
    if not papers:
        # Unknown id and an author who only wrote for another project are the
        # same answer: nothing here says which. When the scan window hid older
        # documents the caller must be able to tell "incomplete" from "unknown".
        missing: dict[str, Any] = {"error": "researcher_not_found"}
        if truncated:
            missing["truncated"] = True
        return ToolResult(content=[missing], is_error=True, source_refs=[])
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
    from src.services.agent._pii_redact import redact_pii

    payload: dict[str, Any] = {
        "researcher": {"researcher_id": key, "name": name},
        "papers": [
            {
                "document_id": paper.document_id,
                "title": redact_pii(paper.title)[:500],
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
    # _finish's generic _source_refs also sees a "papers" key (arXiv-search
    # shaped, keyed by "id"); it yields nothing here and is overridden on purpose.
    return ToolResult(
        content=result.content,
        is_error=False,
        source_refs=_paper_refs(payload),
    )


async def _args_only(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tool that takes no identity; upstream detail never leaves.

    Agent tools report failures as payloads (``error`` text naming env vars,
    connector exceptions, ...) as well as by raising; both are normalised.
    """
    args = dict(arguments)
    try:
        if name == "search_arxiv":
            args["max_results"] = _clamp(
                args.get("max_results", 5), 1, MAX_EXTERNAL_RESULTS, 5
            )
            payload = await asyncio.wait_for(
                _tool_search_arxiv(args), timeout=_search_arxiv_budget_seconds()
            )
        elif name == "search_external_database":
            args["max_results"] = _clamp(
                args.get("max_results", 10), 1, MAX_EXTERNAL_RESULTS, 10
            )
            payload = await _tool_search_external_database(args)
        else:
            payload = await _tool_list_external_databases(args)
    except asyncio.TimeoutError:
        return {"error": "upstream_timeout"}
    except Exception:
        logger.warning("%s failed", name, exc_info=True)
        return {"error": "upstream_unavailable"}
    if payload.get("error") or payload.get("is_error"):
        logger.warning("%s returned an error payload: %s", name, payload.get("error"))
        return {"error": "upstream_unavailable"}
    return payload


async def _project_artifacts(
    db: AsyncSession, context: IntegrationContext, project_id: UUID, limit: int
) -> dict[str, Any]:
    """Current versions in one project; identity never comes from args."""
    from src.services.agent._pii_redact import redact_pii

    try:
        rows = await list_project_artifacts(
            db,
            user_id=context.user_id,
            organization_id=context.organization_id,
            project_id=project_id,
            limit=limit,
        )
    except ArtifactError:
        return {"error": "Project not found or access denied"}
    return {
        "artifacts": [
            {
                "artifact_id": str(row.artifact_id),
                "version_id": str(row.current_version.version_id),
                "title": redact_pii(row.title),
                "kind": row.kind,
                "byte_size": row.current_version.byte_size,
                "sha256": row.current_version.sha256,
                "created_at": row.current_version.created_at.isoformat(),
                "thread_id": str(row.thread_id) if row.thread_id else None,
            }
            for row in rows
        ],
        "total": len(rows),
    }


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
    for artifact in payload.get("artifacts") or []:
        refs.append(
            {
                "artifact_id": str(artifact["artifact_id"]),
                "version_id": str(artifact["version_id"]),
            }
        )
    draft = payload.get("draft")
    if isinstance(draft, dict) and draft.get("id"):
        refs.append({"draft_id": str(draft["id"])})
    return refs[:MAX_RESULTS]


async def _target_project(
    db: AsyncSession, context: IntegrationContext, arguments: dict[str, Any]
) -> UUID | None:
    """The Collection a call acts on.

    Project grants: always the bound Collection (a client project_id was
    already refused as an unknown argument). Workspace grants: ``project_id``
    is a *selector* and must be one of the workspace's live Collections as
    ``authorized_scope`` computes them on this call. Returns None when a
    workspace grant omits the selector. The selector is a UUID, never a name:
    the agent helpers resolve any other string as a project NAME.
    """
    if context.project_id is not None:
        return context.project_id
    allowed = await authorized_scope(db, context)
    raw = arguments.get("project_id")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ToolArgumentError("project_id must be a UUID")
    try:
        chosen = UUID(raw)
    except ValueError as error:
        raise ToolArgumentError("project_id must be a UUID") from error
    if chosen not in allowed:
        raise IntegrationAccessDenied()
    return chosen


async def _library_folders(
    db: AsyncSession,
    scope: ColumnElement[bool],
    organization_id: UUID,
    offset: int,
    limit: int,
) -> Sequence[Row[Any]]:
    """One page of live Collections in ``scope``, with live document counts.

    ``scope`` is a condition (``authorized_scope_filter``), never a list of ids:
    a workspace can hold more Collections than one statement may bind. The count
    applies the filters ``list_project_documents`` applies to its ``total``, so
    the two never disagree. The Workspace join re-checks that ancestor in this
    statement, because the scope's access check ran in another one.
    """
    live_documents = (
        select(func.count(CollectionDocument.id))
        .join(Document, Document.id == CollectionDocument.document_id)
        .where(
            CollectionDocument.collection_id == Collection.id,
            CollectionDocument.is_deleted.is_(False),
            Document.organization_id == organization_id,
            Document.is_deleted.is_(False),
        )
        .correlate(Collection)
        .scalar_subquery()
    )
    rows = await db.execute(
        select(Collection.id, Collection.name, Collection.description, live_documents)
        .join(Workspace, Workspace.id == Collection.workspace_id)
        .where(
            scope,
            Collection.is_deleted.is_(False),
            Workspace.is_deleted.is_(False),
        )
        .order_by(Collection.name, Collection.id)
        .offset(offset)
        .limit(limit)
    )
    return rows.all()


async def _list_library(
    db: AsyncSession, context: IntegrationContext, arguments: dict[str, Any]
) -> dict[str, Any]:
    """The folders (projects) the grant may use, with live document counts.

    A project grant sees its own project, a workspace grant the live projects
    of its workspace; the scope is resolved afresh on every call.
    """
    # The shape was checked by _validate_arguments; this only applies defaults.
    limit = _clamp(arguments.get("limit", MAX_RESULTS), 1, MAX_RESULTS, MAX_RESULTS)
    offset = _clamp(arguments.get("offset", 0), 0, MAX_OFFSET, 0)
    scope = await authorized_scope_filter(db, context)
    found = await _library_folders(
        db, scope, context.organization_id, offset, limit + 1
    )
    folders = [
        {
            "id": str(folder_id),
            "name": name,
            "description": (description or "")[:MAX_FOLDER_DESCRIPTION_CHARS],
            "document_count": int(document_count),
        }
        for folder_id, name, description, document_count in found[:limit]
    ]
    # Rows are bounded by characters, the cap by UTF-8 bytes: 50 long CJK or
    # emoji descriptions pass it. End the page at the longest prefix that fits
    # and let next_offset resume there, rather than failing the whole page.
    # ponytail: re-serialises per dropped row (at most 49); bisect if limit grows.
    while (
        len(folders) > 1
        and _wire_size(_library_page(folders, offset, len(found))) > MAX_RESULT_BYTES
    ):
        folders.pop()
    return _library_page(folders, offset, len(found))


def _library_page(
    folders: list[dict[str, Any]], offset: int, fetched: int
) -> dict[str, Any]:
    """A page of ``folders``; ``fetched`` rows were read, so more follow when
    fewer were kept."""
    return {
        "folders": folders,
        "offset": offset,
        "next_offset": offset + len(folders) if len(folders) < fetched else None,
    }


async def invoke_read(
    db: AsyncSession, context: IntegrationContext, invocation: ToolInvocation
) -> ToolResult:
    required_scope = TOOL_SCOPES.get(invocation.tool_name)
    if required_scope is not None and required_scope not in context.scopes:
        # The router only demands tools:read; a grant can hold that and still
        # lack the scope of this tool (list_library needs library:read). An
        # unknown tool falls through to _validate_arguments, which rejects it.
        raise IntegrationAccessDenied()
    arguments = _validate_arguments(context, invocation)
    user = await db.get(User, context.user_id)
    if user is None or user.organization_id != context.organization_id:
        raise IntegrationAccessDenied()
    name = invocation.tool_name
    if name in _PROJECTLESS_TOOLS:
        # No single project is involved, so neither the selector nor the
        # per-project ownership check below applies. list_library resolves
        # authorized_scope_filter itself, which covers every folder it returns.
        if name == "list_library":
            return _finish(await _list_library(db, context, arguments))
        if name == "get_arxiv_paper_content":
            return await _arxiv_paper_content(arguments)
        return _finish(await _args_only(name, arguments))
    target = await _target_project(db, context, arguments)
    if target is None:
        # Never str() a missing project: the agent helper would treat "None"
        # as a project name and could resolve one outside the grant.
        return ToolResult(
            content=[{"error": "project_id_required"}], is_error=True, source_refs=[]
        )
    project_id = str(target)
    # Recheck project ownership and live ancestors on every call.
    if await _verify_project_ownership(project_id, db, user) is None:
        return ToolResult(
            content=[{"error": "Project not found or access denied"}],
            is_error=True,
            source_refs=[],
        )

    if name == "get_document_content":
        return await _document_content(db, target, context.organization_id, arguments)
    if name == "retrieve_passages":
        return _finish(await _retrieve_passages(db, context, target, arguments))
    if name == "search_documents":
        payload = await _search_project_documents(
            db,
            target,
            context.organization_id,
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
            db, target, context.organization_id, arguments.get("document_ids")
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
        return await _find_researchers(db, target, context.organization_id, arguments)
    elif name == "get_researcher":
        return await _get_researcher(db, target, context.organization_id, arguments)
    elif name == "list_project_artifacts":
        payload = await _project_artifacts(
            db,
            context,
            target,
            _clamp(
                arguments.get("limit", MAX_ARTIFACTS), 1, MAX_ARTIFACTS, MAX_ARTIFACTS
            ),
        )
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


def _wire_size(payload: dict[str, Any]) -> int:
    """Bytes of a payload as FastAPI emits it (UTF-8, no ASCII escaping)."""
    return len(json.dumps(payload, default=str, ensure_ascii=False).encode())


def _finish(payload: dict[str, Any]) -> ToolResult:
    if _wire_size(payload) > MAX_RESULT_BYTES:
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
