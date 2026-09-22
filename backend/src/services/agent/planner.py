"""Adaptive planner for multi-step agent queries.

Generates an execution plan when the query requires three or more tool
calls, using a single structured-output LLM call that both assesses
complexity and produces the plan (simple queries come back with an empty
``steps`` list). Uses structured output from an LLM to produce validated
Pydantic models.
"""

import asyncio
import logging
import re
from typing import Any, Callable, Dict, List, Union

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from src.services.agent._sanitize import _sanitize_prompt_field, sanitize_page_context
from src.services.agent.llm_factory import build_lightweight_llm
from src.services.agent.trace_metadata import internal_llm_config

logger = logging.getLogger(__name__)


ACTIONABLE_VERBS: frozenset[str] = frozenset(
    {
        "add",
        "ingest",
        "import",
        "save",
        "find",
        "search",
        "summarize",
        "summarise",
        "grab",
        "get",
        "show",
        "fetch",
        "download",
        "extract",
        "list",
        "create",
    }
)
_CONVERSATIONAL_STARTS: frozenset[str] = frozenset(
    {
        "hi",
        "hello",
        "hey",
        "thanks",
        "thank",
        "ok",
        "okay",
        "yes",
        "no",
        "sure",
        "what",
        "who",
        "when",
        "where",
        "why",
        "is",
        "are",
        "can",
        "could",
        "would",
        "will",
    }
)
_LEADING_PUNCTUATION = "?!,:"
_ARXIV_ID_RE = re.compile(r"\b\d{4}\.\d{4,5}\b")
# Trace 019e69f4: complexity LLM returned >=3 for "Add arXiv X to project Y",
# triggering a 25s generate_plan call on the full model-router deployment.
# The research LLM handles this in 1-2 tool rounds without a formal plan.
_SIMPLE_ADD_TARGET_RE = re.compile(r"\b(project|library|collection)\b", re.IGNORECASE)

# Wall-clock cap for the planner LLM call. The planner is purely advisory —
# on timeout it returns {} and the turn proceeds without a plan. The
# lightweight model's normal latency is ~2-3s (p95≈8s); 8s is ample
# headroom. The planner makes a single LLM call per turn (complexity
# gating is folded into plan generation), so worst-case is 8s flat.
PLANNER_LLM_TIMEOUT_SECONDS = 8


def _is_simple_add_flow(query: str) -> bool:
    """Return True for single-target ingest/add imperatives.

    Example: "Add arXiv 2401.12345 to project My Project"
    """
    words = query.split()
    if not words:
        return False
    first_word = words[0].lower().rstrip(_LEADING_PUNCTUATION)
    if first_word not in {"add", "ingest", "import", "save"}:
        return False
    if not _ARXIV_ID_RE.search(query):
        return False
    if not _SIMPLE_ADD_TARGET_RE.search(query):
        return False
    return len(words) <= 15


# Patterns that signal multi-step intent in writing queries — presence of any
# of these means the planner should run rather than skipping.
_WRITING_CONJUNCTION_RE = re.compile(
    r"\b(and|then|also|additionally|plus|as well as|followed by|after that)\b",
    re.IGNORECASE,
)
# Leading verbs that indicate a single-tool writing action.
_SIMPLE_WRITING_VERBS: frozenset[str] = frozenset(
    {"summarize", "summarise", "create", "draft", "write"}
)
_CONTEXTUAL_WRITING_PREFIX_RE = re.compile(r"^(?:based on|using|from)\b", re.IGNORECASE)
_WRITING_VERB_RE = re.compile(
    r"\b(?:summarize|summarise|create|draft|write)\b", re.IGNORECASE
)


