"""Agent observability and tracing helpers.

Provides custom metrics and tracing for agent execution monitoring.
Dual approach: LangSmith for LangGraph tracing + OpenTelemetry/Prometheus for custom metrics.
"""

import logging
import os
import time
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_PROJECT_SKILL_EVENTS = frozenset(
    {
        "registry_parity",
        "snapshot",
        "scan",
        "proposal",
        "approval",
        "rejection",
        "supersede",
        "rescan",
        "loader",
    }
)
_PROJECT_SKILL_OUTCOMES = frozenset(
    {
        "match",
        "mismatch",
        "success",
        "failure",
        "skipped",
        "rejected",
        "passed",
        "blocked",
        "error",
        "completed",
    }
)

# LangGraph control-flow signals. interrupt() (HITL confirm for destructive
# tools) raises GraphInterrupt, and Send/Command parent-bubbling raises
# ParentCommand; both subclass GraphBubbleUp. They are normal control flow that
# must propagate to the graph runner — NOT node failures. Catching them in the
# node wrapper's generic ``except Exception`` logged a false ERROR and bumped
# AGENT_ERRORS on every HITL confirm. Import is guarded so observability never
# hard-fails if langgraph is absent (metrics are already optional here).
try:
    from langgraph.errors import GraphBubbleUp as _GraphBubbleUp

    _CONTROL_FLOW_EXC: tuple = (_GraphBubbleUp,)
except ImportError:  # pragma: no cover - langgraph always present in app runtime
    _CONTROL_FLOW_EXC = ()

# ---------------------------------------------------------------------------
# LangSmith configuration
# ---------------------------------------------------------------------------

# Map the many ways a deploy env can be spelled onto the canonical suffix used
# in the LangSmith project name. Anything unrecognised (local shells, CI, eval,
# unset) returns None so the caller can fall back instead of inventing a name.
_DEPLOY_ENV_ALIASES = {
    "dev": "dev",
    "development": "dev",
    "staging": "staging",
    "stage": "staging",
    "prod": "prod",
    "production": "prod",
}


# Environments where LangSmith may carry full run I/O. Everything else — every
# deployed environment, and an unset/unknown env — hides it (R7-M8: ``dev`` is
# a LIVE deployment with real user prompts, it was never a laptop).
_IO_VISIBLE_ENVS = frozenset({"local", "test", "testing", "ci"})


def _raw_deploy_env() -> str:
    return (
        (os.environ.get("DEPLOY_ENV") or os.environ.get("ENVIRONMENT") or "")
        .strip()
        .lower()
    )


def _normalize_deploy_env() -> Optional[str]:
    """Resolve DEPLOY_ENV/ENVIRONMENT to a canonical ``dev|staging|prod`` suffix.

    Returns ``None`` for unknown/unset envs (local, test, CI) so trace project
    naming stays deliberate rather than guessing.
    """
    return _DEPLOY_ENV_ALIASES.get(_raw_deploy_env())


