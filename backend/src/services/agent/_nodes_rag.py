"""RAG node + DO KB / hybrid search helpers for the agent graph.

Extracted from ``graph.py`` so the orchestration module stays under the
800-line house rule. ``graph.py`` re-exports ``rag_node`` so the
LangGraph builder + existing tests keep working without rewriting any
import.

Two stages of retrieval policy live here:

* **Conversational fast-path** — ``_is_retrieval_query`` rejects greetings
  / acks / one-word messages so we don't burn an embedding round-trip
  + ~935 retrieval tokens on every "hi". Trace 019e168a documented the
  cost of leaving the fast-path off.
* **Primary read (DO KB) + fallback** — ``_try_primary_do_kb_read`` runs
  the DigitalOcean Knowledge Base when the org has one configured and
  the active project is known; ``_legacy_hybrid_search_fallback`` is
  the Postgres hybrid search + reranker chain used when DO KB is
  disabled or returns nothing.

This module reaches back into ``graph.py`` for the shared helper
``_extract_project_id_from_text`` via a lazy import to avoid a cycle.
Document resolution / project-scope filtering is delegated to
``src.services.do_kb.resolve.resolve_and_filter_chunks``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from src.services.agent.observability import track_node_execution
from src.services.agent.state import AgentState
from src.services.do_kb.postprocess import drop_low_relevance_chunks

logger = logging.getLogger(__name__)

# Counter name is registered in src.observability.metrics.initialize_default_metrics.
_DO_KB_READ_METRIC = "rag_do_kb_read_total"

# Keep the old private import path available to focused callers/tests.
_drop_low_relevance_chunks = drop_low_relevance_chunks


def _record_do_kb_read(outcome: str) -> None:
    """Emit an outcome-labeled counter for the DO KB primary-read path.

    Best-effort: metrics are optional infra, so a missing/unconfigured meter
    must never break retrieval. Reuses the existing increment_counter helper —
    no new observability module.
    """
    try:
        from src.observability.metrics import increment_counter

        increment_counter(_DO_KB_READ_METRIC, attributes={"outcome": outcome})
    except Exception:  # pragma: no cover - observability is optional
        pass


# ---------------------------------------------------------------------------
# Conversational fast-path heuristics
# ---------------------------------------------------------------------------

# Verbs / phrases that strongly imply the user wants document retrieval. Kept as
# a frozenset to make membership checks O(1) and the literal hard to mutate.
_RETRIEVAL_VERBS: frozenset[str] = frozenset(
    {
        "find",
        "search",
        "show",
        "list",
        "lookup",
        "summarize",
        "compare",
        "explore",
        "ingest",
        "what is",
        "tell me about",
    }
)

# Tool-name prefixes — if the user pasted/typed something like ``list_projects``
# or ``search_arxiv`` we should treat that as a retrieval-style query and skip
# the heuristic shortcut.
_TOOL_NAME_PREFIXES: tuple[str, ...] = (
    "search_",
    "list_",
    "summarize_",
    "compare_",
    "explore_",
    "extract_",
    "ingest_",
)

# Conversational patterns that never need document retrieval. Checked before
# retrieval-verb detection so "thanks" doesn't accidentally match "search".
_CONVERSATIONAL_PATTERNS: frozenset[str] = frozenset(
    {
        "what model",
        "which model",
        "how are you",
        "thank",
        "thanks",
        "ok",
        "okay",
        "yes",
        "no",
        "got it",
        "cool",
        "nice",
        "good",
        "great",
        "bye",
        "goodbye",
        "see you",
        "hello",
        "hey",
        "hi",
    }
)

# Length threshold (in whitespace-delimited tokens) below which a query is
# considered "short". Short queries without a retrieval verb skip RAG.
_SHORT_QUERY_TOKEN_LIMIT: int = 8

# Explicit chat attachments are a small, user-selected source set. Keep their
# input and prompt footprint bounded even if a stale checkpoint contains a
# larger list than the request schema permits.
_MAX_ATTACHMENT_IDS = 10
_MAX_ATTACHMENT_CONTEXT_CHARS = 3000
_MAX_ATTACHMENT_HISTORY_ROWS = 100
_ATTACHMENT_PROCESSING_STATUSES = frozenset({"pending", "processing", "retrying"})
_FOLLOW_UP_ATTACHMENT_UNAVAILABLE = {
    "status": "unavailable",
    "title": "previous thread attachment",
}


def _resolve_active_project_id(
    existing_project_id: Optional[str], extracted_pid: Optional[str]
) -> Optional[str]:
    """Resolve which project RAG retrieval should be scoped to.

    The active context (``state.current_project_id`` / ``page_context``) takes
    precedence over a UUID parsed from the user's message text, so a stale or
    quoted ``/projects/<uuid>`` URL cannot silently re-scope retrieval to
    another project (cross-project exposure). Text extraction only wins when no
    project context is active yet. (Audit #7.)
    """
    return existing_project_id or extracted_pid or None


def _workspace_accessible_by(user_uuid: UUID):
    """Owner-or-member predicate on ``Workspace``, for ``user_uuid``.

    Matches ``workspace_access.user_can_access_workspace`` semantics (owner or
    a live ``WorkspaceMember`` row) minus its ``is_public`` arm: these two
    callers gate a *retrieval scope*, so a public workspace the user never
    joined stays out (audit B8-T1). Expressed as SQL so neither caller has to
    load the workspace and its members.
    """
    from sqlalchemy import exists, or_, select

    from src.models.workspace import Workspace, WorkspaceMember

    return or_(
        Workspace.owner_id == user_uuid,
        exists(
            select(WorkspaceMember.id).where(
                WorkspaceMember.workspace_id == Workspace.id,
                WorkspaceMember.user_id == user_uuid,
                WorkspaceMember.is_deleted == False,  # noqa: E712
            )
        ),
    )


async def _user_owns_project(
    session, project_id: Optional[str], user_id: Optional[str]
) -> Optional[bool]:
    """Access to project (collection) ``project_id`` by ``user_id``.

    Access is owner OR live workspace membership, matching
    ``workspace_access.user_can_access_workspace`` (audit B8-T1) — an owner-only
    predicate silently dropped the project scope for a non-owner member and
    widened the DO KB read to the whole organization.

    The DO KB is org-scoped, and ``resolve_and_filter_chunks`` happily filters
    chunks by ANY project id it's handed. ``project_id`` here originates from
    client-supplied ``page_context`` / message text, so without this check a
    user could pass another in-org project's id and learn which org documents
    belong to it (membership inference). Mirrors the ownership guard the agent
    tools use (``_verify_project_ownership``); kept inline to avoid a
    services→api import. Takes the scalar ``user_id`` from the ids-only
    configurable (audit B8). A non-UUID id is treated as not-owned (drop the
    scope) — malformed input is a deterministic negative, not a transient
    failure, so it does not need the three-way return below.

    Returns ``True``/``False`` for an actual answer (checked, owned or not),
    and ``None`` when the check could not run at all (DB error/timeout) —
    callers MUST NOT treat ``None`` the same as ``False``. Collapsing them
    was audit M2: a transient DB blip during this check made the caller drop
    the project scope and fall through to an ORG-WIDE read, i.e. exactly the
    fail-*open* behavior the "fail closed" comment below was meant to
    prevent. ``None`` means "couldn't verify" — the caller must abort the
    scoped operation rather than silently widen it.
    """
    if not project_id or not user_id:
        return False
    try:
        from uuid import UUID as _UUID

        proj_uuid = _UUID(str(project_id).strip())
        user_uuid = _UUID(str(user_id).strip())
    except (ValueError, AttributeError, TypeError):
        return False
    try:
        from sqlalchemy import select

        from src.models.collection import Collection
        from src.models.workspace import Workspace

        stmt = (
            select(Collection.id)
            .join(Workspace, Collection.workspace_id == Workspace.id)
            .where(
                Collection.id == proj_uuid,
                Collection.is_deleted == False,  # noqa: E712
                _workspace_accessible_by(user_uuid),
            )
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None
    except Exception:  # noqa: BLE001 — unverifiable, NOT a verified negative
        logger.warning("project ownership check failed; unverifiable", exc_info=True)
        return None


async def _verify_extracted_project_id(
    extracted_pid: Optional[str], user_id: Optional[str]
) -> Optional[str]:
    """Ownership-gate a project id parsed from raw message text (audit M3).

    ``_extract_project_id_from_text``'s bare-UUID fallback matches ANY UUID
    in the message — a document id, a run id, another org member's project
    link — not just a genuine ``/projects/<uuid>`` paste. Promoting it
    unverified sets ``current_project_id`` and forces ``page_context.type =
    "project"``, which downstream consumers (``_with_injected_project_id``,
    the page-context prompt line telling the LLM "do NOT ask") treat as
    trusted context. Unlike the DO KB read's own scope check (M2), which can
    fall back to hybrid search on an unverifiable result, promoting into
    state has no softer fallback — so both an explicit "not owned" (False)
    and "couldn't check" (None) block promotion here.
    """
    if not extracted_pid or not user_id:
        return None
    from src.services.agent.tool_session import tool_session

    try:
        async with tool_session() as session:
            owned = await _user_owns_project(session, extracted_pid, user_id)
    except Exception:  # noqa: BLE001 — unverifiable, same as a failed check
        # `_user_owns_project` converts its OWN failures into None, but it
        # never runs if the session cannot be acquired (pool exhausted, DB
        # down). Without this, a blip raises out of `rag_node` — which has no
        # handler — and `_RETRY_POLICY` burns three attempts before failing
        # the turn, for a check whose only job is to withhold a promotion.
        # Unverifiable collapses to "do not promote", exactly like None.
        logger.warning(
            "extracted project id could not be verified; not promoting",
            exc_info=True,
        )
        return None
    return extracted_pid if owned is True else None


def is_conversational(content: str) -> bool:
    """Return ``True`` when *content* is a bare greeting / acknowledgement.

    Small talk such as ``"hi"``, ``"thanks"`` or ``"ok cool"`` needs neither
    document retrieval (``rag_node``) nor long-term memory recall
    (``memory_retrieval_node``). Both hot-path nodes share this predicate so
    they agree on what counts as conversational. Empty / whitespace-only
    input counts as conversational — there is nothing to retrieve or recall.
    """
    if not content or not content.strip():
        return True
    lowered = content.lower().strip()
    return any(
        re.search(r"\b" + re.escape(pattern) + r"\b", lowered)
        for pattern in _CONVERSATIONAL_PATTERNS
    )


def _is_retrieval_query(content: str) -> bool:
    """Return ``True`` when *content* looks like it needs document retrieval.

    Used by :func:`rag_node` to skip the (expensive) hybrid search whenever the
    latest user message is clearly conversational — e.g. ``"hi"``, ``"thanks!"``
    or a one-word ack. The heuristic is intentionally conservative: any
    long-ish message OR any message containing a retrieval verb / tool-name
    prefix is treated as retrieval to avoid degrading recall.

    Rules (a query is treated as NON-retrieval when ANY of these hold):
      0. lowercased content matches a ``_CONVERSATIONAL_PATTERNS`` entry

    Otherwise treated as retrieval when ANY of these hold:
      1. token count >= ``_SHORT_QUERY_TOKEN_LIMIT`` (8)
      2. lowercased content contains any ``_RETRIEVAL_VERBS`` substring
      3. lowercased content starts with any ``_TOOL_NAME_PREFIXES`` prefix
    """
    if is_conversational(content):
        return False

    lowered = content.lower().strip()
    tokens = content.split()

    if len(tokens) >= _SHORT_QUERY_TOKEN_LIMIT:
        return True

    for prefix in _TOOL_NAME_PREFIXES:
        if lowered.startswith(prefix):
            return True

    for verb in _RETRIEVAL_VERBS:
        if verb in lowered:
            return True

    return False


def _normalize_attachment_ids(values: Sequence[Any] | None) -> list[UUID]:
    """Parse and de-duplicate attachment ids while preserving request order."""
    normalized: list[UUID] = []
    seen: set[UUID] = set()
    for index, value in enumerate(values or ()):
        if index >= _MAX_ATTACHMENT_IDS:
            break
        try:
            document_id = value if isinstance(value, UUID) else UUID(str(value))
        except (AttributeError, TypeError, ValueError):
            continue
        if document_id not in seen:
            seen.add(document_id)
            normalized.append(document_id)
    return normalized


def _attachment_excerpt(content: str, query: str) -> str:
    """Return a bounded, query-focused excerpt from attached document text."""
    from src.services.agent._pii_redact import redact_pii

    safe_content = redact_pii(content or "")
    if len(safe_content) <= _MAX_ATTACHMENT_CONTEXT_CHARS:
        return safe_content

    # Uploaded text can be much larger than the prompt budget. Prefer a window
    # around a meaningful query term so a fact near the end of a document is
    # still available, while retaining a deterministic bounded fallback.
    terms = [
        term.lower()
        for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{3,}", query or "")
        if term.lower()
        not in {"only", "attached", "file", "document", "please", "what", "give"}
    ]
    lowered_content = safe_content.lower()
    positions = [lowered_content.find(term) for term in terms]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return safe_content[:_MAX_ATTACHMENT_CONTEXT_CHARS]
    start = max(0, min(positions) - 1000)
    end = start + _MAX_ATTACHMENT_CONTEXT_CHARS
    if end > len(safe_content):
        end = len(safe_content)
        start = max(0, end - _MAX_ATTACHMENT_CONTEXT_CHARS)
    return safe_content[start:end]


def _attachment_status(
    document_id: UUID, status: str, title: str | None = None
) -> dict:
    """Build a prompt-safe status record without carrying processing errors."""
    result: dict[str, str] = {"document_id": str(document_id), "status": status}
    if title:
        from src.services.agent._pii_redact import redact_pii

        result["title"] = redact_pii(title)[:500]
    return result


async def _load_attachment_contexts(
    *,
    attachment_ids: Sequence[Any] | None,
    thread_id: str | None,
    user_id: str,
    organization_id: str,
    query: str,
) -> tuple[list[dict], list[dict]]:
    """Load only authorized attached documents for the current graph turn.

    Explicit ids are authoritative: inaccessible or unready ids produce an
    unavailable status and never widen into organization/project search. When
    a follow-up omits ids, the latest live user message in the owned thread
    that has attachments supplies the source set. Every document read remains
    organization-scoped and soft-delete filtered.
    """
    explicit_ids = bool(attachment_ids)
    requested_ids = _normalize_attachment_ids(attachment_ids)
    if explicit_ids and not requested_ids:
        return [], []

    try:
        user_uuid = UUID(str(user_id))
        organization_uuid = UUID(str(organization_id))
    except (AttributeError, TypeError, ValueError):
        return [], [{"status": "unavailable"} for _ in requested_ids]

    from sqlalchemy import select

    from src.models.chat_message import ChatMessage, MessageRole
    from src.models.document import Document, ProcessingStatus
    from src.models.message_attachment import MessageAttachment
    from src.services.agent.tool_session import resolve_tool_user, tool_session
    from src.services.threads import workspace_access

    try:
        async with tool_session() as session:
            actor = await resolve_tool_user(
                session, str(user_uuid), str(organization_uuid)
            )
            if actor is None:
                if not explicit_ids and thread_id:
                    return [], [dict(_FOLLOW_UP_ATTACHMENT_UNAVAILABLE)]
                return [], [
                    _attachment_status(document_id, "unavailable")
                    for document_id in requested_ids
                ]

            if not explicit_ids:
                if not thread_id:
                    return [], []
                try:
                    thread_uuid = UUID(str(thread_id))
                except (AttributeError, TypeError, ValueError):
                    return [], [dict(_FOLLOW_UP_ATTACHMENT_UNAVAILABLE)]

                # A thread UUID alone is not authorization. Prove access to
                # the full workspace -> conversation -> thread chain before
                # reading any historical attachment rows. Keep this gate
                # lean: the attachment query below loads only the rows needed
                # for the latest attached user turn.
                thread = await workspace_access.get_thread(
                    session,
                    thread_uuid,
                    actor.id,
                    include_messages=False,
                )
                if thread is None:
                    return [], [dict(_FOLLOW_UP_ATTACHMENT_UNAVAILABLE)]

                # Select the latest attached user turn before checking whether
                # each referenced document is still readable. A soft-deleted
                # or otherwise inaccessible document must remain the selected
                # scope so a follow-up cannot fall back to an older file or
                # broad corpus search. The document read below applies the
                # organization and ownership filters and reports unavailable
                # ids without exposing their content.
                history_result = await session.execute(
                    select(MessageAttachment.message_id, MessageAttachment.document_id)
                    .join(
                        ChatMessage,
                        ChatMessage.id == MessageAttachment.message_id,
                    )
                    .where(
                        ChatMessage.thread_id == thread_uuid,
                        ChatMessage.role == MessageRole.USER,
                        ChatMessage.is_deleted == False,  # noqa: E712
                        ChatMessage.superseded_by_message_id.is_(None),
                    )
                    .order_by(
                        ChatMessage.created_at.desc(),
                        MessageAttachment.created_at.asc(),
                    )
                    .limit(_MAX_ATTACHMENT_HISTORY_ROWS)
                )
                history_rows = list(history_result.all())
                if not history_rows:
                    return [], []
                latest_message_id = history_rows[0][0]
                requested_ids = []
                seen: set[UUID] = set()
                for message_id, document_id in history_rows:
                    if message_id != latest_message_id:
                        continue
                    if document_id not in seen:
                        seen.add(document_id)
                        requested_ids.append(document_id)
                        if len(requested_ids) >= _MAX_ATTACHMENT_IDS:
                            break

            # Keep the write-path organization predicate as the shared access
            # funnel, then repeat it on the content read for defense in depth.
            owned_ids = await workspace_access.filter_owned_document_ids(
                session, requested_ids, actor.id
            )
            documents_result = await session.execute(
                select(Document).where(
                    Document.id.in_(owned_ids),
                    Document.organization_id == organization_uuid,
                    Document.is_deleted == False,  # noqa: E712
                )
            )
            documents = {
                document.id: document for document in documents_result.scalars().all()
            }

            contexts: list[dict] = []
            statuses: list[dict] = []
            for document_id in requested_ids:
                document = documents.get(document_id)
                if document is None:
                    statuses.append(_attachment_status(document_id, "unavailable"))
                    continue

                raw_processing_status = getattr(document, "processing_status", None)
                raw_processing_status = getattr(
                    raw_processing_status, "value", raw_processing_status
                )
                processing_status = str(raw_processing_status).lower()
                if processing_status in _ATTACHMENT_PROCESSING_STATUSES:
                    statuses.append(
                        _attachment_status(
                            document_id, "processing", getattr(document, "title", None)
                        )
                    )
                    continue
                if processing_status != ProcessingStatus.COMPLETED.value:
                    statuses.append(
                        _attachment_status(
                            document_id, "unavailable", getattr(document, "title", None)
                        )
                    )
                    continue

                content = _attachment_excerpt(
                    getattr(document, "content_text", None) or "", query
                )
                if not content.strip():
                    statuses.append(
                        _attachment_status(
                            document_id, "unavailable", getattr(document, "title", None)
                        )
                    )
                    continue

                from src.services.agent._pii_redact import redact_pii

                contexts.append(
                    {
                        "document_id": str(document_id),
                        "title": redact_pii(
                            getattr(document, "title", None) or "Untitled"
                        )[:500],
                        "content": content,
                        "score": 1.0,
                        "score_source": "attachment",
                        "context_origin": "attachment",
                    }
                )
                statuses.append(
                    _attachment_status(
                        document_id, "ready", getattr(document, "title", None)
                    )
                )
            return contexts, statuses
    except Exception:  # noqa: BLE001 - attachment reads fail closed
        logger.warning(
            "attachment context load failed; returning no content", exc_info=True
        )
        if not explicit_ids and thread_id and not requested_ids:
            return [], [dict(_FOLLOW_UP_ATTACHMENT_UNAVAILABLE)]
        return [], [
            _attachment_status(document_id, "unavailable")
            for document_id in requested_ids
        ]


# ---------------------------------------------------------------------------
# DO KB primary read (Phase 4b) + Postgres hybrid fallback
# ---------------------------------------------------------------------------


def _shape_do_kb_context(chunk, title_by_key: dict[str, tuple[str, str]]) -> dict:
    """Map a DO KB chunk into the rag_node context envelope.

    Resolves the chunk's storage-key ``document_id`` back to the canonical
    ``Document.id`` (UUID) + ``Document.title`` so citations render with real
    titles instead of "DO KB chunk".
    """
    from src.services.agent._pii_redact import redact_pii

    storage_key = chunk.document_id
    resolved_id, title = title_by_key.get(storage_key, (None, None))
    try:
        canonical_document_id = str(UUID(str(resolved_id))) if resolved_id else None
    except (ValueError, TypeError, AttributeError):
        canonical_document_id = None
    title_candidate = title or (chunk.metadata or {}).get("title")
    safe_title = redact_pii(title_candidate).strip() or "Untitled"
    return {
        "document_id": canonical_document_id,
        "title": safe_title,
        "content": chunk.text[:3000],
        "score": float(chunk.score),
        "score_source": (chunk.metadata or {}).get("score_source"),
    }


try:
    from langsmith import traceable as _ls_traceable
except Exception:  # noqa: BLE001 - langsmith optional at runtime
    _ls_traceable = None


def _maybe_traced_retriever(name: str):
    """Return a langsmith traceable decorator with run_type=retriever, or no-op."""
    if _ls_traceable is None:

        def _identity(fn):
            return fn

        return _identity
    return _ls_traceable(run_type="retriever", name=name)


@_maybe_traced_retriever("do_kb_retriever")
async def _try_primary_do_kb_read(
    query: str,
    user_id: Optional[str],
    organization_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Optional[List[dict]]:
    """Run the complete primary-read stage inside the hot-path deadline."""
    from src.core.config import settings as _kb_cfg

    timeout = float(_kb_cfg.DO_KB_RETRIEVE_TIMEOUT_SECONDS)
    try:
        return await asyncio.wait_for(
            _try_primary_do_kb_read_impl(
                query,
                user_id,
                organization_id,
                project_id,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "do_kb primary read exceeded %.1fs — falling back",
            timeout,
        )
        _record_do_kb_read("do_kb_timeout")
        return None


async def _try_primary_do_kb_read_impl(
    query: str,
    user_id: Optional[str],
    organization_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Optional[List[dict]]:
    """Phase 4b: return DO KB chunks shaped like rag_node contexts.

    Returns None when primary read is disabled, the org has no KB, or the
    call fails — caller then falls back to the legacy Postgres hybrid path.

    Takes scalar ``user_id`` / ``organization_id`` from the ids-only
    configurable (audit B8) and opens its own tool-call-scoped session.

    When *project_id* is provided, post-filters chunks so only documents
    that belong to the active project survive. DO KB itself is org-scoped,
    so cross-project leakage (chunks from sibling projects) is filtered
    via the ``collection_documents`` association table.
    """
    try:
        from src.core.config import settings as _kb_cfg

        bedrock_kb_id = getattr(_kb_cfg, "BEDROCK_KB_ID", "")
        bedrock_enabled = isinstance(bedrock_kb_id, str) and bool(bedrock_kb_id)
        if not (getattr(_kb_cfg, "DO_KB_PRIMARY_READ", False) or bedrock_enabled):
            return None
        # Normalise to a UUID so downstream org-scoped queries bind the same
        # type the ORM User's organization_id used to provide.
        try:
            from uuid import UUID as _UUID

            org_id = _UUID(str(organization_id).strip()) if organization_id else None
        except (ValueError, TypeError, AttributeError):
            org_id = None
        if not org_id:
            return None

        from src.services.agent.tool_session import tool_session
        from src.services.do_kb.retrieval import (
            DOKBRetrieveStatus,
            resolve_org_kb_uuid,
            retrieve_kb_chunks,
        )

        async with tool_session() as session:
            kb_uuid = await resolve_org_kb_uuid(session, org_id)
            if not kb_uuid:
                return None

            # Resolved BEFORE the retrieve: this check needs only the session
            # and the ids, and both of its non-owning outcomes (abort, or drop
            # to org-wide) are decided independently of what comes back — so
            # running it first keeps the abort path from paying for a remote
            # retrieve it discards.
            #
            # Only scope by project_id if the caller actually owns it; otherwise
            # drop the scope (org-wide read, the same as no project context)
            # rather than filter by — and thereby disclose membership of — a
            # project that isn't theirs. A verified negative (False) still
            # drops to org-wide, same as before (documented membership-
            # inference defense). An unverifiable check (None — DB blip)
            # must NOT be treated as "not owned": that would fall through
            # to the org-wide branch, WIDENING scope during exactly the
            # outage that should have narrowed it (audit M2). Abort the
            # primary read instead so the caller falls back to
            # ``_legacy_hybrid_search_fallback``, which re-verifies
            # ownership itself and fails closed to no results, never org-wide.
            scoped_project_id = project_id
            if project_id:
                owns = await _user_owns_project(session, project_id, user_id)
                if owns is None:
                    logger.warning(
                        "do_kb_read: ownership check for project %s unverifiable "
                        "— aborting primary read",
                        project_id,
                    )
                    _record_do_kb_read("do_kb_ownership_unverifiable")
                    return None
                if owns is False:
                    logger.info(
                        "do_kb_read: project %s not owned by caller — dropping scope",
                        project_id,
                    )
                    scoped_project_id = None

            # Shared retrieve core (audit B2): timeout wrap + 404/other logging
            # live in ``retrieve_kb_chunks``; this node keeps its own telemetry
            # + fallback-to-None semantics by mapping the outcome.
            outcome = await retrieve_kb_chunks(
                kb_uuid=kb_uuid,
                query=query,
                org_id=org_id,
                timeout=_kb_cfg.DO_KB_RETRIEVE_TIMEOUT_SECONDS,
            )
            if outcome.status is DOKBRetrieveStatus.TIMEOUT:
                _record_do_kb_read("do_kb_timeout")
                return None
            if outcome.status is DOKBRetrieveStatus.ERROR_404:
                _record_do_kb_read("do_kb_error_404")
                return None
            if outcome.status is DOKBRetrieveStatus.ERROR_OTHER:
                _record_do_kb_read("do_kb_error_other")
                return None
            result = outcome.result
            if not result.chunks:
                # KB is up and reachable, it just had no matches — a healthy
                # outcome, distinct from an error. Fall back to hybrid search.
                _record_do_kb_read("do_kb_empty")
                return None

            from src.services.do_kb.resolve import resolve_and_filter_chunks

            title_by_key, chunks_to_emit = await resolve_and_filter_chunks(
                chunks=result.chunks,
                org_id=org_id,
                session=session,
                project_id=scoped_project_id,
            )
            # A shared KB must never emit an unresolvable document, even if
            # provider-side metadata filtering was bypassed or misconfigured.
            if bedrock_enabled:
                chunks_to_emit = [
                    chunk
                    for chunk in chunks_to_emit
                    if chunk.document_id in title_by_key
                ]

            # Two distinct empty-result paths that trigger the hybrid fallback:
            if scoped_project_id and not title_by_key and result.chunks:
                logger.info(
                    "do_kb_read: %d chunks unresolvable to org documents under "
                    "project scope %s — returning None to trigger fallback",
                    len(result.chunks),
                    scoped_project_id,
                )
                _record_do_kb_read("project_scope_empty")
                return None
            if scoped_project_id and title_by_key and not chunks_to_emit:
                logger.info(
                    "do_kb_read: all %d chunks filtered out by project scope %s",
                    len(result.chunks),
                    scoped_project_id,
                )
                _record_do_kb_read("project_scope_empty")
                return None

            from src.services.do_kb.postprocess import sanitize_and_deduplicate_chunks

            postprocessed = sanitize_and_deduplicate_chunks(chunks_to_emit)
            logger.info(
                "do_kb primary-read postprocess complete",
                extra={
                    "input_count": postprocessed.input_count,
                    "output_count": postprocessed.output_count,
                    "duplicate_count": postprocessed.duplicate_count,
                    "redacted_count": postprocessed.redacted_count,
                },
            )
            chunks_to_emit = postprocessed.chunks
            if not chunks_to_emit:
                _record_do_kb_read("do_kb_empty")
                return None

        if getattr(_kb_cfg, "AGENT_DOKB_COHERE_RERANK", False) and chunks_to_emit:
            from src.services.do_kb.rerank import cohere_rescore_chunks

            chunks_to_emit = await cohere_rescore_chunks(query, chunks_to_emit)

        # Filter BEFORE recording the outcome (audit review, PR #1395): recording
        # "success" first meant an all-filtered read (every chunk below the
        # cohere floor) still got double-counted as a "fallback_used" by the
        # caller — masking the new failure mode from the metric.
        shaped = [_shape_do_kb_context(c, title_by_key) for c in chunks_to_emit]
        kept = drop_low_relevance_chunks(shaped)
        _record_do_kb_read("success" if kept else "do_kb_low_relevance")
        return kept
    except Exception:  # noqa: BLE001
        # exc_info keeps the traceback for operators; the raw exception string
        # stays out of the indexed message (can carry the user query / chunks).
        logger.warning("do_kb primary read failed, falling back", exc_info=True)
        _record_do_kb_read("do_kb_error_other")
        return None


@_maybe_traced_retriever("hybrid_search_retriever")
async def _legacy_hybrid_search_fallback(
    query: str,
    user_id: str,
    organization_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> List[dict]:
    """Fallback to hybrid search when DO KB is unavailable or returns nothing.

    Takes scalar ids from the ids-only configurable (audit B8). When a project
    is active, resolve its owned document ids first and reuse SearchFilter's
    existing document-id constraint. A missing, unowned, or empty project fails
    closed to no results instead of widening the read to the whole organization.
    """
    try:
        from src.models.search_schemas import (
            SearchFilter,
            SearchQuery,
            SearchSortOrder,
            SearchType,
        )
        from src.services.search.hybrid_search_service import hybrid_search_service

        filters = None
        if project_id:
            try:
                project_uuid = UUID(str(project_id))
                user_uuid = UUID(str(user_id))
            except (ValueError, TypeError, AttributeError):
                return []

            from sqlalchemy import select

            from src.models.collection import Collection, CollectionDocument
            from src.models.workspace import Workspace
            from src.services.agent.tool_session import tool_session

            async with tool_session() as session:
                document_ids = list(
                    (
                        await session.execute(
                            select(CollectionDocument.document_id)
                            .join(
                                Collection,
                                Collection.id == CollectionDocument.collection_id,
                            )
                            .join(Workspace, Workspace.id == Collection.workspace_id)
                            .where(
                                Collection.id == project_uuid,
                                Collection.is_deleted == False,  # noqa: E712
                                CollectionDocument.is_deleted == False,  # noqa: E712
                                _workspace_accessible_by(user_uuid),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            if not document_ids:
                return []
            filters = SearchFilter(
                document_ids=[str(document_id) for document_id in document_ids]
            )

        search_request = SearchQuery(
            query=query,
            limit=5,
            search_type=SearchType.HYBRID,
            sort_order=SearchSortOrder.RELEVANCE,
            filters=filters,
        )
        org_id = str(organization_id) if organization_id else None
        uid = str(user_id)
        search_response = await asyncio.wait_for(
            asyncio.to_thread(
                hybrid_search_service.search,
                search_request=search_request,
                user_id=uid,
                organization_id=org_id,
            ),
            timeout=15.0,
        )
        from src.services.agent._pii_redact import redact_pii

        contexts: List[dict] = []
        for i, result in enumerate(search_response.results[:5]):
            doc_id = getattr(result, "document_id", None)
            title = getattr(result, "title", "Untitled") or f"Document {i + 1}"
            metadata = getattr(result, "metadata", {}) or {}
            content = metadata.get("full_text") or metadata.get("text", "")
            if not content:
                content = getattr(result, "content_preview", None) or ""
            score = getattr(result, "relevance_score", 0.0)
            contexts.append(
                {
                    "document_id": str(doc_id) if doc_id else None,
                    # Redact BEFORE the slice: a token straddling the 3000-char
                    # boundary must not survive as a partial-but-matchable
                    # prefix, and the DO KB branch already ships sanitized
                    # text — this keeps the two paths equivalent.
                    "title": redact_pii(title),
                    "content": redact_pii(content)[:3000],
                    "score": float(score),
                }
            )
        return contexts
    except Exception:
        logger.warning("hybrid search fallback failed", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------

# Leading imperative tokens that add no retrieval signal ("Summarize this
# document ...", "make notes ...").
_INSTRUCTION_LEAD_RE = re.compile(
    r"^\s*(please\s+)?(summari[sz]e|make|create|save|add|write|draft|compare|"
    r"take\s+notes|note|find|search|get|show|list|export)\b[\s,:-]*",
    re.IGNORECASE,
)
# Quoted entity spans (paper titles). 6+ chars to avoid matching stray quotes.
_QUOTED_SPAN_RE = re.compile(r"[\"“”']([^\"“”']{6,})[\"“”']")


def _coerce_text(content: Any) -> str:
    """Flatten a possibly-multimodal HumanMessage.content into plain text.

    ``content`` may be a list of content blocks; the raw value was previously
    used directly and any downstream ``.lower()/.split()`` would raise.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
        return " ".join(parts)
    return str(content or "")


def _normalize_injected_contexts(contexts: Any) -> List[dict]:
    """Keep test/custom retrieval results on the normal context contract."""
    if not isinstance(contexts, list):
        return []

    normalized: List[dict] = []
    for item in contexts:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        content = item.get("content")
        document_id = item.get("document_id")
        if (
            not isinstance(title, str)
            or not isinstance(content, str)
            or (document_id is not None and not isinstance(document_id, str))
        ):
            continue
        try:
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError, OverflowError):
            score = 0.0
        if not math.isfinite(score):
            score = 0.0
        normalized.append({**item, "content": content[:3000], "score": score})
    return normalized