def _is_simple_writing_flow(query: str) -> bool:
    """Return True for obviously single-tool writing imperatives.

    Deliberately narrow — only skip when the request is clearly a one-step
    action with a single target. When in doubt, return False so the planner
    still runs.

    True examples:
      "summarize this document"
      "create a note about the results"
      "draft an intro"
      "write a note on section 3"

    False examples (multi-step or ambiguous):
      "summarize X and compare it with Y"   — conjunction implies two steps
      "draft a section then add citations"  — sequential conjunction
      "what should I write?"                — question, not imperative
      ""                                    — empty
    """
    words = query.split()
    if not words:
        return False

    first_word = words[0].lower().rstrip(_LEADING_PUNCTUATION)
    direct_imperative = first_word in _SIMPLE_WRITING_VERBS
    contextual_imperative = bool(
        _CONTEXTUAL_WRITING_PREFIX_RE.search(query) and _WRITING_VERB_RE.search(query)
    )
    if not direct_imperative and not contextual_imperative:
        return False

    # Questions starting with a writing verb ("what should I write?") are not
    # imperatives — reject if the query contains a "?" or starts ambiguously.
    if "?" in query:
        return False

    # Any coordinating/sequential conjunction signals multiple steps — don't skip.
    if _WRITING_CONJUNCTION_RE.search(query):
        return False

    # Comma-separated clauses can also imply multiple actions
    # ("summarize doc1, add a note, then export"). A single comma is fine
    # (e.g. "write a note on section 3, page 5"), but two or more suggest
    # a list of steps — be conservative and let the planner handle them.
    if query.count(",") >= 2:
        return False

    # Long queries almost certainly contain multiple intents.
    if len(words) > 20:
        return False

    return True


def _is_grounded_summary_flow(query: str) -> bool:
    """Return True when retrieved context can answer a simple summary directly."""
    return _is_simple_writing_flow(query) and bool(
        re.search(r"\bsummari[sz]e\b", query, re.IGNORECASE)
    )


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PlanStep(BaseModel):
    """A single step in an agent execution plan."""

    step: int
    description: str
    # ``tool`` is conceptually required but LLMs occasionally emit a final
    # "summarize / present results" step with no tool. Default to "" so the
    # whole plan doesn't fail validation; downstream consumers already treat
    # the plan as advisory and gracefully ignore empty tool names.
    tool: str = ""
    # Accept dict (preferred, structured) or str (LLM descriptive form).
    # Observed planner traces (019e1554) showed gpt-5 returning args_hint as
    # a string like "query='X'; max_results=5" which failed strict dict
    # validation with 5 errors. Accept both; downstream consumers normalize.
    args_hint: Union[dict, str] = Field(default_factory=dict)
    depends_on: list[int] = Field(default_factory=list)


class AgentPlan(BaseModel):
    """Full execution plan produced by the planner LLM."""

    steps: list[PlanStep]
    # Some models omit the top-level reasoning field even when explicitly
    # asked for it. Don't fail the whole plan over a missing rationale.
    reasoning: str = ""


# ---------------------------------------------------------------------------
# LLM construction (mirrors graph.py _build_llm with configurable model)
# ---------------------------------------------------------------------------


def _build_planner_llm(max_tokens: int = 2048):
    """Build a lightweight LLM for planner structured-output calls.

    tool_calling=True because the plan is extracted via
    ``with_structured_output(AgentPlan, method="function_calling")`` — that
    ships a real function tool, which Azure will not accept alongside
    reasoning_effort.
    """
    return build_lightweight_llm(max_tokens=max_tokens, tool_calling=True)


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