def configure_langsmith():
    """Configure LangSmith tracing if an API key is available.

    Accepts either the modern ``LANGSMITH_*`` names (preferred by the current
    ``langsmith`` SDK) or the legacy ``LANGCHAIN_*`` names. Whichever is
    provided, this propagates to both so libraries on either convention pick
    it up.
    """
    api_key = os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")
    if not api_key:
        logger.debug(
            "LangSmith tracing disabled (no LANGSMITH_API_KEY / LANGCHAIN_API_KEY)"
        )
        return

    # A developer's shell often carries a real LangSmith key.  Ordinary
    # pytest runs must still stay local; live perf/eval runs opt in through
    # the explicit perf harness gate.
    if (os.environ.get("ENVIRONMENT") or "").strip().lower() in {
        "test",
        "testing",
    } and (os.environ.get("RUN_PERF_HARNESS") or "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
            os.environ.pop(name, None)
        logger.debug("LangSmith tracing disabled for ordinary pytest execution")
        return

    os.environ.setdefault("LANGSMITH_API_KEY", api_key)
    os.environ.setdefault("LANGCHAIN_API_KEY", api_key)

    # Project name is derived authoritatively from the deploy environment so a
    # stale externally-injected value (e.g. an old Infisical
    # LANGSMITH_PROJECT=rag-agent-dev-local) can't fragment or mis-route traces.
    # For a recognised deploy env we force rag-agent-{dev|staging|prod}; an
    # explicit LANGSMITH_PROJECT_OVERRIDE always wins (escape hatch for one-off
    # namespaces); otherwise we fall back to any provided value or rag-agent-dev.
    norm_env = _normalize_deploy_env()
    override = os.environ.get("LANGSMITH_PROJECT_OVERRIDE")
    if override:
        project = override
    elif norm_env:
        project = f"rag-agent-{norm_env}"
    else:
        project = (
            os.environ.get("LANGSMITH_PROJECT")
            or os.environ.get("LANGCHAIN_PROJECT")
            or "rag-agent-dev"
        )
    # Force (not setdefault) so the derived value overrides a stale injected one.
    os.environ["LANGSMITH_PROJECT"] = project
    os.environ["LANGCHAIN_PROJECT"] = project

    # Both SDK generations read their own flag; set both to "true" by default.
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGSMITH_TRACING", "true")

    # PII guard: do NOT upload run inputs/outputs (user prompts, RAG chunks,
    # tool args) to LangSmith — tags, metadata, latency, and token usage still
    # flow, so dashboards keep working. Only the explicit local/test allowlist
    # keeps I/O visible by default; every deployed environment (dev included —
    # it serves real users) and any unrecognised value hides it. Override with
    # LANGSMITH_HIDE_IO=false to opt back in.
    hide_io_default = "false" if _raw_deploy_env() in _IO_VISIBLE_ENVS else "true"
    hide_io = os.environ.get("LANGSMITH_HIDE_IO", hide_io_default).lower() == "true"
    if hide_io:
        # Force (not setdefault) — same discipline as LANGSMITH_PROJECT above:
        # a stale injected LANGCHAIN_HIDE_INPUTS=false must not outvote the
        # guard while the log line below claims hide_io=True (audit S2-M14).
        # LANGSMITH_HIDE_IO is the single decision knob; explicit opt-out is
        # LANGSMITH_HIDE_IO=false, not per-name env surgery.
        os.environ["LANGCHAIN_HIDE_INPUTS"] = "true"
        os.environ["LANGCHAIN_HIDE_OUTPUTS"] = "true"
        os.environ["LANGSMITH_HIDE_INPUTS"] = "true"
        os.environ["LANGSMITH_HIDE_OUTPUTS"] = "true"

    endpoint = os.environ.get("LANGSMITH_ENDPOINT") or os.environ.get(
        "LANGCHAIN_ENDPOINT"
    )
    if endpoint:
        os.environ.setdefault("LANGSMITH_ENDPOINT", endpoint)
        os.environ.setdefault("LANGCHAIN_ENDPOINT", endpoint)

    logger.info(
        "LangSmith tracing enabled (project: %s, hide_io: %s)", project, hide_io
    )


def get_langsmith_base_url() -> str:
    """Return the configured LangSmith endpoint or the public default."""
    return (
        os.environ.get("LANGSMITH_ENDPOINT")
        or os.environ.get("LANGCHAIN_ENDPOINT")
        or "https://smith.langchain.com"
    )


def tag_trace_intent(intent: str) -> None:
    """Best-effort: tag the actual trace root before it closes."""
    if not intent:
        return
    try:
        from langsmith.run_helpers import get_current_run_tree

        rt = get_current_run_tree()
        if rt is None:
            return
        if getattr(rt, "trace_id", None) is None:
            return

        tag = f"intent:{intent}"
        root = rt
        while getattr(root, "parent_run", None) is not None:
            root = root.parent_run
        if root is rt:
            # langsmith 0.9 stores the parent id on RunTree children without
            # retaining a Python back-reference. LangChain's active callback
            # tracer still owns the live root in its run map.
            try:
                from langchain_core.runnables.config import var_child_runnable_config

                config = var_child_runnable_config.get() or {}
                callbacks = config.get("callbacks")
                trace_id = str(rt.trace_id)
                for handler in getattr(callbacks, "handlers", ()):
                    candidate = getattr(handler, "run_map", {}).get(trace_id)
                    if candidate is not None:
                        root = candidate
                        break
            except Exception:
                pass
        root.add_tags([tag])
    except Exception:
        logger.debug("tag_trace_intent failed", exc_info=True)


# ---------------------------------------------------------------------------
# Prometheus metrics (using existing monitoring infrastructure)
# ---------------------------------------------------------------------------

try:
    from prometheus_client import REGISTRY, Counter, Histogram

    def _get_or_create_counter(name: str, doc: str, labels: list[str]) -> Counter:
        """Return an existing Counter (if already registered) or create one.

        Module re-imports during dev hot-reload would otherwise raise
        ``ValueError: Duplicated timeseries`` from the global registry and
        crash the process on restart.
        """
        existing = getattr(REGISTRY, "_names_to_collectors", {}).get(name)
        if existing is not None:
            return existing  # type: ignore[return-value]
        return Counter(name, doc, labels)

    def _get_or_create_histogram(
        name: str, doc: str, labels: list[str], buckets: list[float]
    ) -> Histogram:
        existing = getattr(REGISTRY, "_names_to_collectors", {}).get(name)
        if existing is not None:
            return existing  # type: ignore[return-value]
        return Histogram(name, doc, labels, buckets=buckets)

    AGENT_EXECUTION_DURATION = _get_or_create_histogram(
        "agent_execution_duration_seconds",
        "Duration of agent graph execution",
        ["intent", "status"],
        [0.5, 1, 2, 5, 10, 30, 60, 120],
    )

    AGENT_NODE_DURATION = _get_or_create_histogram(
        "agent_node_duration_seconds",
        "Duration of an individual agent node execution",
        ["node", "status"],
        [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30],
    )

    AGENT_TOOL_CALLS = _get_or_create_counter(
        "agent_tool_calls_total",
        "Total number of tool calls made by the agent",
        ["tool_name", "status"],
    )

    AGENT_TOKEN_USAGE = _get_or_create_counter(
        "agent_token_usage_total",
        "Total token usage by the agent",
        ["model", "type"],
    )

    AGENT_STREAM_ACCEPTED_DURATION = _get_or_create_histogram(
        "agent_stream_accepted_duration_seconds",
        "Time from request handling start to the accepted SSE event",
        [],
        [0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2],
    )

    AGENT_STREAM_FIRST_TOKEN_DURATION = _get_or_create_histogram(
        "agent_stream_first_token_duration_seconds",
        "Time from request handling start to the first user-facing token",
        ["route"],
        [0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 10, 20, 30],
    )

    AGENT_STREAM_COMPLETION_DURATION = _get_or_create_histogram(
        "agent_stream_completion_duration_seconds",
        "Time from request handling start to the first terminal SSE event",
        ["route"],
        [0.25, 0.5, 1, 2, 3, 5, 8, 10, 20, 30, 60, 120, 300],
    )

    AGENT_STREAM_TURNS = _get_or_create_counter(
        "agent_stream_turns_total",
        "Terminal agent SSE turns by bounded route and outcome",
        ["route", "status"],
    )

    AGENT_STREAM_ROUTES = _get_or_create_counter(
        "agent_stream_routes_total",
        "Agent SSE turns assigned to each bounded execution route",
        ["route"],
    )

    AGENT_ERRORS = _get_or_create_counter(
        "agent_error_total",
        "Total number of agent errors",
        # ``node`` records WHERE the error occurred; ``error_type`` records
        # WHAT class of failure it was (kept separate so dashboards don't
        # conflate location with cause).
        ["node", "error_type"],
    )

    # Distribution across classifier paths. Lets dashboards alert when LLM
    # path drops (e.g. Azure 404s) and keyword fallback share rises.
    AGENT_CLASSIFIER_SOURCE = _get_or_create_counter(
        "agent_classifier_source_total",
        "Intent classifier path (llm/keyword/shortcut/fallback) per intent",
        ["source", "intent"],
    )

    # Tool-level error counter with structured category from
    # error_recovery.classify_error (transient/permanent/auth/validation/
    # not_found/...). Lets us separate "search_arxiv timeout spike" from
    # "create_project auth failure" on the same dashboard.
    AGENT_TOOL_ERRORS = _get_or_create_counter(
        "agent_tool_errors_total",
        "Tool execution errors by tool + category",
        ["tool", "category"],
    )

    # Memory recall hit rate — emits 0 (miss) or 1 (hit, >=1 memory returned)
    # per recall call. Use rate() in Prometheus / Grafana to derive hit-rate %.
    AGENT_MEMORY_RECALL = _get_or_create_counter(
        "agent_memory_recall_total",
        "Memory recall outcomes per call",
        ["outcome"],  # "hit" | "miss"
    )

    # Background assistant-row persistence failures — Task 5 of
    # docs/plans/2026-05-13-agent-persist-perf.md. Bumped by the safe
    # wrapper around _persist_assistant_message when the deferred commit
    # raises. Lets dashboards alert when assistant rows are silently lost
    # off the request hot path.
    agent_assistant_persist_failures_total = _get_or_create_counter(
        "agent_assistant_persist_failures_total",
        "Number of background assistant-row persistence failures",
        [],
    )

    # User-turn persistence failures on the agent hot path (P2.6 / audit D3).
    # The user row is a single idempotent INSERT written BEFORE the LLM call;
    # historically its failure was swallowed (warn-and-continue), so the
    # LangGraph checkpoint could accumulate a turn the chat_messages store
    # never recorded — a permanent divergence the user can't see. Bumped by
    # _persist_user_message_guarded after a retry still fails, so the drop is
    # observable instead of silent.
    agent_dualstore_user_turn_persist_failures_total = _get_or_create_counter(
        "agent_dualstore_user_turn_persist_failures_total",
        "User-turn persistence failures after one retry on the agent hot path",
        [],
    )

    # Detected (not repaired) divergence between the LangGraph checkpoint's
    # HumanMessage count and the persisted chat_messages user-row count for a
    # thread (P2.6 / audit D3). Detection only; re-seed repair is a follow-up.
    agent_dualstore_divergence_detected_total = _get_or_create_counter(
        "agent_dualstore_divergence_detected_total",
        "Threads where checkpoint human-count and chat user-row count diverged",
        [],
    )

    # Quality histogram: max similarity score returned per recall call.
    # Trace evidence showed score=null for every recalled item — once the
    # store has a semantic index wired this histogram surfaces whether
    # recalls actually returned ranked, useful memories.
    AGENT_MEMORY_SCORE = _get_or_create_histogram(
        "agent_memory_relevance_score",
        "Max relevance score returned by memory recall",
        [],
        [0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0],
    )

    # Degraded-answer signal: the agent hit the tool-loop ceiling and was
    # forced to synthesize from partial tool results. A rising rate means
    # MAX_TOOL_LOOPS is too low or a tool is looping — both hurt answer quality.
    AGENT_LOOP_EXHAUSTION = _get_or_create_counter(
        "agent_loop_exhaustion_total",
        "Turns that hit the tool-loop ceiling and were force-synthesized",
        ["intent", "subgraph"],
    )

    # Reflection gate outcome per turn (proceed vs revise). Rising "revise"
    # share means the agent's first answers are increasingly low-quality.
    AGENT_REFLECTION_DECISION = _get_or_create_counter(
        "agent_reflection_decision_total",
        "Reflection-node decision per turn",
        ["decision", "intent"],  # decision: proceed | revise
    )

    # Intent classifier confidence distribution per source. Low-confidence
    # spikes flag misrouting (wrong subgraph) before users complain.
    AGENT_INTENT_CONFIDENCE = _get_or_create_histogram(
        "agent_intent_confidence",
        "Intent classification confidence by classifier source",
        ["source"],
        [0.0, 0.25, 0.5, 0.7, 0.85, 0.95, 1.0],
    )

    # Project-skill rollout events intentionally have only low-cardinality,
    # server-owned dimensions. Never attach a skill name, project/user ID, or
    # instruction text: those values can be sensitive and are not needed for
    # operational rollout decisions.
    PROJECT_SKILL_EVENTS = _get_or_create_counter(
        "project_skill_events_total",
        "Project skill catalog, snapshot, and loader outcomes",
        ["event", "outcome"],
    )
    PROJECT_SKILL_LOADED_SKILLS = _get_or_create_counter(
        "project_skill_loaded_skills_total",
        "Project skill documents loaded into agent turns",
        [],
    )
    PROJECT_SKILL_LOADED_TOKENS = _get_or_create_counter(
        "project_skill_loaded_tokens_total",
        "Estimated project skill instruction tokens loaded into agent turns",
        [],
    )

    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False
    AGENT_STREAM_ACCEPTED_DURATION = None
    AGENT_STREAM_FIRST_TOKEN_DURATION = None
    AGENT_STREAM_COMPLETION_DURATION = None
    AGENT_STREAM_TURNS = None
    AGENT_STREAM_ROUTES = None
    PROJECT_SKILL_EVENTS = None
    PROJECT_SKILL_LOADED_SKILLS = None
    PROJECT_SKILL_LOADED_TOKENS = None
    logger.debug("prometheus_client not available, metrics disabled")


class AgentStreamSLOTracker:
    """Record one low-cardinality SLI lifecycle for an SSE turn.

    The tracker deliberately accepts only four route values. It never records
    tenant, user, thread, prompt, model, tool, or error text as metric labels.
    Duplicate token and terminal frames are ignored so retries/error cleanup
    cannot inflate observations.
    """

    _ROUTES = frozenset({"pending", "luna", "graph", "unknown"})
    _TERMINAL_EVENTS = frozenset({"done", "error", "confirmation"})

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        started_at: Optional[float] = None,
    ) -> None:
        """``started_at`` is a ``clock()`` reading taken by the caller.

        The accepted-latency SLI measures the request's queue-to-first-frame
        time, which includes auth, rate limiting, and body parsing — all of
        which run before this object can exist. Routes therefore stamp the
        clock on handler entry and hand the reading in; defaulting to
        ``clock()`` would silently exclude exactly the overhead the SLI is
        meant to catch.
        """
        self._clock = clock
        self._started_at = clock() if started_at is None else started_at
        self.route = "pending"
        self._accepted_recorded = False
        self._first_token_recorded = False
        # Monotonic reading of the turn's first token frame, retained (not just
        # observed into a histogram) because it is persisted per-message as
        # chat_messages.ttft_ms. Absolute, not a delta: callers subtract their
        # own stream-start reading so the split agrees with latency_ms, which
        # is measured from a later origin than this tracker's started_at.
        self.first_token_at: Optional[float] = None
        self._terminal_recorded = False
        self._route_recorded = False

    def _elapsed(self) -> float:
        return max(0.0, self._clock() - self._started_at)

    def _record_route(self) -> None:
        if (
            self._route_recorded
            or not _METRICS_AVAILABLE
            or AGENT_STREAM_ROUTES is None
        ):
            return
        AGENT_STREAM_ROUTES.labels(route=self.route).inc()
        self._route_recorded = True

    def set_route(self, route: str) -> None:
        self.route = route if route in self._ROUTES else "unknown"
        self._record_route()

    def record(self, event_type: object, data: Dict[str, Any]) -> None:
        event = getattr(event_type, "value", str(event_type))
        # Stamped before the metrics guard: unlike the histogram below, this
        # reading is persisted with the message, so it has to survive a
        # deployment that has no prometheus_client installed. Read the clock
        # once and let the histogram reuse it — sampling twice would make the
        # observed SLI and the persisted ttft_ms disagree by a few micros.
        if event == "token" and self.first_token_at is None:
            self.first_token_at = self._clock()
        if not _METRICS_AVAILABLE:
            return
        if (
            event == "status"
            and data.get("phase") == "accepted"
            and not self._accepted_recorded
            and AGENT_STREAM_ACCEPTED_DURATION is not None
        ):
            AGENT_STREAM_ACCEPTED_DURATION.observe(self._elapsed())
            self._accepted_recorded = True
            return
        if (
            event == "token"
            and not self._first_token_recorded
            and AGENT_STREAM_FIRST_TOKEN_DURATION is not None
        ):
            stamped = self.first_token_at
            AGENT_STREAM_FIRST_TOKEN_DURATION.labels(route=self.route).observe(
                self._elapsed()
                if stamped is None
                else max(0.0, stamped - self._started_at)
            )
            self._first_token_recorded = True
            return
        if event not in self._TERMINAL_EVENTS or self._terminal_recorded:
            return
        self._record_route()
        if AGENT_STREAM_COMPLETION_DURATION is not None:
            AGENT_STREAM_COMPLETION_DURATION.labels(route=self.route).observe(
                self._elapsed()
            )
        if AGENT_STREAM_TURNS is not None:
            AGENT_STREAM_TURNS.labels(route=self.route, status=event).inc()
        self._terminal_recorded = True