def _build_search_query(raw: str) -> str:
    """Derive a focused retrieval query from a raw user message.

    Trace 019ea8f0 fed the retriever the whole message — three quoted paper
    titles glued to "make notes and save them" — as one literal query. It
    embedded to a degenerate centroid that matched nothing, and the
    instruction tokens diluted lexical search. Prefer the quoted entity names
    (what the user actually wants retrieved); otherwise strip a leading
    imperative clause. Fall back to the raw text when nothing structured is
    found.
    """
    if not raw:
        return raw
    quoted = _QUOTED_SPAN_RE.findall(raw)
    if quoted:
        return " ".join(q.strip() for q in quoted)[:512]
    cleaned = _INSTRUCTION_LEAD_RE.sub("", raw).strip()
    return (cleaned or raw)[:512]


# ---------------------------------------------------------------------------
# RAG node
# ---------------------------------------------------------------------------


@track_node_execution("rag_node")
async def rag_node(state: AgentState, config: RunnableConfig) -> dict:
    """Retrieve relevant documents via hybrid search and store in state."""
    # Lazy import — graph.py owns the project-id text extractor.
    from src.services.agent.graph import _extract_project_id_from_text

    configurable = config.get("configurable", {})
    # Ids-only configurable (audit B8): retrieval needs the scalar user /
    # org identifiers only — sessions are opened by the helpers themselves.
    user_id = str(configurable.get("user_id", "") or "")
    organization_id = str(configurable.get("organization_id", "") or "")

    # Find the last user message to use as search query
    last_user_msg: Optional[str] = None
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            last_user_msg = _coerce_text(msg.content)
            break

    # Fast-path: skip retrieval entirely for short, clearly-conversational
    # queries (e.g. "hi", "thanks!"). Saves an embedding + hybrid search call
    # and avoids injecting ~935 retrieval tokens into the system prompt.
    #
    # Earlier revision gated the skip on "no project context to propagate".
    # That gate made every short query in an active project trigger RAG
    # (observed trace 019e168a: "hi" in ML-in-Health-Care project pulled
    # 5 chunks from sibling Copilot productivity PDFs and used 7152 input
    # tokens). Project context still propagates via current_project_id +
    # page_context — we only suppress the RAG chunk injection itself.
    page_context = dict(state.get("page_context") or {})
    existing_project_id = state.get("current_project_id") or page_context.get(
        "project_id"
    )

    # Explicit attachments are authoritative for this turn. On a follow-up,
    # an omitted list may resolve to the latest attached user turn, but only
    # while retrieval is enabled; ``use_rag=False`` still suppresses implicit
    # corpus/thread reads. In either case, an attachment scope returns before
    # project/org retrieval so unrelated documents cannot enter the prompt.
    explicit_attachment_ids = state.get("attachment_ids") or []
    should_load_attachment_scope = bool(explicit_attachment_ids) or (
        state.get("use_rag", True) is not False
        and state.get("thread_persistence") == "durable"
        and bool(configurable.get("thread_id"))
        and not configurable.get("search_fn")
    )
    if should_load_attachment_scope:
        attachment_contexts, attachment_status = await _load_attachment_contexts(
            attachment_ids=explicit_attachment_ids,
            thread_id=str(configurable.get("thread_id") or ""),
            user_id=user_id,
            organization_id=organization_id,
            query=last_user_msg or "",
        )
        if explicit_attachment_ids or attachment_contexts or attachment_status:
            state_update: Dict[str, Any] = {
                "retrieved_contexts": attachment_contexts,
                "attachment_status": attachment_status,
            }
            if existing_project_id:
                state_update["current_project_id"] = existing_project_id
            return state_update

    if state.get("use_rag", True) is False:
        logger.debug("rag_node: retrieval disabled by request")
        state_update: Dict[str, Any] = {"retrieved_contexts": []}
        if existing_project_id:
            state_update["current_project_id"] = existing_project_id
        return state_update

    if last_user_msg and not _is_retrieval_query(last_user_msg):
        logger.debug(
            "rag_node: skipping retrieval for conversational query: %r",
            last_user_msg[:80],
        )
        # Still surface a UUID extracted from the text or carried in state
        # so downstream nodes can act on the project context.
        extracted_pid = _extract_project_id_from_text(last_user_msg or "")
        if extracted_pid and not existing_project_id:
            # Only the text-extraction path needs gating (audit M3) —
            # ``existing_project_id`` already flows through a verified path
            # and takes precedence in ``_resolve_active_project_id`` anyway,
            # so skip the DB round trip when it won't change the outcome.
            extracted_pid = await _verify_extracted_project_id(extracted_pid, user_id)
        resolved_pid: Optional[str] = _resolve_active_project_id(
            existing_project_id, extracted_pid
        )
        state_update: Dict[str, Any] = {"retrieved_contexts": []}
        if resolved_pid:
            state_update["current_project_id"] = resolved_pid
            if (
                page_context.get("project_id") != resolved_pid
                or page_context.get("type") != "project"
            ):
                state_update["page_context"] = {
                    **page_context,
                    "type": "project",
                    "project_id": resolved_pid,
                }
        return state_update

    # Extract a project UUID from the latest user message (e.g. a pasted
    # /projects/<uuid> URL) so downstream nodes carry the context across
    # turns without depending on the client always re-sending page_context.
    extracted_pid = _extract_project_id_from_text(last_user_msg or "")
    if extracted_pid and not existing_project_id:
        # Gate the text-extraction path only (audit M3) — see the identical
        # comment in the conversational branch above.
        extracted_pid = await _verify_extracted_project_id(extracted_pid, user_id)

    resolved_project_id: Optional[str] = _resolve_active_project_id(
        existing_project_id, extracted_pid
    )
    state_update: Dict[str, Any] = {}
    if resolved_project_id:
        state_update["current_project_id"] = resolved_project_id
        # Mirror into page_context so the system prompt built by llm_node
        # tells the LLM about the active project, and any tool that reads
        # page_context (e.g. list_project_documents) can resolve it.
        # When a project is resolved, force type="project" — otherwise the
        # llm_node falls into the "chat" branch and never tells the LLM that
        # a project is active. This is what was happening when users named
        # a project in plain text after the CLI sent type="chat".
        needs_update = (
            page_context.get("project_id") != resolved_project_id
            or page_context.get("type") != "project"
        )
        if needs_update:
            page_context = {
                **page_context,
                "type": "project",
                "project_id": resolved_project_id,
            }
            state_update["page_context"] = page_context

    if not last_user_msg or not user_id:
        return {"retrieved_contexts": [], **state_update}

    # Test-time injection / custom retriever override receives the query
    # verbatim — the caller owns it. Query cleaning below applies only to the
    # built-in DO KB + hybrid retrieval.
    search_fn = configurable.get("search_fn")
    if search_fn:
        try:
            contexts = _normalize_injected_contexts(
                await search_fn(last_user_msg, user_id)
            )
            return {"retrieved_contexts": contexts, **state_update}
        except Exception as e:
            logger.warning("injected search_fn failed", exc_info=e)
            return {"retrieved_contexts": [], **state_update}

    # Clean the retrieval query: drop instruction clauses, prefer quoted
    # entity names. Keep last_user_msg raw for project-id/intent checks above.
    search_query = _build_search_query(last_user_msg)

    # Skip the org-wide knowledge-base read when no project context is
    # active. Trace 019e191a showed a chat-mode "Find recent transformer
    # papers" query pull 5 chunks of unrelated Copilot productivity PDFs
    # — the org KB indexes every project's docs, so without a project
    # filter the chunks are noise that bloats input by ~10k chars. The
    # agent will use search_arxiv/search_documents for explicit lookup.
    if not resolved_project_id:
        logger.debug("rag_node: skipping DO KB read — no active project context")
        return {"retrieved_contexts": [], **state_update}

    # Production retrieval: DO KB primary, hybrid search fallback.
    # Pass resolved_project_id so KB results stay scoped to the active project
    # — org-scoped KB otherwise leaks chunks from sibling projects.
    primary_contexts: Optional[List[dict]] = await _try_primary_do_kb_read(
        search_query, user_id, organization_id, project_id=resolved_project_id
    )
    # `is not None` (not truthiness — audit review, PR #1395): a successful
    # read that the relevance floor filtered down to nothing is a healthy
    # "retrieved, nothing relevant" outcome and must NOT fall through to the
    # unfiltered legacy hybrid search — only an actually unavailable/errored/
    # empty primary read (which always returns None) may fall back.
    if primary_contexts is not None:
        return {"retrieved_contexts": primary_contexts, **state_update}

    _record_do_kb_read("fallback_used")
    contexts = await _legacy_hybrid_search_fallback(
        search_query,
        user_id,
        organization_id,
        project_id=resolved_project_id,
    )
    return {"retrieved_contexts": contexts, **state_update}


__all__ = [
    "is_conversational",
    "_is_retrieval_query",
    "_shape_do_kb_context",
    "_try_primary_do_kb_read",
    "_legacy_hybrid_search_fallback",
    "rag_node",
]
