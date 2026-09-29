"""Read-only NOUS tool gateway for external harnesses.

Identity (actor, organization, project) comes from the resolved
``IntegrationContext``; model-supplied arguments never choose it. The
allowlist is deliberately narrow: the absence of a ``DESTRUCTIVE`` tag is not
evidence that a registry tool is safe to expose outside the agent graph.
"""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

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
    _tool_list_project_documents,
)
from src.services.integrations.context import IntegrationAccessDenied

READ_TOOL_NAMES: tuple[str, ...] = (
    "search_documents",
    "list_project_documents",
    "do_kb_retrieve",
    "get_current_draft",
)
IDENTITY_ARGUMENTS = frozenset(
    {"project_id", "user_id", "organization_id", "thread_id", "run_id", "grant_id"}
)
MAX_RESULTS = 50
MAX_DOCUMENT_IDS = 20
MAX_RESULT_BYTES = 64 * 1024
# Keeps a content-bearing draft under MAX_RESULT_BYTES after JSON escaping.
MAX_DRAFT_CONTENT_CHARS = 32_000
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
}
_EXTRA_REQUIRED = {"do_kb_retrieve": ["document_ids"]}


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
    ]


def _validate_arguments(invocation: ToolInvocation) -> dict[str, Any]:
    from pydantic import ValidationError

    if invocation.tool_name not in READ_TOOL_NAMES:
        raise ToolArgumentError("tool is not available to integrations")
    arguments = invocation.arguments
    _, schema, model = _registry_schema(invocation.tool_name)
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
    return [str(value) for value in raw_ids]


def _source_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if "error" in payload:
        return []
    refs: list[dict[str, Any]] = []
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
    project_id = str(context.project_id)
    # Recheck project ownership and live ancestors on every call.
    if await _verify_project_ownership(project_id, db, user) is None:
        return ToolResult(
            content=[{"error": "Project not found or access denied"}],
            is_error=True,
            source_refs=[],
        )

    name = invocation.tool_name
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
            # Foreign and unknown ids look identical: no membership probing.
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