async def generate_plan(
    query: str, tool_names: list[str], page_context: dict
) -> AgentPlan:
    """Generate an execution plan for a query in a single LLM call.

    Complexity gating is folded into this call: the prompt instructs the
    model to return an EMPTY ``steps`` list when the query needs fewer
    than three tool calls, so no separate complexity-estimation call is
    needed (dev traces showed the old two-call design costing ~5s of
    sequential planner latency per turn).

    Uses the lightweight deployment with structured output. Trace 019e69f4
    showed the full model-router call stalling ~25s on a simple add-to-project
    query; gpt-5-mini handles plan JSON fine and stays inside the HTTP budget.
    Returns a validated AgentPlan with ordered steps.
    """
    llm = _build_planner_llm(max_tokens=4096)
    structured_llm = llm.with_structured_output(AgentPlan, method="function_calling")

    safe_query = _sanitize_prompt_field(query)
    # Recursive: the top-level-only comprehension this replaced embedded the
    # free-form ``metadata`` dict raw (R7-H1).
    safe_page_context = sanitize_page_context(page_context)
    prompt = (
        "Given the user query and available tools, generate a step-by-step "
        "execution plan.\n\n"
        f"Available tools: {', '.join(tool_names)}\n"
        f"Page context: {safe_page_context}\n\n"
        f"User query: {safe_query}\n\n"
        "FIRST assess complexity: if the query can be answered with FEWER "
        "than three tool calls, return an empty steps list (steps: []) and "
        "nothing else — simple queries need no plan.\n\n"
        "Otherwise, for each step, specify:\n"
        "- step: sequential step number starting at 1\n"
        "- description: what this step does\n"
        "- tool: which tool to use — MUST be one of the available tools "
        "listed above; do not invent tool names; do not leave blank. "
        "If a step is purely summarisation with no tool call, omit it "
        "from the plan entirely.\n"
        "- args_hint: suggested arguments (can reference prior steps)\n"
        "- depends_on: list of step numbers this step depends on\n\n"
        "Ordering rule: data must exist before it is used. If the user refers "
        "to papers by title or arXiv id, the FIRST steps must resolve/ingest "
        "them (e.g. search_arxiv → ingest_arxiv_papers) BEFORE any step that "
        "summarizes, drafts, or saves notes about them — a write step must "
        "depend_on the resolve/ingest steps.\n"
        "create_draft returns pending and MUST be the final planned step with "
        "no same-turn dependents. revise_draft is synchronous and may have "
        "later reporting steps.\n"
        "Also provide a top-level ``reasoning`` string explaining the "
        "overall approach (required)."
    )

    result = await structured_llm.ainvoke(
        [HumanMessage(content=prompt)], config=internal_llm_config()
    )
    return result


_PLAN_LINE_BREAK_RE = re.compile(r"[\r\n\x0b\x0c\x85\u2028\u2029]")


def _one_line(value: str, max_chars: int = 400) -> str:
    """Cap + collapse line breaks, no brace escaping.

    The plan directive is rendered straight into a SystemMessage (never
    str.format), so doubling braces would only corrupt dict-shaped
    args_hint values (review on #1595).
    """
    text = str(value or "")
    if len(text) > max_chars:
        text = text[:max_chars] + "..."
    return _PLAN_LINE_BREAK_RE.sub(" ", text)


def render_plan_directive(plan: list[dict] | None) -> str | None:
    """Render the planner's plan into an execution directive for an executor LLM.

    The planner writes its plan to ``state["plan"]``, but the executor LLM
    nodes historically only ever read ``state["messages"]`` — so the plan was
    consumed only by the reflection gate and ledger and never reached the
    model that acts. Trace 019ea8f0 showed the planner correctly choosing
    ``create_draft`` → ``create_project_note`` while the executor ignored it
    and refused, demanding document_id / arXiv IDs the user can't supply.
    Injecting this directive into the executor prompt closes the
    plan→execute handoff. Shared by writing/research/data subgraphs and the
    main ``llm_node`` so the four executors behave consistently.
    """
    if not plan:
        return None
    # R7-L2: description / tool / args_hint are model-emitted and can echo
    # injected document text. Sanitising each rendered string keeps them on a
    # single line so nothing can impersonate a prompt section (execution is
    # already tool-allowlisted, so this is the whole fix).
    lines: list[str] = []
    for step in plan:
        tool = _one_line((step.get("tool") or "").strip())
        desc = _one_line((step.get("description") or "").strip())
        if not desc:
            continue
        if tool and tool.upper() != "N/A":
            args = step.get("args_hint")
            arg_str = f"  args: {_one_line(str(args))}" if args else ""
            lines.append(f"{step.get('step', '?')}. [{tool}] {desc}{arg_str}")
        else:
            lines.append(f"{step.get('step', '?')}. {desc}")
    if not lines:
        return None
    plan_block = "\n".join(lines)
    return (
        "ACTIVE PLAN (produced by the planner for this turn — advisory, follow "
        "its intent). Execute the next incomplete step now by emitting the "
        "appropriate tool call. Do NOT ask the user for document_ids or arXiv "
        "IDs that you can resolve yourself via the available ingest/search "
        "tools. IMPORTANT: a write/create step (e.g. create_draft, "
        "create_project_note, summarize_document) requires its source "
        "documents to already be resolved + ingested — if the plan lists a "
        "write step before the sources exist, run the search/ingest steps "
        "FIRST, then the write. Never write a note/draft/summary for a paper "
        "you only have a title for. create_draft returns pending, so it is "
        "terminal for this turn; do not execute later planned steps. "
        "revise_draft is synchronous.\n"
        f"{plan_block}"
    )