def record_project_skill_event(
    event: str,
    outcome: str,
    *,
    loaded_skill_count: int = 0,
    loaded_skill_tokens: int = 0,
    **_unsafe_details: object,
) -> None:
    """Emit safe, low-cardinality project-skill rollout telemetry.

    Callers may have access to instructions, scanner findings, or user-provided
    audit notes. This boundary deliberately ignores arbitrary details so those
    values cannot reach indexed logs or metric labels by accident.
    """
    safe_event = event if event in _PROJECT_SKILL_EVENTS else "unknown"
    safe_outcome = outcome if outcome in _PROJECT_SKILL_OUTCOMES else "unknown"
    safe_count = max(0, int(loaded_skill_count))
    safe_tokens = max(0, int(loaded_skill_tokens))
    logger.info(
        "project_skill_event",
        extra={
            "project_skill_event": safe_event,
            "project_skill_outcome": safe_outcome,
            "loaded_skill_count": safe_count,
            "loaded_skill_tokens": safe_tokens,
        },
    )
    if not _METRICS_AVAILABLE:
        return
    assert PROJECT_SKILL_EVENTS is not None
    PROJECT_SKILL_EVENTS.labels(event=safe_event, outcome=safe_outcome).inc()
    if safe_count:
        assert PROJECT_SKILL_LOADED_SKILLS is not None
        PROJECT_SKILL_LOADED_SKILLS.inc(safe_count)
    if safe_tokens:
        assert PROJECT_SKILL_LOADED_TOKENS is not None
        PROJECT_SKILL_LOADED_TOKENS.inc(safe_tokens)


