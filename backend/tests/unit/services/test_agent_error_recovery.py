"""Tests for structured error recovery module."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest


@pytest.mark.unit
class TestClassifyError:
    def test_timeout_is_transient(self):
        from src.services.agent.error_recovery import classify_error

        err = classify_error("search_arxiv", asyncio.TimeoutError())
        assert err.category == "transient"

    def test_connection_error_is_transient(self):
        from src.services.agent.error_recovery import classify_error

        err = classify_error("search_arxiv", ConnectionError("reset"))
        assert err.category == "transient"

    def test_not_found_payload_is_recoverable(self):
        from src.services.agent.error_recovery import classify_error_from_payload

        err = classify_error_from_payload(
            "add_document_to_project", {"error": "Document abc123 not found"}
        )
        assert err.category == "recoverable"
        assert "ingest" in err.suggestion.lower()

    def test_permission_denied_is_user_fixable(self):
        from src.services.agent.error_recovery import classify_error

        err = classify_error("search_documents", PermissionError("org mismatch"))
        assert err.category == "user_fixable"

    def test_generic_exception_is_fatal(self):
        from src.services.agent.error_recovery import classify_error

        err = classify_error("search_arxiv", RuntimeError("unexpected"))
        assert err.category == "fatal"

    def test_exception_details_are_not_returned_to_tool_clients(self):
        from src.services.agent.error_recovery import (
            classify_error_from_payload,
            tool_error_payload,
        )

        secret = "postgresql://user:password@internal-db/app"
        payload = tool_error_payload("search_documents", RuntimeError(secret))

        assert secret not in json.dumps(payload)
        assert payload["error_type"] == "fatal"

        timeout = tool_error_payload(
            "ingest_arxiv_papers", RuntimeError("timed out at internal host")
        )
        assert (
            classify_error_from_payload("ingest_arxiv_papers", timeout).category
            == "transient"
        )

    def test_hint_for_known_tool_error(self):
        from src.services.agent.error_recovery import classify_error_from_payload

        err = classify_error_from_payload(
            "ingest_arxiv_papers", {"error": "timed out after 120s"}
        )
        assert err.category == "transient"

    def test_arxiv_cooldown_survives_error_payload_round_trip(self) -> None:
        from src.services.agent.error_recovery import (
            classify_error_from_payload,
            tool_error_payload,
        )

        payload = tool_error_payload(
            "search_arxiv",
            RuntimeError(
                "ArXiv rate limited (HTTP 429). Try again in 60 seconds. secret"
            ),
        )
        result = classify_error_from_payload("search_arxiv", payload).to_payload()
        assert result["error_type"] == "transient"
        assert "60 seconds" in result["error"]
        assert "Do not retry arXiv this turn" in result["suggestion"]
        assert "secret" not in json.dumps(result)

    def test_arxiv_406_is_distinct_safe_unavailability(self) -> None:
        from src.services.agent.error_recovery import (
            classify_error_from_payload,
            tool_error_payload,
        )

        payload = tool_error_payload(
            "search_arxiv",
            RuntimeError("ArXiv search unavailable (HTTP 406): secret upstream body"),
        )
        result = classify_error_from_payload("search_arxiv", payload).to_payload()

        assert result["error_type"] == "transient"
        assert result["error"] == "ArXiv search is temporarily unavailable."
        assert "rate limit" not in result["error"].lower()
        assert "Do not retry arXiv this turn" in result["suggestion"]
        assert "secret" not in json.dumps(result)

    def test_no_results_is_recoverable(self):
        from src.services.agent.error_recovery import classify_error_from_payload

        err = classify_error_from_payload("search_arxiv", {"error": "No results found"})
        assert err.category == "recoverable"
        assert "broader" in err.suggestion.lower()


@pytest.mark.unit
class TestToolErrorFormat:
    def test_to_tool_message_content(self):
        from src.services.agent.error_recovery import ToolError

        err = ToolError(
            category="recoverable",
            message="Document not found",
            suggestion="Ingest it first",
        )
        content = err.to_tool_message_content()
        parsed = json.loads(content)
        assert parsed["error"] == "Document not found"
        assert parsed["error_type"] == "recoverable"
        assert parsed["suggestion"] == "Ingest it first"


@pytest.mark.unit
class TestRetryTransient:
    @pytest.mark.asyncio
    async def test_retries_on_transient_then_succeeds(self):
        from src.services.agent.error_recovery import retry_transient

        call_count = 0

        async def flaky_fn():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise asyncio.TimeoutError()
            return {"result": "ok"}

        result = await retry_transient(flaky_fn, max_attempts=3, base_delay=0.01)
        assert result == {"result": "ok"}
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self):
        from src.services.agent.error_recovery import retry_transient

        async def always_fails():
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            await retry_transient(always_fails, max_attempts=3, base_delay=0.01)

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates_immediately(self):
        """CancelledError must never be retried — it means the caller aborted."""
        from src.services.agent.error_recovery import retry_transient

        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await retry_transient(fn, max_attempts=3, base_delay=0.01)

        assert call_count == 1


@pytest.mark.unit
class TestConnectionKeywordScoping:
    """The transient match must be scoped to connection-FAILURE phrases.

    A bare "connection" substring used to match benign payload text like
    "no connection found between entities" (a KG tool result), triggering a
    retry of a non-transient error. The scoped list was then re-widened once
    because it missed the canonical requests/urllib3 failure strings — both
    directions are pinned here so neither regression can recur silently.
    """

    def test_benign_connection_text_is_not_transient(self):
        from src.services.agent.error_recovery import classify_error_from_payload

        err = classify_error_from_payload(
            "find_entity_paths",
            {"error": "no connection found between entities"},
        )
        assert err.category != "transient"

    @pytest.mark.parametrize(
        "message",
        [
            "connection refused by host",
            "connection reset by peer",
            "connection error while contacting Qdrant",
            "connection closed unexpectedly",
            "connection failed after 3 attempts",
            "('Connection aborted.', RemoteDisconnected('Remote end closed'))",
            "ECONNREFUSED 127.0.0.1:7687",
            "ECONNRESET while reading response",
        ],
    )
    def test_connection_failure_strings_are_transient(self, message: str):
        from src.services.agent.error_recovery import classify_error_from_payload

        err = classify_error_from_payload("search_documents", {"error": message})
        assert err.category == "transient"


@pytest.mark.unit
class TestRateLimitIsTransient:
    """Rate-limit errors (429 / "rate limit" / "too many requests") must be
    transient, not fatal.

    A fatal classification burns the error-ceiling counter (error_increment=1)
    and, because the reflection gate only skips on a transient failure, lets a
    rate-limited turn trigger a wasteful revise loop — observed as two answers
    streamed back-to-back and concatenated in the client bubble. The canonical
    arXiv shape is "Ingestion failed: ArXiv rate limited (HTTP 429). ...".
    """

    @pytest.mark.parametrize(
        "message",
        [
            "Ingestion failed: ArXiv rate limited (HTTP 429). Try again in 60 seconds.",
            "rate limit exceeded",
            "HTTP 429 Too Many Requests",
            "Too Many Requests",
        ],
    )
    def test_rate_limit_payload_is_transient(self, message: str):
        from src.services.agent.error_recovery import classify_error_from_payload

        err = classify_error_from_payload("ingest_arxiv_papers", {"error": message})
        assert err.category == "transient"

    def test_rate_limit_exception_is_transient(self):
        from src.services.agent.error_recovery import classify_error

        err = classify_error(
            "ingest_arxiv_papers",
            RuntimeError("ArXiv rate limited (HTTP 429). Try again in 60 seconds."),
        )
        assert err.category == "transient"

    @pytest.mark.parametrize(
        "message",
        [
            "Paper 2304.04290 not found",
            "entity e429 does not exist",
            "4290 records rejected — invalid input",
        ],
    )
    def test_bare_429_digits_are_not_transient(self, message: str):
        """A 429 buried in an arXiv id / count / year must NOT read as a rate
        limit — only an anchored "http 429" (or rate-limit wording) counts."""
        from src.services.agent.error_recovery import classify_error_from_payload

        err = classify_error_from_payload("search_arxiv", {"error": message})
        assert err.category != "transient"

    def test_transient_429_does_not_burn_error_ceiling(self):
        """A transient classification must yield error_increment=0 downstream.

        _execute_single_tool sets error_increment = 1 only when the category is
        NOT transient; assert the category here so that contract holds.
        """
        from src.services.agent.error_recovery import classify_error_from_payload

        err = classify_error_from_payload(
            "ingest_arxiv_papers",
            {"error": "ArXiv rate limited (HTTP 429)."},
        )
        assert err.category == "transient"

    def test_none_error_value_does_not_crash_the_classifier(self):
        """B8-S2: `payload.get("error", "")` returned None for `{"error": None}`
        and `.lower()` raised AttributeError inside the classifier itself."""
        from src.services.agent.error_recovery import classify_error_from_payload

        err = classify_error_from_payload("extract_entities", {"error": None})
        assert err.category
