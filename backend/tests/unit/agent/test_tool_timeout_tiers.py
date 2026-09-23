"""Regression tests for per-tool timeout tier assignment.

Pins the fix for the dev-trace failure mode (traces 019f2a48-9083 /
019f2245-cf9d): ``search_arxiv`` ran under the default 30s tool timeout,
but its worst-case internal path in ``arxiv_service._make_request`` (3s
rate gate + 20s httpx timeout + 2s sleep + a second attempt) exceeds
30s, so every call died with ``TimeoutError`` at exactly 30s and the
agent re-issued the identical query until the tool-loop cap killed the
turn. The tool must sit in the slow tier, and must stay in the
no-outer-retry set so the 120s backstop is not amplified 2x by
``retry_transient``.
"""

from __future__ import annotations

import pytest

from src.services.agent._nodes_tools import (
    _NO_OUTER_RETRY_TOOLS,
    _SLOW_TOOL_TIMEOUT_SECONDS,
    _SLOW_TOOLS,
    TOOL_TIMEOUT_SECONDS,
)


def _resolve_timeout(tool_name: str) -> int:
    """Mirror of the tier selection in ``_execute_single_tool_call``."""
    return (
        _SLOW_TOOL_TIMEOUT_SECONDS if tool_name in _SLOW_TOOLS else TOOL_TIMEOUT_SECONDS
    )


@pytest.mark.unit
class TestToolTimeoutTiers:
    def test_search_arxiv_uses_slow_tier(self):
        # arxiv_service's internal worst case (~45-50s) exceeds the 30s
        # default; the 30s tier guarantees a TimeoutError on slow arXiv days.
        assert _resolve_timeout("search_arxiv") == _SLOW_TOOL_TIMEOUT_SECONDS

    def test_search_arxiv_keeps_single_outer_attempt(self):
        # arxiv_service retries internally; an outer retry on top of the
        # slow tier would amplify worst-case wall clock to ~2x the cap.
        assert "search_arxiv" in _NO_OUTER_RETRY_TOOLS

    def test_destructive_tools_are_never_retried_by_the_outer_wrapper(self):
        assert {
            "ingest_arxiv_papers",
            "create_project",
            "add_document_to_project",
            "create_project_note",
            "create_draft",
            "revise_draft",
            "execute_code",
            "forget_memory",
        } <= _NO_OUTER_RETRY_TOOLS

    def test_ingest_and_draft_tools_stay_slow(self):
        for tool in (
            "ingest_arxiv_papers",
            "create_draft",
            "revise_draft",
            "compare_documents",
        ):
            assert _resolve_timeout(tool) == _SLOW_TOOL_TIMEOUT_SECONDS

    def test_default_tier_unchanged_for_fast_tools(self):
        assert _resolve_timeout("search_documents") == TOOL_TIMEOUT_SECONDS
        assert TOOL_TIMEOUT_SECONDS == 30
