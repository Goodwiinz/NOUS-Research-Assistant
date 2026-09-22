"""Unit tests for agent context compactor.

Covers token estimation, ID extraction, compaction candidacy,
validation/fix of compacted output, and the should_compact predicate.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.services.agent.compactor import (
    estimate_tool_message_tokens,
    extract_ids,
    find_compaction_candidates,
    should_compact,
    validate_and_fix_compacted,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_msg(
    content: str, tool_call_id: str = "tc-1", msg_id: str = "m-1"
) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id, id=msg_id)


# ---------------------------------------------------------------------------
# estimate_tool_message_tokens
# ---------------------------------------------------------------------------


class TestEstimateToolMessageTokens:
    def test_basic_estimation(self):
        msgs = [_tool_msg("a" * 400)]  # 400 chars -> ~100 tokens
        result = estimate_tool_message_tokens(msgs)
        assert result == 100

    def test_multiple_messages(self):
        msgs = [
            _tool_msg("a" * 400, tool_call_id="tc-1"),
            _tool_msg("b" * 800, tool_call_id="tc-2"),
        ]
        result = estimate_tool_message_tokens(msgs)
        assert result == 300  # 100 + 200

    def test_empty_list(self):
        assert estimate_tool_message_tokens([]) == 0

    def test_empty_content(self):
        msgs = [_tool_msg("")]
        assert estimate_tool_message_tokens(msgs) == 0


# ---------------------------------------------------------------------------
# extract_ids
# ---------------------------------------------------------------------------


class TestExtractIds:
    def test_extracts_uuids(self):
        text = "Document id: 550e8400-e29b-41d4-a716-446655440000 found."
        uuids, arxiv_ids = extract_ids(text)
        assert uuids == {"550e8400-e29b-41d4-a716-446655440000"}
        assert arxiv_ids == set()

    def test_extracts_arxiv_ids(self):
        text = "Paper 2301.07041 and 2305.12345v2 are relevant."
        uuids, arxiv_ids = extract_ids(text)
        assert uuids == set()
        assert arxiv_ids == {"2301.07041", "2305.12345v2"}

    def test_extracts_both(self):
        text = "UUID: a1b2c3d4-e5f6-7890-abcd-ef1234567890 " "arXiv: 2401.00001v1"
        uuids, arxiv_ids = extract_ids(text)
        assert uuids == {"a1b2c3d4-e5f6-7890-abcd-ef1234567890"}
        assert arxiv_ids == {"2401.00001v1"}

    def test_empty_text(self):
        uuids, arxiv_ids = extract_ids("")
        assert uuids == set()
        assert arxiv_ids == set()

    def test_no_matches(self):
        uuids, arxiv_ids = extract_ids("no special ids here")
        assert uuids == set()
        assert arxiv_ids == set()

    def test_multiple_uuids(self):
        text = (
            "550e8400-e29b-41d4-a716-446655440000 "
            "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
        )
        uuids, _ = extract_ids(text)
        assert len(uuids) == 2


# ---------------------------------------------------------------------------
# should_compact
# ---------------------------------------------------------------------------


class TestShouldCompact:
    def test_below_threshold_returns_false(self):
        msgs = [_tool_msg("a" * 100)]  # 25 tokens
        assert should_compact(msgs, compaction_count=0, threshold=8000) is False

    def test_above_threshold_returns_true(self):
        msgs = [_tool_msg("a" * 40000)]  # 10000 tokens > 8000
        assert should_compact(msgs, compaction_count=0, threshold=8000) is True

    def test_exact_threshold_returns_false(self):
        # 8000 * 4 = 32000 chars -> exactly 8000 tokens, not *exceeding*
        msgs = [_tool_msg("a" * 32000)]
        assert should_compact(msgs, compaction_count=0, threshold=8000) is False

    def test_custom_threshold(self):
        msgs = [_tool_msg("a" * 400)]  # 100 tokens
        assert should_compact(msgs, compaction_count=0, threshold=50) is True


# ---------------------------------------------------------------------------
# find_compaction_candidates
# ---------------------------------------------------------------------------


class TestFindCompactionCandidates:
    def test_protects_latest_batch(self):
        """Latest AIMessage's tool_calls and their ToolMessages are protected."""
        messages = [
            HumanMessage(content="hello"),
            AIMessage(
                content="",
                tool_calls=[{"id": "tc-old", "name": "search", "args": {}}],
            ),
            _tool_msg("old result", tool_call_id="tc-old", msg_id="m-old"),
            AIMessage(
                content="",
                tool_calls=[{"id": "tc-new", "name": "search", "args": {}}],
            ),
            _tool_msg("new result", tool_call_id="tc-new", msg_id="m-new"),
        ]
        candidates = find_compaction_candidates(messages)
        assert len(candidates) == 1
        assert candidates[0].id == "m-old"

    def test_skips_already_compacted(self):
        """Messages flagged as compaction output are not candidates.

        Flagged via ``additional_kwargs`` — the ``[Compacted]`` prefix alone
        is attacker-settable text (R7-L5) and no longer exempts anything.
        """
        messages = [
            HumanMessage(content="hello"),
            AIMessage(
                content="",
                tool_calls=[{"id": "tc-1", "name": "search", "args": {}}],
            ),
            ToolMessage(
                content="[Compacted] summary",
                tool_call_id="tc-1",
                id="m-1",
                additional_kwargs={"compacted": True},
            ),
            AIMessage(
                content="",
                tool_calls=[{"id": "tc-2", "name": "search", "args": {}}],
            ),
            _tool_msg("latest result", tool_call_id="tc-2", msg_id="m-2"),
        ]
        candidates = find_compaction_candidates(messages)
        assert len(candidates) == 0

    def test_no_ai_messages_returns_empty(self):
        messages = [
            HumanMessage(content="hello"),
            _tool_msg("orphan tool msg", tool_call_id="tc-1", msg_id="m-1"),
        ]
        candidates = find_compaction_candidates(messages)
        assert len(candidates) == 0

    def test_single_ai_with_tools_returns_empty(self):
        """Only one AIMessage with tool_calls — all its ToolMessages are protected."""
        messages = [
            HumanMessage(content="hello"),
            AIMessage(
                content="",
                tool_calls=[{"id": "tc-1", "name": "search", "args": {}}],
            ),
            _tool_msg("result", tool_call_id="tc-1", msg_id="m-1"),
        ]
        candidates = find_compaction_candidates(messages)
        assert len(candidates) == 0

    def test_multiple_old_batches(self):
        """Multiple older batches are all candidates."""
        messages = [
            HumanMessage(content="hello"),
            AIMessage(
                content="",
                tool_calls=[{"id": "tc-1", "name": "search", "args": {}}],
            ),
            _tool_msg("result 1", tool_call_id="tc-1", msg_id="m-1"),
            AIMessage(
                content="",
                tool_calls=[{"id": "tc-2", "name": "search", "args": {}}],
            ),
            _tool_msg("result 2", tool_call_id="tc-2", msg_id="m-2"),
            AIMessage(
                content="",
                tool_calls=[{"id": "tc-3", "name": "search", "args": {}}],
            ),
            _tool_msg("result 3", tool_call_id="tc-3", msg_id="m-3"),
        ]
        candidates = find_compaction_candidates(messages)
        candidate_ids = {c.id for c in candidates}
        assert candidate_ids == {"m-1", "m-2"}


