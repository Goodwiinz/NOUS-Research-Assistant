"""Structured error recovery for agent tool execution."""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, cast

logger = logging.getLogger(__name__)

ErrorCategory = Literal["transient", "recoverable", "user_fixable", "fatal"]

# Categories a tool may declare for itself in its error payload. Anything
# else is ignored and falls through to the keyword heuristics, so a typo
# cannot silently become a category.
#
# "transient" is deliberately NOT declarable. This function classifies a
# payload the tool *returned*, so the call ran to completion — transience is
# a property of the call, knowable on the raised-exception path
# (classify_error) rather than here. It also carries teeth: _nodes_tools
# sets error_increment = 0 for transient, so a tool could zero its own
# contribution to MAX_ERRORS by writing one string, and reflection skips its
# response-quality gate for transient failures. Nothing declares it today;
# keep that door shut.
_DECLARABLE_CATEGORIES: frozenset[str] = frozenset(
    {"recoverable", "user_fixable", "fatal"}
)

# Maps (tool_name, error_keyword) -> (category, suggestion)
TOOL_ERROR_HINTS: dict[tuple[str, str], tuple[ErrorCategory, str]] = {
    ("add_document_to_project", "not found"): (
        "recoverable",
        "The document must be ingested first. Use ingest_arxiv_papers with the arXiv paper IDs.",
    ),
    ("add_document_to_project", "not a valid uuid"): (
        "recoverable",
        "Use document UUIDs from ingest_arxiv_papers, not arXiv paper IDs.",
    ),
    ("create_project_note", "project_id is required"): (
        "recoverable",
        "Call list_projects for an existing project_id, then retry with " "that id.",
    ),
    ("create_draft", "project_id is required"): (
        "recoverable",
        "Call list_projects for an existing project_id, then retry with " "that id.",
    ),
    ("ingest_arxiv_papers", "timed out"): (
        "transient",
        "ArXiv ingestion timed out. Try fewer papers (max 3 at a time).",
    ),
    ("search_arxiv", "no results"): (
        "recoverable",
        "No results found. Try broader search terms or different keywords.",
    ),
    ("search_documents", "no results"): (
        "recoverable",
        "No matching documents found. Try different search terms or ingest new papers first.",
    ),
}

# Exception types that are always transient (CancelledError is intentionally excluded —
# it means the caller aborted the request and must propagate immediately, not be retried)
_TRANSIENT_EXCEPTIONS = (asyncio.TimeoutError, ConnectionError, OSError)
_USER_FIXABLE_EXCEPTIONS = (PermissionError,)
_RATE_LIMIT_KEYWORDS = ("rate limit", "http 429", "too many requests")
_ARXIV_UNAVAILABLE_KEYWORDS = (
    "http 406",
    "arxiv search unavailable",
    "arxiv search is temporarily unavailable",
    "arxiv request coordination",
)
_ARXIV_TOOLS = frozenset({"search_arxiv", "ingest_arxiv_papers"})

# Keyword signals that an error is transient/upstream — retrying (or simply
# waiting) can recover, and regenerating the AI response cannot. Connection
# keywords are scoped: a bare "connection" also matches benign payload text
# like "no connection found between entities" and must not retry a fatal error.
# Rate-limit signals ("429", "rate limit", "too many requests") are included so
# that e.g. an arXiv 429 — surfaced as "ArXiv rate limited (HTTP 429)" — is not
# misclassified as fatal, which would burn the error ceiling and trigger a
# wasteful reflection revise loop (the response cannot fix an upstream limit).
_TRANSIENT_ERROR_KEYWORDS = (
    "timeout",
    "timed out",
    "connection refused",
    "connection reset",
    "connection error",
    "connection closed",
    "connection failed",
    # Canonical requests/urllib3 failure string:
    # ('Connection aborted.', RemoteDisconnected(...))
    "connection aborted",
    "econnrefused",
    "econnreset",
    # Rate limiting. "rate limit" also matches "rate limited". "429" is
    # anchored as "http 429" so a bare 429 inside an arXiv id / count / year
    # (e.g. "2304.04290 not found") is not misread as a rate limit.
    "rate limit",
    "http 429",
    "too many requests",
)