# ---------------------------------------------------------------------------
# Tracing decorators
# ---------------------------------------------------------------------------


def track_node_execution(node_name: str):
    """Decorator to track node execution time and errors.

    Records both the success and the error paths into Prometheus so node
    latency dashboards have data even when no error is raised.
    """

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            try:
                result = await func(*args, **kwargs)
                duration = time.monotonic() - t0
                logger.debug("Node %s completed in %.2fs", node_name, duration)
                if _METRICS_AVAILABLE:
                    AGENT_NODE_DURATION.labels(
                        node=node_name, status="success"
                    ).observe(duration)
                return result
            except _CONTROL_FLOW_EXC:
                # HITL interrupt / parent-command bubble-up — control flow, not a
                # node failure. Record the latency under a benign status and
                # re-raise so the graph runner can pause/route; do NOT log ERROR
                # or increment AGENT_ERRORS.
                duration = time.monotonic() - t0
                if _METRICS_AVAILABLE:
                    AGENT_NODE_DURATION.labels(
                        node=node_name, status="interrupted"
                    ).observe(duration)
                raise
            except Exception as e:
                duration = time.monotonic() - t0
                # Keep the traceback for operators (exc_info) but keep the raw
                # exception string out of the indexed primary message — it can
                # carry the user query or DB fragments (PII leak into logs).
                logger.error(
                    "Node %s failed after %.2fs", node_name, duration, exc_info=True
                )
                if _METRICS_AVAILABLE:
                    AGENT_NODE_DURATION.labels(node=node_name, status="error").observe(
                        duration
                    )
                    AGENT_ERRORS.labels(
                        node=node_name, error_type=type(e).__name__
                    ).inc()
                raise

        return wrapper

    return decorator