# ---------------------------------------------------------------------------
# validate_and_fix_compacted
# ---------------------------------------------------------------------------


class TestValidateAndFixCompacted:
    def test_all_ids_present_no_change(self):
        original_ids = (
            {"550e8400-e29b-41d4-a716-446655440000"},
            {"2301.07041"},
        )
        compacted = (
            "Summary mentioning 550e8400-e29b-41d4-a716-446655440000 " "and 2301.07041"
        )
        result = validate_and_fix_compacted(compacted, original_ids)
        assert result == compacted

    def test_missing_uuid_appended(self):
        original_ids = (
            {"550e8400-e29b-41d4-a716-446655440000"},
            set(),
        )
        compacted = "Summary without the UUID"
        result = validate_and_fix_compacted(compacted, original_ids)
        assert "550e8400-e29b-41d4-a716-446655440000" in result
        assert "Referenced IDs:" in result

    def test_missing_arxiv_id_appended(self):
        original_ids = (set(), {"2301.07041"})
        compacted = "Summary without the arXiv ID"
        result = validate_and_fix_compacted(compacted, original_ids)
        assert "2301.07041" in result
        assert "Referenced IDs:" in result

    def test_empty_original_ids_no_change(self):
        compacted = "Simple summary"
        result = validate_and_fix_compacted(compacted, (set(), set()))
        assert result == compacted

    def test_multiple_missing_ids(self):
        original_ids = (
            {"aaa-bbb-ccc-ddd-eee", "111-222-333-444-555"},
            {"2401.00001"},
        )
        compacted = "Summary"
        result = validate_and_fix_compacted(compacted, original_ids)
        assert "aaa-bbb-ccc-ddd-eee" in result
        assert "111-222-333-444-555" in result
        assert "2401.00001" in result