@dataclass(frozen=True)
class ToolError:
    """Structured tool error with category and actionable suggestion."""

    category: ErrorCategory
    message: str
    suggestion: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": self.message,
            "error_type": self.category,
        }
        if self.suggestion:
            payload["suggestion"] = self.suggestion
        return payload

    def to_tool_message_content(self) -> str:
        return json.dumps(self.to_payload())

    def to_state_info(self) -> dict:
        return {
            "category": self.category,
            "message": self.message,
            "suggestion": self.suggestion,
        }


def _arxiv_rate_limit_error() -> ToolError:
    # Matches the arXiv client's cooldown without exposing raw exception text.
    return ToolError(
        category="transient",
        message="ArXiv is rate limited. Wait at least 60 seconds before retrying.",
        suggestion="Do not retry arXiv this turn. Use existing sources or another database.",
    )


def _arxiv_unavailable_error() -> ToolError:
    """Return a client-safe 406/coordination outcome distinct from 429."""
    return ToolError(
        category="transient",
        message="ArXiv search is temporarily unavailable.",
        suggestion="Do not retry arXiv this turn. Use cached or existing sources.",
    )


def classify_error(tool_name: str, exc: Exception) -> ToolError:
    """Classify a thrown exception into a ToolError."""
    msg = str(exc)

    # Check user_fixable before transient (PermissionError is a subclass of OSError)
    if isinstance(exc, _USER_FIXABLE_EXCEPTIONS):
        return ToolError(
            category="user_fixable",
            message="The tool could not access the requested resource.",
            suggestion="Check your permissions or ask the user for help.",
        )

    if tool_name in _ARXIV_TOOLS and any(
        kw in msg.lower() for kw in _ARXIV_UNAVAILABLE_KEYWORDS
    ):
        return _arxiv_unavailable_error()

    if tool_name == "search_arxiv" and any(
        kw in msg.lower() for kw in _RATE_LIMIT_KEYWORDS
    ):
        return _arxiv_rate_limit_error()

    if isinstance(exc, _TRANSIENT_EXCEPTIONS):
        return ToolError(
            category="transient",
            message="The tool timed out or its upstream connection failed. Please retry.",
            suggestion="Retrying automatically...",
        )

    # Check hints by keyword
    msg_lower = msg.lower()
    for (tn, keyword), (cat, suggestion) in TOOL_ERROR_HINTS.items():
        if tn == tool_name and keyword in msg_lower:
            return ToolError(
                category=cat,
                message=(
                    "The tool timed out or its upstream connection failed. Please retry."
                    if cat == "transient"
                    else "The tool could not complete the request."
                ),
                suggestion=suggestion,
            )

    # Transient infrastructure / rate-limit errors raised as exceptions.
    if any(kw in msg_lower for kw in _TRANSIENT_ERROR_KEYWORDS):
        return ToolError(
            category="transient",
            message="The tool timed out or its upstream connection failed. Please retry.",
            suggestion="Retrying automatically...",
        )

    return ToolError(
        category="fatal", message="The tool could not complete the request."
    )


def tool_error_payload(tool_name: str, exc: Exception) -> dict[str, Any]:
    """Client-safe error payload for tools that catch their own exceptions."""
    return classify_error(tool_name, exc).to_payload()


