"""Shared DO KB retrieve orchestration.

Two call sites drive the org's DigitalOcean Knowledge Base retrieve endpoint
with byte-identical org→kb lookup and 404-vs-transient error classification:

  - the RAG node's primary read (``_nodes_rag._try_primary_do_kb_read``),
    which wraps the call in an ``asyncio.wait_for`` timeout and returns
    ``None`` on any non-success so the caller falls back to hybrid search;
  - the agent ``do_kb_retrieve`` tool (``tools_impl._tool_do_kb_retrieve``),
    which has no timeout and returns an error dict on failure.

They diverge in timeout, telemetry, and return shape but shared the *same*
org→kb resolution and the *same* 404/other logging (audit finding B2). This
module is the single home for that shared core; each caller maps the returned
:class:`DOKBRetrieveOutcome` onto its own return/telemetry contract, so the
per-site timeout / circuit-breaker / error semantics are preserved exactly.

Only :class:`asyncio.TimeoutError` (when a *timeout* is supplied) and
:class:`~src.services.do_kb.client.DOKnowledgeBaseError` are handled here; any
other exception propagates so each caller's own broad ``except`` keeps its
existing behavior.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .models import RetrieveResult

logger = logging.getLogger(__name__)


class DOKBRetrieveStatus(str, Enum):
    """Discriminated outcome of a DO KB retrieve attempt."""

    SUCCESS = "success"
    TIMEOUT = "timeout"
    ERROR_404 = "error_404"
    ERROR_OTHER = "error_other"


@dataclass
class DOKBRetrieveOutcome:
    """Result envelope for :func:`retrieve_kb_chunks`.

    ``result`` is populated only on :attr:`DOKBRetrieveStatus.SUCCESS`;
    ``error`` carries the originating ``DOKnowledgeBaseError`` on the two
    ``ERROR_*`` statuses so callers can surface the exact message they did
    before the dedup.
    """

    status: DOKBRetrieveStatus
    result: Optional[RetrieveResult] = None
    error: Optional[Exception] = None


async def resolve_org_kb_uuid(session: Any, org_id: Any) -> Optional[str]:
    """Return the org's provisioned DO KB uuid, or ``None`` if it has none.

    Shared by both call sites, which previously each loaded the
    ``Organization`` row and read ``do_kb_uuid`` inline.
    """
    if not org_id:
        return None
    from src.models.organization import Organization

    org = await session.get(Organization, org_id)
    return getattr(org, "do_kb_uuid", None) if org else None


async def retrieve_kb_chunks(
    *,
    kb_uuid: str,
    query: str,
    org_id: Any = None,
    top_k: Optional[int] = None,
    timeout: Optional[float] = None,
    filters: Optional[dict[str, Any]] = None,
) -> DOKBRetrieveOutcome:
    """Call DO KB retrieve with unified timeout + 404/error classification.

    Args:
        kb_uuid: The org's knowledge-base uuid.
        query: The retrieval query.
        org_id: Included in log lines only (for operator triage).
        top_k: Passed through to the client (``None`` uses the client default).
        timeout: When set, wrap the call in ``asyncio.wait_for``; on expiry
            return :attr:`DOKBRetrieveStatus.TIMEOUT` instead of blocking on the
            full HTTP retry window. When ``None`` the call is awaited directly
            (no premature timeout), matching the tool's semantics.

    Returns:
        A :class:`DOKBRetrieveOutcome`. Non-``DOKnowledgeBaseError`` /
        non-timeout exceptions are *not* swallowed — they propagate to the
        caller's own error handling.
    """
    from src.services.do_kb import DOKnowledgeBaseError, get_do_kb_client

    client = get_do_kb_client()
    retrieve_kwargs: dict[str, Any] = {
        "kb_uuid": kb_uuid,
        "query": query,
        "top_k": top_k,
    }
    if filters is not None:
        retrieve_kwargs["filters"] = filters
    try:
        if timeout is not None:
            result = await asyncio.wait_for(
                client.retrieve(**retrieve_kwargs),
                timeout=timeout,
            )
        else:
            result = await client.retrieve(**retrieve_kwargs)
    except asyncio.TimeoutError:
        logger.warning(
            "do_kb retrieve timed out after %.1fs — falling back (org_id=%s)",
            timeout,
            org_id,
        )
        return DOKBRetrieveOutcome(status=DOKBRetrieveStatus.TIMEOUT)
    except DOKnowledgeBaseError as exc:
        # A 404 means the KB was deleted on DO's side (permanent — needs a human
        # to re-provision). Log distinctly at ERROR with org_id + kb_uuid so it
        # doesn't blend into ordinary transient failures, which stay at WARNING.
        if exc.status_code == 404:
            logger.error(
                "do_kb retrieve 404 — knowledge base deleted/missing on DO's "
                "side; retrieval permanently degraded until re-provisioned "
                "(org_id=%s, kb_uuid=%s)",
                org_id,
                kb_uuid,
            )
            return DOKBRetrieveOutcome(status=DOKBRetrieveStatus.ERROR_404, error=exc)
        logger.warning(
            "do_kb retrieve failed (status=%s, org_id=%s): %s",
            exc.status_code,
            org_id,
            exc,
        )
        return DOKBRetrieveOutcome(status=DOKBRetrieveStatus.ERROR_OTHER, error=exc)

    return DOKBRetrieveOutcome(status=DOKBRetrieveStatus.SUCCESS, result=result)