def record_tool_call(tool_name: str, status: str):
    """Record a tool call metric."""
    if _METRICS_AVAILABLE:
        AGENT_TOOL_CALLS.labels(tool_name=tool_name, status=status).inc()


def record_execution_duration(intent: str, status: str, duration: float):
    """Record agent execution duration."""
    if _METRICS_AVAILABLE:
        AGENT_EXECUTION_DURATION.labels(intent=intent, status=status).observe(duration)


def record_node_duration(node: str, status: str, duration: float) -> None:
    """Record a sub-node / phase duration into the shared node histogram.

    Lets phases that don't go through ``track_node_execution`` (e.g. the
    subtasks ``preprocessing_node`` fans out via ``asyncio.gather``, which
    otherwise emit no traced run) surface on the same
    ``agent_node_duration_seconds`` dashboard.
    """
    if _METRICS_AVAILABLE:
        AGENT_NODE_DURATION.labels(node=node, status=status).observe(duration)


def record_token_usage(model: str, prompt_tokens: int, completion_tokens: int):
    """Record token usage metrics."""
    if _METRICS_AVAILABLE:
        AGENT_TOKEN_USAGE.labels(model=model, type="prompt").inc(prompt_tokens)
        AGENT_TOKEN_USAGE.labels(model=model, type="completion").inc(completion_tokens)