def classify_error_from_payload(tool_name: str, payload: dict) -> ToolError:
    """Classify an error from a returned payload dict.

    Keyword check ordering matters: more-specific categories must run
    before more-general ones. Otherwise:
    - "permission timeout" gets caught by ``timeout`` and misclassified
      as transient (the underlying cause is access, not flakiness).
    - "invalid api key" / "invalid credentials" get caught by
      ``invalid`` and misclassified as recoverable input errors.
    """
    # `or ""` (not a default): tools have returned an explicit `"error": None`
    # (audit B8-S2), and `str()` covers a non-string error value.
    error_msg = str(payload.get("error") or "")
    msg_lower = error_msg.lower()

    # 1. Per-tool hints (most specific).
    for (tn, keyword), (cat, suggestion) in TOOL_ERROR_HINTS.items():
        if tn == tool_name and keyword in msg_lower:
            return ToolError(category=cat, message=error_msg, suggestion=suggestion)

    # 2. Credential-style "invalid" errors are NOT recoverable by the LLM
    # — the user has to fix them. Match these BEFORE the generic
    # "invalid" fallthrough below.
    if any(
        kw in msg_lower
        for kw in (
            "invalid api key",
            "invalid credentials",
            "invalid token",
            "invalid signature",
            "expired token",
        )
    ):
        return ToolError(
            category="user_fixable",
            message=error_msg,
            suggestion="Authentication failed — check API credentials.",
        )

    # 3. Permission / authorization errors take precedence over transient
    # ones so phrases like "permission timeout" are not swallowed by the
    # transient-keyword check.
    if any(kw in msg_lower for kw in ("permission", "unauthorized", "forbidden")):
        return ToolError(
            category="user_fixable",
            message=error_msg,
            suggestion="You may need different permissions.",
        )

    if tool_name in _ARXIV_TOOLS and any(
        kw in msg_lower for kw in _ARXIV_UNAVAILABLE_KEYWORDS
    ):
        return _arxiv_unavailable_error()

    if tool_name == "search_arxiv" and any(
        kw in msg_lower for kw in _RATE_LIMIT_KEYWORDS
    ):
        return _arxiv_rate_limit_error()

    # 3.5 The tool's own classification, when it declared one.
    #
    # Tools that raise a specific, well-understood error write
    # ``{"error_type": …, "suggestion": …}`` at the raise site, where the
    # context is known. That was silently discarded: ``_nodes_tools``
    # rebuilds the ToolMessage from this function, which read only
    # ``payload["error"]``. So summarize_document's "'<id>' is a project
    # id, not a document id" — written as *recoverable* with a concrete next
    # call — matched no keyword and reached the model as **fatal**, which
    # tells the agent not to recover at all. Every hand-written hint in
    # tools_impl.py was dead on arrival the same way.
    #
    # Placed after TOOL_ERROR_HINTS (the curated central overrides keep
    # winning) and after the credential/permission checks. That last ordering
    # is conservatism, not a security property: tool payloads are literals in
    # this repo, the same trust boundary as the hint table, and a tool that
    # wanted to hide an auth failure controls payload["error"] too. The cost
    # is that a tool cannot declare "recoverable" for a message containing
    # the word "permission"; revisit if a real case turns up.
    declared = payload.get("error_type")
    if isinstance(declared, str) and declared in _DECLARABLE_CATEGORIES:
        # Distinct name: ``suggestion`` is already bound as ``str`` by the
        # TOOL_ERROR_HINTS loop above.
        declared_suggestion = payload.get("suggestion")
        return ToolError(
            category=cast(ErrorCategory, declared),
            message=error_msg,
            suggestion=(
                declared_suggestion if isinstance(declared_suggestion, str) else ""
            ),
        )

    # 4. Transient infrastructure / rate-limit errors. See
    # _TRANSIENT_ERROR_KEYWORDS for why connection keywords are scoped and
    # why rate-limit signals are treated as transient.
    if any(kw in msg_lower for kw in _TRANSIENT_ERROR_KEYWORDS):
        return ToolError(category="transient", message=error_msg)

    # 5. Recoverable input-shape errors (LLM can usually retry differently).
    #
    # "<param> is required" belongs here and used to fall through to fatal:
    # a missing argument is the most recoverable failure there is — the model
    # can fetch the value and call again. Observed live: create_project_note
    # wrote a full note, was rejected for a missing project_id, and the fatal
    # classification told the agent not to recover, so the work was discarded
    # (synthetic writing_draft, TOOL-FAILED(create_project_note)).
    if any(
        kw in msg_lower
        for kw in (
            "not found",
            "does not exist",
            "no results",
            "invalid",
            "is required",
            "missing required",
            "must be provided",
        )
    ):
        return ToolError(
            category="recoverable",
            message=error_msg,
            suggestion="Check the input and try again.",
        )

    return ToolError(category="fatal", message=error_msg)


async def retry_transient(
    fn: Callable[[], Awaitable[Any]],
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Retry a coroutine on transient errors with exponential backoff.

    Raises ``ValueError`` for non-positive ``max_attempts`` rather than
    falling through the empty range and re-raising ``None`` (which would
    surface as a confusing ``TypeError: exceptions must derive from
    BaseException``).
    """
    if max_attempts < 1:
        raise ValueError(
            f"retry_transient requires max_attempts >= 1, got {max_attempts}"
        )

    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except _TRANSIENT_EXCEPTIONS as e:
            last_exc = e
            if attempt < max_attempts - 1:
                delay = base_delay * (2**attempt)
                logger.info(
                    "Transient error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    max_attempts,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)
    # ``last_exc`` is non-None here because the only way to exit the loop
    # without ``return`` is by hitting a transient exception that
    # populated it.
    assert last_exc is not None
    raise last_exc
