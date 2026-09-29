"""Read-only NOUS tool gateway for external harnesses.

Identity (actor, organization, project) comes from the resolved
``IntegrationContext``; model-supplied arguments never choose it. The
allowlist is deliberately narrow: the absence of a ``DESTRUCTIVE`` tag is not
evidence that a registry tool is safe to expose outside the agent graph.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.collection import CollectionDocument
from src.models.document import Document
from src.models.user import User
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_tools import (
    ToolDescriptorDTO,
    ToolInvocation,
    ToolResult,
)
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


class ToolArgumentError(ValueError):
    """Invocation shape rejected before any adapter runs (HTTP 422)."""


def _registry_schema(name: str) -> tuple[str, dict[str, Any]]:
    from src.services.agent.tools import TOOL_REGISTRY

    descriptor = TOOL_REGISTRY.descriptor(name)
    assert descriptor is not None, name
    schema = dict(descriptor.tool.tool_call_schema.model_json_schema())
    properties = {
        key: value
        for key, value in schema.get("properties", {}).items()
        if key not in IDENTITY_ARGUMENTS
    }
    schema["properties"] = properties
    schema["required"] = [
        key for key in schema.get("required", []) if key in properties
    ]
    schema["additionalProperties"] = False
    return descriptor.tool.description, schema


def list_read_tools() -> list[ToolDescriptorDTO]:
    return [
        ToolDescriptorDTO(name=name, description=desc_, input_schema=schema)
        for name in READ_TOOL_NAMES
        for desc_, schema in (_registry_schema(name),)
    ]


def _validate_arguments(invocation: ToolInvocation) -> dict[str, Any]:
    if invocation.tool_name not in READ_TOOL_NAMES:
        raise ToolArgumentError("tool is not available to integrations")
    arguments = invocation.arguments
    _, schema = _registry_schema(invocation.tool_name)
    # Identity keys were stripped from the advertised schema, so a caller
    # supplying project_id/user_id/... is rejected here as an unknown argument.
    allowed = set(schema["properties"])
    if set(arguments) - allowed:
        raise ToolArgumentError("unknown arguments")
    if set(schema["required"]) - set(arguments):
        raise ToolArgumentError("missing required arguments")
    return arguments


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


async def _search_project_documents(
    db: AsyncSession, context: IntegrationContext, query: str, limit: int
) -> dict[str, Any]:
    """Title/filename search joined to the grant's project, never org-wide."""
    from src.services.agent._pii_redact import redact_pii

    pattern = f"%{_escape_like(query)}%"
    rows = await db.execute(
        select(Document)
        .join(CollectionDocument, CollectionDocument.document_id == Document.id)
        .where(
            CollectionDocument.collection_id == context.project_id,
            CollectionDocument.is_deleted == False,  # noqa: E712
            Document.organization_id == context.organization_id,
            Document.is_deleted == False,  # noqa: E712
            (Document.title.ilike(pattern) | Document.filename.ilike(pattern)),
        )
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
        select(CollectionDocument.document_id)
        .join(Document, Document.id == CollectionDocument.document_id)
        .where(
            CollectionDocument.collection_id == context.project_id,
            CollectionDocument.document_id.in_(requested),
            CollectionDocument.is_deleted == False,  # noqa: E712
            Document.organization_id == context.organization_id,
            Document.is_deleted == False,  # noqa: E712
        )
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
        ref: dict[str, Any] = {"document_id": chunk.get("document_id")}
        if chunk.get("chunk_id"):
            ref["chunk_id"] = chunk["chunk_id"]
        refs.append(ref)
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
                "limit": _clamp(arguments.get("limit", MAX_RESULTS), 1, MAX_RESULTS, MAX_RESULTS),
                "offset": _clamp(arguments.get("offset", 0), 0, 2**31 - 1, 0),
            },
            db,
            user,
        )
    elif name == "do_kb_retrieve":
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
                "top_k": _clamp(arguments.get("top_k", 5), 1, MAX_DOCUMENT_IDS, 5),
                "document_ids": document_ids,
                "project_id": project_id,
            },
            db,
            user,
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

    if len(json.dumps(payload, default=str).encode()) > MAX_RESULT_BYTES:
        return ToolResult(
            content=[{"error": "result_too_large"}], is_error=True, source_refs=[]
        )
    return ToolResult(
        content=[payload],
        is_error="error" in payload,
        source_refs=_source_refs(payload),
    )