# ---------------------------------------------------------------------------
# Graph node factory
# ---------------------------------------------------------------------------


def make_planner_node(
    tool_names: list[str],
) -> Callable:
    """Return a LangGraph node function that conditionally plans.

    The returned async function:
    1. Skips if ``state["plan"]`` is already populated.
    2. Generates a plan in ONE LLM call; the prompt tells the model to
       return empty steps for simple (<3 tool call) queries, so no
       separate complexity-check call is made.
    3. Returns the plan as serialized dicts when it has 3+ steps.
    """

    async def planner_node(state: Dict[str, Any], config: RunnableConfig) -> dict:
        # Skip if a plan already exists for THIS turn. ``preprocessing_node``
        # clears stale plans at the start of every new user turn, so any
        # ``state["plan"]`` we see here was produced by an earlier pass
        # within the current turn (e.g. a revise loop) and must be reused.
        if state.get("plan"):
            return {}

        # Plans only matter when there's a project context to organize the
        # multi-step output into (ingest → add to project → list). In chat
        # mode the agent runs ad-hoc and the plan is never executed —
        # generating one wastes a planner LLM call. Skip.
        page_context = state.get("page_context") or {}
        if page_context.get("type") != "project":
            return {}

        # Extract the last user message for planning
        messages = state.get("messages", [])
        query = ""
        for msg in reversed(messages):
            content = getattr(msg, "content", None)
            if content and (
                isinstance(msg, HumanMessage) or getattr(msg, "type", None) == "human"
            ):
                query = content
                break

        if not query:
            return {}

        # Fast heuristic: skip the planner LLM call for obviously simple queries.
        # Bumped from 8 → 12 tokens after trace 019e1554 showed a 52s planner
        # spin on a query the LLM would have handled in one tool call anyway.
        # Tool-trigger detection runs first so short imperatives like
        # "Add arxiv 1706.03762 to my library" still reach the planner.
        words = query.split()
        first_word = words[0].lower().rstrip(_LEADING_PUNCTUATION) if words else ""
        needs_tool = first_word in ACTIONABLE_VERBS or bool(_ARXIV_ID_RE.search(query))

        if not needs_tool:
            if len(words) < 12:
                return {}
            if first_word in _CONVERSATIONAL_STARTS and len(words) < 18:
                return {}

        page_context = state.get("page_context", {})

        # Trace 019e6a08: planner LLM latency pushed the turn past the ~30s
        # HTTP cancel budget. Simple single-tool imperatives need no plan.
        if _is_simple_add_flow(query) or _is_simple_writing_flow(query):
            logger.info("Skipping planner entirely for simple single-tool flow")
            return {}

        # 2. Generate plan (single LLM call — complexity gating is folded
        # into the prompt, which returns empty steps for simple queries).
        try:
            plan = await asyncio.wait_for(
                generate_plan(query, tool_names, page_context),
                timeout=PLANNER_LLM_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Plan generation exceeded %ds; skipping planner",
                PLANNER_LLM_TIMEOUT_SECONDS,
            )
            return {}
        except Exception:
            logger.warning("Plan generation failed, skipping planner", exc_info=True)
            return {}

        # Gate on plan size: empty steps = the model judged the query simple
        # (<3 tool calls), and a 1-2 step plan adds no value over the
        # executor's own tool loop — same threshold the old two-call design
        # enforced via its separate complexity estimate. An empty plan must
        # also never occupy the ``plan`` slot (it would skip planning on
        # later passes and confuse reflection-prompt rendering). ``plan``
        # may be ``None`` if the structured-output LLM call returned an
        # unparseable response without raising.
        if plan is None or len(plan.steps) < 3:
            logger.info("Planner judged query simple (<3 steps); no plan stored")
            return {}

        return {
            "plan": [step.model_dump() for step in plan.steps],
            "plan_reasoning": (plan.reasoning or "")[:2000],
        }

    return planner_node