def record_loop_exhaustion(intent: str, subgraph: str = "main"):
    """Record a turn that hit the tool-loop ceiling and was force-synthesized."""
    if _METRICS_AVAILABLE:
        AGENT_LOOP_EXHAUSTION.labels(intent=intent, subgraph=subgraph).inc()


def record_reflection_decision(decision: str, intent: str = "unknown"):
    """Record a reflection-node decision (``proceed`` | ``revise``)."""
    if _METRICS_AVAILABLE:
        AGENT_REFLECTION_DECISION.labels(decision=decision, intent=intent).inc()


def record_intent_confidence(source: str, confidence: float):
    """Record the classifier confidence for a routed intent."""
    if _METRICS_AVAILABLE:
        try:
            AGENT_INTENT_CONFIDENCE.labels(source=source).observe(float(confidence))
        except (TypeError, ValueError):
            pass


def record_error(error_type: str, node: str = "unknown"):
    """Record an agent error.

    ``node`` records WHERE the error happened (which graph node) so
    dashboards can pivot by location independently of the error class
    captured in ``error_type``.
    """
    if _METRICS_AVAILABLE:
        AGENT_ERRORS.labels(node=node, error_type=error_type).inc()


def record_classifier_source(source: str, intent: str) -> None:
    """Record which classifier path produced the intent.

    Source values: llm, keyword, shortcut, fallback.
    """
    if _METRICS_AVAILABLE:
        AGENT_CLASSIFIER_SOURCE.labels(source=source, intent=intent).inc()


