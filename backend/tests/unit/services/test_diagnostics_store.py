"""Unit tests for DiagnosticsStore.

Tests cover:
- store_trace() calls Redis SET with correct key prefix and TTL
- get_trace() deserializes correctly from JSON
- get_recent_traces() uses zrevrange and returns summaries
- get_aggregate_stats() computes averages correctly
- update_trace_evaluation() round-trips
- Graceful handling when Redis is None (writes/single reads degrade; the
  dashboard reads raise DiagnosticsUnavailable so an outage is never
  reported as "no traces")
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.diagnostics.diagnostics_store import (
    TRACE_INDEX_KEY,
    TRACE_KEY_PREFIX,
    TRACE_TTL_SECONDS,
    DiagnosticsStore,
    DiagnosticsUnavailable,
    _index_key,
)
from src.services.diagnostics.retrieval_diagnostics import (
    ContextDiagnostics,
    RetrievalTrace,
    SourceDiagnostics,
)

# ============================================================================
# Factories
# ============================================================================


def _make_trace(**overrides) -> RetrievalTrace:
    """Factory for a RetrievalTrace with sensible defaults."""
    defaults = {
        "trace_id": "trace-001",
        "query": "What is machine learning?",
        "timestamp": "2026-02-17T12:00:00+00:00",
        "total_time_ms": 200.0,
        "sources": [
            SourceDiagnostics(
                source_type="fulltext",
                search_time_ms=50.0,
                result_count=5,
                total_available=20,
                success=True,
            ),
            SourceDiagnostics(
                source_type="vector",
                search_time_ms=80.0,
                result_count=3,
                total_available=15,
                success=True,
            ),
        ],
        "final_result_count": 5,
        "search_type": "hybrid",
    }
    defaults.update(overrides)
    return RetrievalTrace(organization_id="test-org", **defaults)


def _make_sync_redis():
    """Create a mock synchronous Redis client.

    The DiagnosticsStore detects async vs sync Redis by checking
    ``hasattr(client.set, "__await__")``.  A plain ``MagicMock`` method
    does **not** have ``__await__``, so the store follows the sync code
    path -- which is what we want for unit tests that do not actually run
    against a real Redis server.
    """
    client = MagicMock()
    client.set = MagicMock(return_value=True)
    client.get = MagicMock(return_value=None)
    client.zadd = MagicMock(return_value=1)
    client.zrevrange = MagicMock(return_value=[])
    client.zremrangebyrank = MagicMock(return_value=0)
    client.zrangebyscore = MagicMock(return_value=[])
    return client


# ============================================================================
# store_trace() Tests
# ============================================================================


class TestStoreTrace:
    """Tests for DiagnosticsStore.store_trace()."""

    @pytest.mark.asyncio
    async def test_stores_with_correct_key_prefix(self) -> None:
        """store_trace() should use 'diag:trace:{trace_id}' as the Redis key."""
        redis = _make_sync_redis()
        store = DiagnosticsStore(redis_client=redis)
        trace = _make_trace(trace_id="test-trace-123")

        result = await store.store_trace(trace)

        assert result is True
        redis.set.assert_called_once()
        call_args = redis.set.call_args
        key = call_args[0][0]
        assert key == f"{TRACE_KEY_PREFIX}test-trace-123"

    @pytest.mark.asyncio
    async def test_stores_with_correct_ttl(self) -> None:
        """store_trace() should set TTL to 86400 seconds (24 hours)."""
        redis = _make_sync_redis()
        store = DiagnosticsStore(redis_client=redis)
        trace = _make_trace()

        await store.store_trace(trace)

        call_kwargs = redis.set.call_args
        # ex parameter should be TRACE_TTL_SECONDS
        assert call_kwargs.kwargs.get("ex") == TRACE_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_stores_json_data(self) -> None:
        """store_trace() should serialize trace to JSON."""
        redis = _make_sync_redis()
        store = DiagnosticsStore(redis_client=redis)
        trace = _make_trace(trace_id="json-test")

        await store.store_trace(trace)

        call_args = redis.set.call_args[0]
        stored_data = call_args[1]
        parsed = json.loads(stored_data)
        assert parsed["trace_id"] == "json-test"
        assert parsed["query"] == "What is machine learning?"

    @pytest.mark.asyncio
    async def test_adds_to_sorted_index(self) -> None:
        """store_trace() should add trace_id to the sorted set index."""
        redis = _make_sync_redis()
        store = DiagnosticsStore(redis_client=redis)
        trace = _make_trace(trace_id="indexed-trace")

        await store.store_trace(trace)

        redis.zadd.assert_called_once()
        call_args = redis.zadd.call_args[0]
        # The index is per tenant, so it follows the trace's owner.
        assert call_args[0] == _index_key("test-org")
        # Second arg should be a dict with trace_id as key
        assert "indexed-trace" in call_args[1]

    @pytest.mark.asyncio
    async def test_trims_index(self) -> None:
        """store_trace() should trim the index to 1000 entries."""
        redis = _make_sync_redis()
        store = DiagnosticsStore(redis_client=redis)
        trace = _make_trace()

        await store.store_trace(trace)

        redis.zremrangebyrank.assert_called_once_with(_index_key("test-org"), 0, -1001)

    @pytest.mark.asyncio
    async def test_returns_false_when_redis_none(self) -> None:
        """store_trace() should return False when no Redis client is available."""
        store = DiagnosticsStore(redis_client=None)
        with patch.object(
            type(store),
            "redis",
            new_callable=lambda: property(lambda self: None),
        ):
            result = await store.store_trace(_make_trace())
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_redis_error(self) -> None:
        """store_trace() should return False when Redis raises an exception."""
        redis = _make_sync_redis()
        redis.set.side_effect = ConnectionError("Redis unavailable")
        store = DiagnosticsStore(redis_client=redis)

        result = await store.store_trace(_make_trace())

        assert result is False


# ============================================================================
# get_trace() Tests
# ============================================================================


class TestGetTrace:
    """Tests for DiagnosticsStore.get_trace(organization_id="test-org")."""

    @pytest.mark.asyncio
    async def test_returns_trace_from_json(self) -> None:
        """get_trace() should deserialize stored JSON into a RetrievalTrace."""
        trace = _make_trace(trace_id="get-test")
        redis = _make_sync_redis()
        redis.get.return_value = json.dumps(trace.to_dict())

        store = DiagnosticsStore(redis_client=redis)
        result = await store.get_trace("get-test", organization_id="test-org")

        assert result is not None
        assert result.trace_id == "get-test"
        assert result.query == "What is machine learning?"
        assert len(result.sources) == 2

    @pytest.mark.asyncio
    async def test_returns_trace_from_bytes(self) -> None:
        """get_trace() should handle bytes response from Redis."""
        trace = _make_trace(trace_id="bytes-test")
        redis = _make_sync_redis()
        redis.get.return_value = json.dumps(trace.to_dict()).encode("utf-8")

        store = DiagnosticsStore(redis_client=redis)
        result = await store.get_trace("bytes-test", organization_id="test-org")

        assert result is not None
        assert result.trace_id == "bytes-test"

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_trace(self) -> None:
        """get_trace() should return None when the trace does not exist."""
        redis = _make_sync_redis()
        redis.get.return_value = None

        store = DiagnosticsStore(redis_client=redis)
        result = await store.get_trace("nonexistent", organization_id="test-org")

        assert result is None

    @pytest.mark.asyncio
    async def test_uses_correct_key(self) -> None:
        """get_trace() should query Redis with the correct key."""
        redis = _make_sync_redis()
        redis.get.return_value = None

        store = DiagnosticsStore(redis_client=redis)
        await store.get_trace("my-trace-id", organization_id="test-org")

        redis.get.assert_called_once_with(f"{TRACE_KEY_PREFIX}my-trace-id")

    @pytest.mark.asyncio
    async def test_returns_none_when_redis_none(self) -> None:
        """get_trace() should return None when no Redis client is available."""
        store = DiagnosticsStore(redis_client=None)
        with patch.object(
            type(store),
            "redis",
            new_callable=lambda: property(lambda self: None),
        ):
            result = await store.get_trace("any-id", organization_id="test-org")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_redis_error(self) -> None:
        """get_trace() should return None when Redis raises an exception."""
        redis = _make_sync_redis()
        redis.get.side_effect = ConnectionError("Redis unavailable")
        store = DiagnosticsStore(redis_client=redis)

        result = await store.get_trace("error-trace", organization_id="test-org")

        assert result is None


# ============================================================================
# get_recent_traces() Tests
# ============================================================================


class TestGetRecentTraces:
    """Tests for DiagnosticsStore.get_recent_traces(organization_id="test-org")."""

    @pytest.mark.asyncio
    async def test_uses_zrevrange(self) -> None:
        """get_recent_traces() should query the sorted set with zrevrange."""
        redis = _make_sync_redis()
        redis.zrevrange.return_value = []

        store = DiagnosticsStore(redis_client=redis)
        await store.get_recent_traces(limit=10, offset=5, organization_id="test-org")

        redis.zrevrange.assert_called_once_with(_index_key("test-org"), 5, 14)

    @pytest.mark.asyncio
    async def test_returns_summaries(self) -> None:
        """get_recent_traces() should return trace summaries with expected fields."""
        trace = _make_trace(trace_id="summary-test")
        redis = _make_sync_redis()
        redis.zrevrange.return_value = ["summary-test"]
        redis.get.return_value = json.dumps(trace.to_dict())

        store = DiagnosticsStore(redis_client=redis)
        summaries = await store.get_recent_traces(limit=50, organization_id="test-org")

        assert len(summaries) == 1
        summary = summaries[0]
        assert summary["trace_id"] == "summary-test"
        assert summary["query"] == "What is machine learning?"[:100]
        assert summary["timestamp"] == "2026-02-17T12:00:00+00:00"
        assert summary["total_time_ms"] == 200.0
        assert summary["final_result_count"] == 5
        assert summary["search_type"] == "hybrid"
        assert summary["source_count"] == 2
        assert summary["has_evaluation"] is False

    @pytest.mark.asyncio
    async def test_handles_bytes_trace_ids(self) -> None:
        """get_recent_traces() should handle trace IDs returned as bytes."""
        trace = _make_trace(trace_id="bytes-id")
        redis = _make_sync_redis()
        redis.zrevrange.return_value = [b"bytes-id"]
        redis.get.return_value = json.dumps(trace.to_dict())

        store = DiagnosticsStore(redis_client=redis)
        summaries = await store.get_recent_traces(organization_id="test-org")

        assert len(summaries) == 1
        assert summaries[0]["trace_id"] == "bytes-id"

    @pytest.mark.asyncio
    async def test_raises_unavailable_when_redis_none(self) -> None:
        """get_recent_traces() must signal 'unavailable' when Redis is None.

        Contract change: this used to assert ``== []``, which made an outage
        indistinguishable from an empty index and put "No traces captured
        yet" on the dashboard while Redis was down. The intent (no crash, no
        garbage) is kept; the signal is now a typed exception the router maps
        to 503.
        """
        store = DiagnosticsStore(redis_client=None)
        with patch.object(
            type(store),
            "redis",
            new_callable=lambda: property(lambda self: None),
        ):
            with pytest.raises(DiagnosticsUnavailable):
                await store.get_recent_traces(organization_id="test-org")

    @pytest.mark.asyncio
    async def test_raises_unavailable_on_redis_error(self) -> None:
        """A Redis failure is 'unavailable', not an empty list."""
        redis = _make_sync_redis()
        redis.zrevrange.side_effect = ConnectionError("Redis unavailable")
        store = DiagnosticsStore(redis_client=redis)

        with pytest.raises(DiagnosticsUnavailable):
            await store.get_recent_traces(organization_id="test-org")

    @pytest.mark.asyncio
    async def test_empty_index_still_returns_empty_list(self) -> None:
        """A reachable Redis with no traces is genuinely empty, not unavailable."""
        redis = _make_sync_redis()
        redis.zrevrange.return_value = []
        store = DiagnosticsStore(redis_client=redis)

        assert await store.get_recent_traces(organization_id="test-org") == []

    @pytest.mark.asyncio
    async def test_skips_missing_traces(self) -> None:
        """get_recent_traces() should skip trace IDs that no longer exist."""
        redis = _make_sync_redis()
        redis.zrevrange.return_value = ["exists", "deleted"]

        trace_data = _make_trace(trace_id="exists").to_dict()

        def mock_get(key):
            if key == f"{TRACE_KEY_PREFIX}exists":
                return json.dumps(trace_data)
            return None

        redis.get.side_effect = mock_get

        store = DiagnosticsStore(redis_client=redis)
        summaries = await store.get_recent_traces(organization_id="test-org")

        assert len(summaries) == 1
        assert summaries[0]["trace_id"] == "exists"

    @pytest.mark.asyncio
    async def test_has_evaluation_flag(self) -> None:
        """Summary has_evaluation should be True when evaluation_id is set."""
        trace = _make_trace(trace_id="eval-trace", evaluation_id="eval-123")
        redis = _make_sync_redis()
        redis.zrevrange.return_value = ["eval-trace"]
        redis.get.return_value = json.dumps(trace.to_dict())

        store = DiagnosticsStore(redis_client=redis)
        summaries = await store.get_recent_traces(organization_id="test-org")

        assert summaries[0]["has_evaluation"] is True


# ============================================================================
# get_aggregate_stats() Tests
# ============================================================================


class TestGetAggregateStats:
    """Tests for DiagnosticsStore.get_aggregate_stats(organization_id="test-org")."""

    @pytest.mark.asyncio
    async def test_computes_averages(self) -> None:
        """get_aggregate_stats() should compute correct averages."""
        trace1 = _make_trace(trace_id="t1", total_time_ms=100.0, final_result_count=3)
        trace2 = _make_trace(trace_id="t2", total_time_ms=200.0, final_result_count=7)

        redis = _make_sync_redis()
        redis.zrangebyscore.return_value = ["t1", "t2"]

        traces_data = {
            f"{TRACE_KEY_PREFIX}t1": json.dumps(trace1.to_dict()),
            f"{TRACE_KEY_PREFIX}t2": json.dumps(trace2.to_dict()),
        }

        def mock_get(key):
            return traces_data.get(key)

        redis.get.side_effect = mock_get

        store = DiagnosticsStore(redis_client=redis)
        stats = await store.get_aggregate_stats(hours=24, organization_id="test-org")

        assert stats["total_traces"] == 2
        assert stats["avg_time_ms"] == 150.0  # (100 + 200) / 2
        assert stats["avg_result_count"] == 5.0  # (3 + 7) / 2

    @pytest.mark.asyncio
    async def test_source_stats(self) -> None:
        """get_aggregate_stats() should compute per-source stats."""
        trace = _make_trace(trace_id="t1")
        redis = _make_sync_redis()
        redis.zrangebyscore.return_value = ["t1"]
        redis.get.return_value = json.dumps(trace.to_dict())

        store = DiagnosticsStore(redis_client=redis)
        stats = await store.get_aggregate_stats(hours=24, organization_id="test-org")

        assert "fulltext" in stats["source_stats"]
        assert "vector" in stats["source_stats"]
        ft_stats = stats["source_stats"]["fulltext"]
        assert ft_stats["avg_time_ms"] == 50.0
        assert ft_stats["query_count"] == 1

    @pytest.mark.asyncio
    async def test_truncation_stats(self) -> None:
        """get_aggregate_stats() should compute truncation statistics."""
        trace = _make_trace(trace_id="t1")
        trace.context = ContextDiagnostics(
            docs_retrieved=5,
            docs_with_content=4,
            total_chars_before_truncation=10000,
            total_chars_after_truncation=7000,
            truncation_ratio=0.3,
        )
        redis = _make_sync_redis()
        redis.zrangebyscore.return_value = ["t1"]
        redis.get.return_value = json.dumps(trace.to_dict())

        store = DiagnosticsStore(redis_client=redis)
        stats = await store.get_aggregate_stats(hours=24, organization_id="test-org")

        assert stats["truncation_stats"]["avg_ratio"] == 0.3
        assert stats["truncation_stats"]["max_ratio"] == 0.3
        assert stats["truncation_stats"]["traces_with_truncation"] == 1

    @pytest.mark.asyncio
    async def test_source_failure_count(self) -> None:
        """get_aggregate_stats() should count failed sources."""
        trace = _make_trace(trace_id="t1")
        trace.sources[1] = SourceDiagnostics(
            source_type="vector",
            search_time_ms=0.0,
            result_count=0,
            total_available=0,
            success=False,
            error="timeout",
        )
        redis = _make_sync_redis()
        redis.zrangebyscore.return_value = ["t1"]
        redis.get.return_value = json.dumps(trace.to_dict())

        store = DiagnosticsStore(redis_client=redis)
        stats = await store.get_aggregate_stats(hours=24, organization_id="test-org")

        assert stats["source_failure_count"] == 1

    @pytest.mark.asyncio
    async def test_empty_traces_returns_zero_stats(self) -> None:
        """get_aggregate_stats() with no traces should return zero values."""
        redis = _make_sync_redis()
        redis.zrangebyscore.return_value = []

        store = DiagnosticsStore(redis_client=redis)
        stats = await store.get_aggregate_stats(hours=24, organization_id="test-org")

        assert stats["total_traces"] == 0
        assert stats["avg_time_ms"] == 0
        assert stats["source_stats"] == {}

    @pytest.mark.asyncio
    async def test_raises_unavailable_when_redis_none(self) -> None:
        """get_aggregate_stats() must signal 'unavailable' when Redis is None.

        Contract change: this used to assert an ``{"error": ...}`` dict, which
        the router passed through as HTTP 200 and the dashboard ignored
        ("Total queries 0"). The signal is now a typed exception the router
        maps to 503.
        """
        store = DiagnosticsStore(redis_client=None)
        with patch.object(
            type(store),
            "redis",
            new_callable=lambda: property(lambda self: None),
        ):
            with pytest.raises(DiagnosticsUnavailable):
                await store.get_aggregate_stats(organization_id="test-org")

    @pytest.mark.asyncio
    async def test_raises_unavailable_on_redis_error(self) -> None:
        """A Redis failure is 'unavailable', not a 200-shaped {"error": str(e)}."""
        redis = _make_sync_redis()
        redis.zrangebyscore.side_effect = ConnectionError("Redis unavailable")
        store = DiagnosticsStore(redis_client=redis)

        with pytest.raises(DiagnosticsUnavailable) as excinfo:
            await store.get_aggregate_stats(organization_id="test-org")

        # The original cause stays attached for logs, never for the response.
        assert isinstance(excinfo.value.__cause__, ConnectionError)


# ============================================================================
# update_trace_evaluation() Tests
# ============================================================================


class TestUpdateTraceEvaluation:
    """Tests for DiagnosticsStore.update_trace_evaluation()."""

    @pytest.mark.asyncio
    async def test_updates_evaluation_fields(self) -> None:
        """update_trace_evaluation() should set evaluation_id and scores on the trace."""
        trace = _make_trace(trace_id="eval-update")
        redis = _make_sync_redis()
        redis.get.return_value = json.dumps(trace.to_dict())
        redis.set.return_value = True

        store = DiagnosticsStore(redis_client=redis)
        scores = {"precision_at_3": 0.85, "mrr": 0.92}
        result = await store.update_trace_evaluation(
            "eval-update",
            "eval-xyz",
            scores,
            organization_id="test-org",
        )

        assert result is True

        # Verify the stored trace has evaluation data
        stored_call = redis.set.call_args[0]
        stored_data = json.loads(stored_call[1])
        assert stored_data["evaluation_id"] == "eval-xyz"
        assert stored_data["evaluation_scores"]["precision_at_3"] == 0.85
        assert stored_data["evaluation_scores"]["mrr"] == 0.92

    @pytest.mark.asyncio
    async def test_returns_false_for_missing_trace(self) -> None:
        """update_trace_evaluation() should return False if trace is not found."""
        redis = _make_sync_redis()
        redis.get.return_value = None

        store = DiagnosticsStore(redis_client=redis)
        result = await store.update_trace_evaluation(
            "nonexistent",
            "eval-1",
            {"mrr": 0.5},
            organization_id="test-org",
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_round_trip_preserves_original_data(self) -> None:
        """update_trace_evaluation() should preserve existing trace data."""
        trace = _make_trace(trace_id="roundtrip")
        redis = _make_sync_redis()
        redis.get.return_value = json.dumps(trace.to_dict())

        store = DiagnosticsStore(redis_client=redis)
        await store.update_trace_evaluation(
            "roundtrip",
            "eval-rt",
            {"mrr": 0.8},
            organization_id="test-org",
        )

        stored_call = redis.set.call_args[0]
        stored_data = json.loads(stored_call[1])
        assert stored_data["trace_id"] == "roundtrip"
        assert stored_data["query"] == "What is machine learning?"
        assert len(stored_data["sources"]) == 2
        assert stored_data["evaluation_id"] == "eval-rt"
