"""Agent state schema for LangGraph."""

from typing import Annotated, Any, Literal

from langgraph.graph import add_messages
from typing_extensions import TypedDict

THREAD_PERSISTENCE_DURABLE = "durable"
THREAD_PERSISTENCE_EPHEMERAL = "ephemeral"


class AgentState(TypedDict):
    """Full agent state passed through the graph.

    All fields must be provided in the initial state dict passed to
    ``graph.ainvoke()`` / ``graph.astream_events()``.  See
    ``_run_agent_graph`` and ``event_generator`` in ``execute.py``
    for the canonical initial-state construction.
    """

    messages: Annotated[list, add_messages]
    page_context: dict
    retrieved_contexts: list
    # Server-validated document ids for the current turn. The rag node treats
    # a non-empty list as an exact source scope and never widens it to corpus
    # search; an empty list may resolve the latest thread-owned attachments on
    # a retrieval-enabled follow-up.
    attachment_ids: list
    # Prompt-safe readiness records for explicitly requested attachments.
    attachment_status: list
    tool_executions: list
    thread_id: str
    # Server-authored checkpoint provenance. An exact ``ephemeral`` value is
    # the only proof that a confirmation checkpoint intentionally has no
    # durable Thread row; missing/unknown values are treated as durable.
    thread_persistence: Literal["durable", "ephemeral"]
    # preprocessing_node increments once per fresh turn. Plain last-value state
    # prevents compiled specialist subgraphs from adding the value again.
    turn_index: int
    tool_loop_count: int
    error_count: int
    last_error: str
    pending_confirmation: dict
    user_confirmed: bool
    intent: str
    user_memories: list
    project_memories: list  # Durable facts saved for the bound project,
    # recalled across all its threads (list[str])
    # --- v2 additions ---
    plan: list  # [{step, tool, args_hint}] advisory plan
    plan_reasoning: str  # Planner's top-level rationale for `plan`, capped 2000 chars
    reflection_count: int  # Max 2 per turn, reset per user message
    compaction_count: int  # Increments each compaction, reset per turn
    intent_confidence: float  # LLM classifier confidence 0-1
    last_error_info: dict  # {category, message, suggestion}
    user_id: str  # Owner user ID for HITL ownership verification
    current_project_id: str  # UUID of the project the user is currently discussing
    # (extracted from URLs, inherited from page_context,
    # or carried forward across turns via checkpoint)
    model: str  # Per-request Azure deployment override; "" ⇒ server default
    use_rag: bool  # Request-level retrieval contract; False skips rag_node reads
    runtime_snapshot_id: str  # Durable frozen skill/tool metadata for this turn
    project_skill_catalog: list  # Compact model-safe skill metadata only
    loaded_skill_versions: list  # Snapshot-audited versions loaded this turn
    # Reflection result of the latest LLM response; cleared at the start of
    # each turn so a stale value from turn N cannot trigger a spurious
    # revision at the start of turn N+1. Stored as ``Any`` to avoid a
    # circular import on ``ReflectionResult``.
    _reflection_result: Any
    # Set by research_force_synthesis_node so research_should_continue
    # routes a defective synthesis (one that still has tool_calls) to
    # the reflection gate instead of looping back into forced synthesis.
    _force_synthesis_fired: bool
    # Set by tool_node when an entire tool batch was served from the
    # in-turn dedupe cache (no fresh calls ran) — signals the graph to
    # skip the wasted re-plan loop (compactor_node → llm_node, ~8 s on
    # Azure p95) and route straight to force_synthesis_node to produce
    # the final answer from the cached results already in state.
    tools_all_deduped: bool
