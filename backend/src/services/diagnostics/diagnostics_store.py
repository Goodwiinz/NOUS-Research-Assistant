"""
Redis-backed store for retrieval diagnostic traces.

Stores traces with a 24-hour TTL and provides retrieval by ID,
recent trace listing, and aggregate statistics.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .retrieval_diagnostics import RetrievalTrace

logger = logging.getLogger(__name__)

TRACE_KEY_PREFIX = "diag:trace:"
TRACE_INDEX_KEY = "diag:traces:index"


def _index_key(organization_id: Optional[str]) -> str:
    """Per-tenant recency index.

    A single global index meant every read returned every tenant's
    traces — and a trace holds the user's raw query. Indexing per tenant
    keeps pagination honest too: filtering a shared index after the fact
    silently returns fewer rows than the caller asked for.
    """
    return f"{TRACE_INDEX_KEY}:{organization_id or 'unscoped'}"


TRACE_TTL_SECONDS = 86400  # 24 hours

# Stable public detail for an unreachable store. Never carries the Redis
# error text: host, port and errno are internal topology (backend/AGENTS.md).
DIAGNOSTICS_UNAVAILABLE_DETAIL = "Diagnostics storage is temporarily unavailable"


class DiagnosticsUnavailable(RuntimeError):
    """The diagnostics store cannot be reached (no client, or Redis failed).

    Raised by the dashboard reads (``get_recent_traces``,
    ``get_aggregate_stats``) instead of returning ``[]`` / ``{"error": ...}``:
    an empty result there means "no traffic", and an outage must not look like
    one. The router maps this to HTTP 503. The original Redis error stays on
    ``__cause__`` for logs only.
    """


class DiagnosticsStore:
    """Stores and retrieves diagnostic traces in Redis."""

    def __init__(self, redis_client=None):
        self._redis = redis_client

    @property
    def redis(self):
        """Lazy access to Redis client, falling back to global if not set."""
        if self._redis is not None:
            return self._redis
        try:
            from src.main import redis_client

            return redis_client
        except ImportError:
            return None

    async def store_trace(self, trace: RetrievalTrace) -> bool:
        """Store a diagnostic trace. Returns True on success."""
        client = self.redis
        if client is None:
            logger.warning("No Redis client available, trace not stored")
            return False

        try:
            key = f"{TRACE_KEY_PREFIX}{trace.trace_id}"
            index_key = _index_key(trace.organization_id)
            data = json.dumps(trace.to_dict())

            # Store trace data with TTL
            if hasattr(client, "set") and hasattr(client.set, "__await__"):
                # Async redis
                await client.set(key, data, ex=TRACE_TTL_SECONDS)
                await client.zadd(
                    index_key,
                    {trace.trace_id: datetime.now(timezone.utc).timestamp()},
                )
                # Trim index to last 1000 entries
                await client.zremrangebyrank(index_key, 0, -1001)
            else:
                # Sync redis
                client.set(key, data, ex=TRACE_TTL_SECONDS)
                client.zadd(
                    index_key,
                    {trace.trace_id: datetime.now(timezone.utc).timestamp()},
                )
                client.zremrangebyrank(index_key, 0, -1001)

            return True
        except Exception as e:
            logger.error(f"Failed to store trace {trace.trace_id}: {e}")
            return False

    async def get_trace(
        self, trace_id: str, *, organization_id: str
    ) -> Optional[RetrievalTrace]:
        """Retrieve a single trace by ID, scoped to the caller's tenant.

        ``organization_id`` is required and keyword-only on purpose: an
        optional one silently disables the boundary for whoever forgets it,
        which is the shape that produced the leak in #1292.

        Returns ``None`` on a tenant mismatch rather than raising, so a
        caller cannot distinguish "another tenant owns this id" from "no such
        id" — the id itself is not a disclosure.
        """
        client = self.redis
        if client is None:
            return None

        try:
            key = f"{TRACE_KEY_PREFIX}{trace_id}"
            if hasattr(client, "get") and hasattr(client.get, "__await__"):
                data = await client.get(key)
            else:
                data = client.get(key)

            if data is None:
                return None

            if isinstance(data, bytes):
                data = data.decode("utf-8")

            trace = RetrievalTrace.from_dict(json.loads(data))
            if trace.organization_id != organization_id:
                # Fail closed, which also hides traces written before the
                # field existed rather than exposing them to every tenant.
                return None
            return trace
        except Exception as e:
            logger.error(f"Failed to get trace {trace_id}: {e}")
            return None

    async def get_recent_traces(
        self,
        *,
        organization_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Get recent trace summaries (trace_id, query, timestamp, total_time_ms).

        Raises ``DiagnosticsUnavailable`` when the store cannot be reached; an
        empty list means Redis answered and holds no traces for this tenant.
        """
        client = self.redis
        if client is None:
            raise DiagnosticsUnavailable("no Redis client configured")

        try:
            index_key = _index_key(organization_id)
            # Get trace IDs from sorted set (most recent first)
            if hasattr(client, "zrevrange") and hasattr(client.zrevrange, "__await__"):
                trace_ids = await client.zrevrange(
                    index_key, offset, offset + limit - 1
                )
            else:
                trace_ids = client.zrevrange(index_key, offset, offset + limit - 1)

            summaries = []
            for tid in trace_ids:
                if isinstance(tid, bytes):
                    tid = tid.decode("utf-8")

                trace = await self.get_trace(tid, organization_id=organization_id)
                if trace:
                    summaries.append(
                        {
                            "trace_id": trace.trace_id,
                            "query": trace.query[:100],
                            "timestamp": trace.timestamp,
                            "total_time_ms": trace.total_time_ms,
                            "final_result_count": trace.final_result_count,
                            "search_type": trace.search_type,
                            "source_count": len(trace.sources),
                            "has_evaluation": trace.evaluation_id is not None,
                        }
                    )

            return summaries
        except Exception as e:
            logger.error(f"Failed to get recent traces: {e}")
            raise DiagnosticsUnavailable("recent traces read failed") from e

    async def get_aggregate_stats(
        self, *, organization_id: str, hours: int = 24
    ) -> Dict[str, Any]:
        """Compute aggregate statistics over recent traces.

        Raises ``DiagnosticsUnavailable`` when the store cannot be reached;
        zeroed stats mean Redis answered and holds no traces in the window.
        """
        client = self.redis
        if client is None:
            raise DiagnosticsUnavailable("no Redis client configured")

        try:
            index_key = _index_key(organization_id)
            cutoff = datetime.now(timezone.utc).timestamp() - (hours * 3600)

            if hasattr(client, "zrangebyscore") and hasattr(
                client.zrangebyscore, "__await__"
            ):
                trace_ids = await client.zrangebyscore(index_key, cutoff, "+inf")
            else:
                trace_ids = client.zrangebyscore(index_key, cutoff, "+inf")

            if not trace_ids:
                return {
                    "period_hours": hours,
                    "total_traces": 0,
                    "avg_time_ms": 0,
                    "source_stats": {},
                    "truncation_stats": {},
                }

            total_time = 0.0
            source_times: Dict[str, List[float]] = {}
            source_failures = 0
            truncation_ratios: List[float] = []
            result_counts: List[int] = []
            # Count traces actually loaded, not index members: index zsets have
            # no TTL and trim only at 1000, while trace bodies expire at
            # TRACE_TTL_SECONDS (24h). With hours up to 168 the index lists ids
            # whose bodies are gone, so len(trace_ids) over-reports total_traces
            # and dilutes avg_time_ms toward zero.
            loaded = 0

            for tid in trace_ids:
                if isinstance(tid, bytes):
                    tid = tid.decode("utf-8")

                trace = await self.get_trace(tid, organization_id=organization_id)
                if not trace:
                    continue

                loaded += 1
                total_time += trace.total_time_ms
                result_counts.append(trace.final_result_count)

                for src in trace.sources:
                    source_times.setdefault(src.source_type, []).append(
                        src.search_time_ms
                    )
                    if not src.success:
                        source_failures += 1

                if trace.context:
                    truncation_ratios.append(trace.context.truncation_ratio)

            count = loaded
            return {
                "period_hours": hours,
                "total_traces": count,
                "avg_time_ms": round(total_time / count, 2) if count else 0,
                "avg_result_count": (
                    round(sum(result_counts) / count, 1) if count else 0
                ),
                "source_stats": {
                    src_type: {
                        "avg_time_ms": round(sum(times) / len(times), 2),
                        "max_time_ms": round(max(times), 2),
                        "query_count": len(times),
                    }
                    for src_type, times in source_times.items()
                },
                "source_failure_count": source_failures,
                "truncation_stats": {
                    "avg_ratio": (
                        round(sum(truncation_ratios) / len(truncation_ratios), 3)
                        if truncation_ratios
                        else 0
                    ),
                    "max_ratio": (
                        round(max(truncation_ratios), 3) if truncation_ratios else 0
                    ),
                    "traces_with_truncation": sum(
                        1 for r in truncation_ratios if r > 0
                    ),
                },
            }
        except Exception as e:
            logger.error(f"Failed to compute aggregate stats: {e}")
            raise DiagnosticsUnavailable("aggregate stats read failed") from e

    async def update_trace_evaluation(
        self,
        trace_id: str,
        evaluation_id: str,
        scores: Dict[str, float],
        organization_id: str,
    ) -> bool:
        """Update a trace with evaluation results (called by background eval)."""
        trace = await self.get_trace(trace_id, organization_id=organization_id)
        if not trace:
            return False

        trace.evaluation_id = evaluation_id
        trace.evaluation_scores = scores
        return await self.store_trace(trace)


# Module-level singleton
diagnostics_store = DiagnosticsStore()