def record_tool_error(tool: str, category: str) -> None:
    """Record a tool failure with classified category."""
    if _METRICS_AVAILABLE:
        AGENT_TOOL_ERRORS.labels(tool=tool, category=category).inc()


def record_memory_recall(hit: bool, max_score: float | None = None) -> None:
    """Record memory recall outcome + optional max similarity score.

    ``hit`` increments the counter under outcome="hit" or "miss".
    ``max_score`` (when not None) feeds the relevance histogram so
    dashboards can distinguish "we returned 5 memories with score=0.1"
    from "we returned 5 strong matches".
    """
    if not _METRICS_AVAILABLE:
        return
    AGENT_MEMORY_RECALL.labels(outcome="hit" if hit else "miss").inc()
    if max_score is not None:
        AGENT_MEMORY_SCORE.observe(max_score)


# Backwards-compat alias — earlier code paths referenced the old name.
record_memory_retrieval = record_memory_recall


@asynccontextmanager
async def trace_agent_execution(intent: str = "general"):
    """Context manager to trace a full agent execution."""
    t0 = time.monotonic()
    status = "success"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        duration = time.monotonic() - t0
        record_execution_duration(intent, status, duration)
        logger.info(
            "Agent execution complete",
            extra={
                "intent": intent,
                "status": status,
                "duration_seconds": round(duration, 2),
            },
        )
