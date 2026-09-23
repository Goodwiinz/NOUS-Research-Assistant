"""Reflection gate for agent response quality evaluation.

Uses a lightweight LLM to evaluate whether the agent's response
adequately addresses the user's request before proceeding to
memory save / END.  When quality is insufficient, the gate routes
back to the LLM node for another attempt (max 2 rounds).
"""

import asyncio
import logging
import re
from typing import Any, Callable, Literal, Optional

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from src.services.agent._sanitize import _sanitize_prompt_field
from src.services.agent.llm_factory import build_lightweight_llm
from src.services.agent.trace_metadata import internal_llm_config

logger = logging.getLogger(__name__)

# Hard wall-clock cap for a single reflection LLM call. Prevents a hung
# Azure endpoint from blocking the whole agent turn.
_REFLECTION_LLM_TIMEOUT_SECONDS = 45.0  # bumped from 20s after 4096-token
# budget let gpt-5-mini reasoning model spend ~20s on hard prompts; trace
# 019e1874 hit CancelledError at exactly the old ceiling.

# Cache the reflection LLM at module scope. The settings/endpoint are
# resolved at import time once and reused across every reflection call,
# saving a ~50ms client-build round-trip per turn. build_lightweight_llm()
# also caches by args, so even a concurrent double-build returns the same
# factory instance — no lock needed here.
_REFLECTION_LLM = None


# ---------------------------------------------------------------------------
# Pydantic model
# ---------------------------------------------------------------------------


class ReflectionResult(BaseModel):
    """Structured output from the reflection LLM."""

    passed: bool
    issues: list[str]
    severity: Literal["none", "minor", "major"]


# ---------------------------------------------------------------------------
# LLM construction
# ---------------------------------------------------------------------------


def _build_reflection_llm():
    """Return a cached lightweight LLM for reflection evaluation."""
    global _REFLECTION_LLM
    if _REFLECTION_LLM is not None:
        return _REFLECTION_LLM
    # 4096 tokens: gpt-5-mini reasoning tokens count against
    # max_completion_tokens. 512 cap caused LengthFinishReasonError in trace
    # 019e1555 (research_reflection_gate). See classifier.py for context.
    _REFLECTION_LLM = build_lightweight_llm(
        max_tokens=4096,
        request_timeout=_REFLECTION_LLM_TIMEOUT_SECONDS,
    )
    return _REFLECTION_LLM


# ---------------------------------------------------------------------------
# Intent-specific evaluation criteria
# ---------------------------------------------------------------------------


_INTENT_CRITERIA: dict[str, str] = {
    "research": (
        "Evaluate the assistant's response against these research-quality criteria:\n"
        "1. Did the assistant execute relevant tools (search, ingest, etc.)?\n"
        "2. Are any referenced document IDs real UUIDs (not fabricated)?\n"
        "3. Does the response fully address the user's request?\n"
        "4. Is the information specific rather than generic filler?"
    ),
    "writing": (
        "Evaluate the assistant's response against these writing-quality criteria:\n"
        "1. Are sources properly referenced or cited?\n"
        "2. Is the output substantive (not a vague outline or placeholder)?\n"
        "3. Does the format match what the user requested (draft, summary, note, etc.)?\n"
        "4. Is the content well-structured and coherent?"
    ),
}


def _criteria_for_intent(intent: str) -> str:
    """Return evaluation criteria for the given intent."""
    return _INTENT_CRITERIA.get(
        intent,
        (
            "Evaluate the assistant's response for general quality:\n"
            "1. Does the response address the user's question?\n"
            "2. Is the information accurate and helpful?\n"
            "3. Is the response complete?"
        ),
    )


# ---------------------------------------------------------------------------
# Core reflection function
# ---------------------------------------------------------------------------


_REFLECTION_SYSTEM_PROMPT = (
    "You are a response-quality evaluator for an AI research assistant. "
    "Your job is to decide whether the assistant's last response adequately "
    "addresses the user's original request.\n\n"
    "Return a structured evaluation with:\n"
    "- passed: true if the response is acceptable, false otherwise\n"
    "- issues: a list of specific problems found (empty if passed)\n"
    "- severity: 'none' if passed, 'minor' for small issues that don't need "
    "a redo, 'major' for significant problems that warrant another attempt\n\n"
    "{criteria}"
)


# Minimum content length for reflection to be worthwhile. Responses shorter
# than this with no tool calls are too trivial to benefit from critique.
_REFLECTION_MIN_CONTENT_CHARS = 200


def _is_transient_failure(te: Any) -> bool:
    """A tool_execution that failed due to an upstream/transient cause —
    rate limit, network timeout, 5xx — that regenerating the response
    cannot fix. Match the shape produced by ``_execute_single_tool`` +
    ``error_recovery.classify_error_from_payload``.
    """
    if not isinstance(te, dict):
        return False
    if te.get("status") != "failed":
        return False
    result = te.get("result")
    if not isinstance(result, dict):
        return False
    return result.get("error_type") == "transient"


# Ingest result statuses that indicate the tool ran but produced no usable
# documents (or only some of the requested ones). Mirror the constants in
# ``src/services/agent/tools_impl.py`` so the reflection gate can detect when an
# AIMessage claims success despite the tool reporting zero/partial ingest.
_INGEST_FAILURE_STATUSES = {"ingestion_failed", "ingestion_partial"}

# Success verbs the agent commonly uses to claim ingest worked. Matched
# case-insensitively against the AIMessage text. ``\b`` boundaries keep
# substrings like "saddled" from triggering "added".
_INGEST_SUCCESS_CLAIM_RE = re.compile(
    r"\b(added|imported|ingested|attached|saved|loaded)\b",
    re.IGNORECASE,
)

# Phrases that show the assistant disclosed the failure. If any of these
# appear we do NOT flag the response as a lie — the user is informed.
_INGEST_FAILURE_DISCLOSURE_RE = re.compile(
    r"\b(failed|could not|couldn'?t|0 papers?|zero papers?|none|unable|"
    r"did not|didn'?t|no papers?|error)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Fabricated ingest guard
# ---------------------------------------------------------------------------

# Question / offer / disclosure patterns that indicate the AI is ASKING
# whether to ingest, not asserting that it already did. If any of these
# match we skip the fabrication check — no false positives on the
# "which of these would you like me to ingest?" search-results turn.
_INGEST_QUESTION_RE = re.compile(
    r"\bwould\s+you\s+like\s+me\s+to\b"
    r"|\bdo\s+you\s+want\s+me\s+to\b"
    r"|\bshould\s+I\b"
    r"|\bwhich\s+of\s+these\b"
    r"|\bwant\s+me\s+to\s+ingest\b",
    re.IGNORECASE,
)

# Strong, low-false-positive signals that the AI is asserting an ingest
# happened (or is actively in progress) WITHOUT having called the tool.
#
# Signals (each sufficient on its own):
#   A) Inline tool-arg JSON: {"paper_ids": ...} dumped into prose.
#   B) Literal narration artefact "Tool call made".
#   C) Past/progressive first-person assertions:
#      "I ran the import", "I've imported/ingested", "is being imported/
#      ingested", "has been imported/ingested", "importing/ingesting … now".
#
# NOT triggered by pure future-tense offers without a strong signal above:
#   "I will import it", "I can ingest it for you" → no match.
_INGEST_FABRICATION_CLAIM_RE = re.compile(
    # A) Inline tool-arg JSON (model dumping ingest args as prose)
    r'\{[^}]*"paper_ids"\s*:'
    # B) Literal narration artefact
    r"|\bTool\s+call\s+made\b"
    # C) Past/progressive assertions
    r"|\bI\s+ran\s+the\s+import\b"
    r"|\b(I'?ve|I\s+have)\s+(imported|ingested)\b"
    r"|\bis\s+being\s+(imported|ingested)\b"
    r"|\bhas\s+been\s+(imported|ingested)\b"
    r"|\b(importing|ingesting)\b.{0,40}\bnow\b",
    re.IGNORECASE | re.DOTALL,
)


def _detect_fabricated_ingest(state: dict) -> Optional[str]:
    """Return an issue string when the AI claims an arXiv ingest happened but
    no ``ingest_arxiv_papers`` tool completed this turn, else None.

    Guards against the hallucination pattern (observed in prod trace) where
    the model dumps the tool-call JSON + "Tool call made." + "I ran the import"
    into prose without ever executing the tool.

    Returns None when:
    - ``ingest_arxiv_papers`` actually completed this turn (a real ingest ran;
      the count-0 lie is handled separately by ``_detect_ingest_success_lie``).
    - The AI text is asking / offering to ingest rather than asserting it did.
    - No strong ingest-assertion signal is present.
    """
    tool_executions: list[Any] = state.get("tool_executions", []) or []

    executed_ingest = any(
        isinstance(te, dict)
        and te.get("tool_name") == "ingest_arxiv_papers"
        and te.get("status") == "completed"
        for te in tool_executions
    )
    # Real ingest ran — the count-0 lie is handled by _detect_ingest_success_lie.
    if executed_ingest:
        return None

    last_ai = _last_ai_message(state)
    if last_ai is None:
        return None

    content = last_ai.content
    if isinstance(content, list):
        text = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    elif isinstance(content, str):
        text = content
    else:
        return None

    # Question / disclosure guard — must come BEFORE the claim regex so that
    # a search-results turn ("do you want me to ingest any of them?") is never
    # flagged.  Also reuse the honest-disclosure phrases from the count-0 guard.
    if _INGEST_QUESTION_RE.search(text):
        return None
    if _CREATE_FAILURE_DISCLOSURE_RE.search(text):
        return None

    if not _INGEST_FABRICATION_CLAIM_RE.search(text):
        return None

    return (
        "Assistant claims an arXiv paper was imported/ingested, but no "
        "ingest_arxiv_papers tool executed this turn — the import was not "
        "performed (any inline tool JSON is fabricated). Re-answer: either "
        "call ingest_arxiv_papers or tell the user the import did not run."
    )


# ---------------------------------------------------------------------------
# Fabricated knowledge-graph search guard
# ---------------------------------------------------------------------------

# Knowledge-graph READ tools. A completed execution of ANY of these means the
# graph was genuinely queried this turn, so a "I searched/explored/queried the
# knowledge graph" narration is truthful (not a fabrication). Mirrors the data
# subgraph's KG read set (subgraphs/data_agent.py).
_KG_READ_TOOLS: frozenset[str] = frozenset(
    {
        "search_knowledge_graph",
        "explore_entity_neighborhood",
        "find_entity_paths",
        "get_graph_stats",
    }
)

# Offer / question phrasing — the AI is proposing to search the graph, not
# claiming it already did. Skip the fabrication check on these.
_KG_SEARCH_QUESTION_RE = re.compile(
    r"\bwould\s+you\s+like\s+me\s+to\b"
    r"|\bdo\s+you\s+want\s+me\s+to\b"
    r"|\bshould\s+I\b"
    r"|\bI\s+can\s+(?:search|explore|query)\b"
    r"|\b(?:use|call)\s+search_knowledge_graph\b",  # instructional, not a claim
    re.IGNORECASE,
)

# Strong, low-false-positive signal that the AI is ASSERTING, in its own action
# voice, that it searched the knowledge graph this turn — a first-person past
# action explicitly naming the "knowledge graph". Deliberately narrow: a paper
# SUMMARY describing a graph ("the authors explored the graph", "the knowledge
# graph community found …") or an API note ("each node has an entity_id") is NOT
# the assistant claiming it performed a search, so those must not match.
# NOT triggered by future/offer phrasing (handled by the question guard).
_KG_SEARCH_FABRICATION_CLAIM_RE = re.compile(
    r"\bI\s+(?:searched|queried|explored)\s+the\s+knowledge\s+graph\b"
    r"|\bsearched\s+the\s+knowledge\s+graph\s+for\b",
    re.IGNORECASE | re.DOTALL,
)

# Honest-failure phrasings specific to a KG search — the assistant says the
# search ran but came back empty / failed. Reused alongside the creation
# disclosure regex so a truthful "I searched the knowledge graph but it returned
# nothing" (where the tool may have errored, status!=completed) is not flagged.
_KG_SEARCH_FAILURE_DISCLOSURE_RE = re.compile(
    r"\breturned\s+(?:nothing|no\s+\w+)"
    r"|\bfound\s+(?:nothing|no\s+\w+)"
    r"|\bno\s+(?:entit|result|match|relation)"
    r"|\bnothing\s+(?:relevant|found)"
    r"|\bsearch\s+(?:failed|errored)"
    r"|\bcould\s*n'?o?t\s+(?:find|search|reach)"
    r"|\bwas\s+unable\b"
    r"|\bdid\s*n'?o?t\s+return\b"
    r"|\bcame\s+back\s+empty\b",
    re.IGNORECASE,
)

# Document / arXiv / DO-KB READ tools — the retrieval side-channels the model
# can claim to have searched without ever emitting a tool_call. Mirrors
# _KG_READ_TOOLS for the same fabrication failure mode on the document surface.
_DOC_READ_TOOLS: frozenset[str] = frozenset(
    {
        "search_documents",
        "search_arxiv",
        "do_kb_retrieve",
        # Other document/retrieval reads whose completion means the model
        # legitimately grounded on real content — narrating "I searched /
        # looked through the documents" after any of these is truthful.
        "list_project_documents",
        "search_external_database",
        "summarize_document",
        "compare_documents",
    }
)

# Offer / question phrasing on the document surface — proposing to search, not
# claiming it already searched. Skip the fabrication check on these.
_DOC_SEARCH_QUESTION_RE = re.compile(
    r"\bwould\s+you\s+like\s+me\s+to\b"
    r"|\bdo\s+you\s+want\s+me\s+to\b"
    r"|\bshould\s+I\b"
    r"|\bI\s+can\s+(?:search|look|find)\b"
    r"|\b(?:use|call)\s+(?:search_documents|search_arxiv|do_kb_retrieve)\b",
    re.IGNORECASE,
)

# Strong, low-false-positive signal that the AI ASSERTS, in first-person past
# action voice, that it searched the documents / library / arXiv this turn.
# Deliberately narrow: a paper summary that mentions "the authors searched a
# corpus" or generic "search your documents" instructional text must NOT match —
# only a first-person past-tense claim of having performed the retrieval.
_DOC_SEARCH_FABRICATION_CLAIM_RE = re.compile(
    r"\bI\s+(?:searched|queried|looked\s+through|scanned|retrieved\s+from)\s+"
    r"(?:your\s+|the\s+|our\s+)?"
    r"(?:documents?|library|collection|corpus|papers?|arxiv|knowledge\s+base)\b"
    r"|\bsearched\s+(?:your\s+|the\s+|our\s+)"
    r"(?:documents?|library|collection|corpus)\s+for\b"
    r"|\bfrom\s+(?:your\s+|the\s+)(?:uploaded\s+|indexed\s+)?documents?\s+I\s+found\b",
    re.IGNORECASE | re.DOTALL,
)


def _prior_turn_read_happened(state: dict, read_tools: frozenset[str]) -> bool:
    """True when any AIMessage BEFORE the last one carries a tool_call whose
    name is in ``read_tools`` — i.e. retrieval genuinely happened earlier in
    the thread.

    ``tool_executions`` / ``retrieved_contexts`` are per-turn channels, reset
    to [] on every request (streaming.py / jobs.py initial_state), so the
    fabrication guards only see the current turn. But the claim regexes also
    match truthful past-turn references ("Earlier I searched your documents
    and found …"), which force-revised legit multi-turn follow-ups. Prior
    tool_calls in ``state["messages"]`` are the durable record that a read
    actually ran, so their presence makes such references truthful.
    """
    messages = state.get("messages", [])
    # Exclude the last AIMessage: its own tool_calls are this turn's, already
    # covered (more strictly, with completion status) by tool_executions.
    last_ai_seen = False
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            if not last_ai_seen:
                last_ai_seen = True
                continue
            for tc in getattr(msg, "tool_calls", None) or []:
                name = (
                    tc.get("name")
                    if isinstance(tc, dict)
                    else getattr(tc, "name", None)
                )
                if name in read_tools:
                    return True
    return False


def _detect_fabricated_doc_search(state: dict) -> Optional[str]:
    """Return an issue string when the AI claims it searched the documents /
    library / arXiv (or presents retrieved results) but no document READ tool
    completed this turn, else None.

    Mirrors ``_detect_fabricated_kg_search`` for the retrieval side-channels
    (``search_documents`` / ``search_arxiv`` / ``do_kb_retrieve``). Same failure
    mode: the model narrates "I searched your documents and found …" while
    emitting NO read tool_call, so nothing was retrieved and any cited
    sources/ids are fabricated or carried over from context. The KG guard only
    covered graph reads, so document-surface fabrication slipped past reflection.

    Returns None when:
    - The RAG channel retrieved real context this turn (``rag_node`` populates
      ``state["retrieved_contexts"]`` WITHOUT emitting a tool_execution — it is
      the platform's PRIMARY document-retrieval path, so a first-person "I
      searched your documents and found …" over real RAG context is truthful,
      not fabricated). Checked first; without it the guard would force-revise a
      broad class of correct, correctly-cited RAG answers.
    - Any document READ tool actually completed this turn (real results).
    - The AI text is offering / instructing rather than asserting it searched.
    - An honest-failure disclosure is present ("returned nothing", "couldn't
      find …" — reuses the shared failure-disclosure regex).
    - No strong retrieval-assertion signal is present.
    """
    # rag_node grounds answers via retrieved_contexts, not a tool_execution —
    # non-empty means retrieval genuinely happened this turn.
    if state.get("retrieved_contexts"):
        return None

    tool_executions: list[Any] = state.get("tool_executions", []) or []

    executed_search = any(
        isinstance(te, dict)
        and te.get("tool_name") in _DOC_READ_TOOLS
        and te.get("status") == "completed"
        for te in tool_executions
    )
    if executed_search:
        return None

    # Retrieval genuinely happened earlier in the thread — a follow-up that
    # references it ("Earlier I searched your documents …") is truthful.
    # Threads with zero retrieval ever keep the strict guard.
    if _prior_turn_read_happened(state, _DOC_READ_TOOLS):
        return None

    last_ai = _last_ai_message(state)
    if last_ai is None:
        return None

    content = last_ai.content
    if isinstance(content, list):
        text = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    elif isinstance(content, str):
        text = content
    else:
        return None

    if _DOC_SEARCH_QUESTION_RE.search(text):
        return None
    if _CREATE_FAILURE_DISCLOSURE_RE.search(text):
        return None
    if _KG_SEARCH_FAILURE_DISCLOSURE_RE.search(text):
        return None

    if not _DOC_SEARCH_FABRICATION_CLAIM_RE.search(text):
        return None

    return (
        "Assistant claims it searched your documents / library / arXiv or "
        "presents retrieved sources, but no document read tool "
        "(search_documents / search_arxiv / do_kb_retrieve) executed this turn — "
        "nothing was retrieved (any cited sources or ids are fabricated). "
        "Re-answer: either call the search tool or tell the user the search did "
        "not run."
    )


def _detect_fabricated_kg_search(state: dict) -> Optional[str]:
    """Return an issue string when the AI claims it searched the knowledge graph
    (or presents graph entities/relations) but no ``search_knowledge_graph``
    tool completed this turn, else None.

    Mirrors ``_detect_fabricated_ingest`` for the read-side failure observed in
    a trace: the model narrates "I searched the knowledge graph … found these
    entities (entity_id: …)" and dumps plausible ids while emitting NO
    ``search_knowledge_graph`` tool_call, so the graph was never queried and the
    ids are fabricated/carried-over. The ingest/create fabrication guards did
    not cover read tools, so this slipped past reflection.

    Returns None when:
    - Any knowledge-graph READ tool actually completed this turn (real results) —
      not just ``search_knowledge_graph`` but also the neighborhood/path/stats
      reads, since the model legitimately narrates "I explored/queried the
      knowledge graph" for those too.
    - The AI text is offering / instructing rather than asserting it searched.
    - No strong KG-result-assertion signal is present.
    """
    tool_executions: list[Any] = state.get("tool_executions", []) or []

    executed_search = any(
        isinstance(te, dict)
        and te.get("tool_name") in _KG_READ_TOOLS
        and te.get("status") == "completed"
        for te in tool_executions
    )
    if executed_search:
        return None

    # Same multi-turn relaxation as the doc guard: a prior-turn KG read makes
    # a past-tense reference truthful even though tool_executions reset.
    if _prior_turn_read_happened(state, _KG_READ_TOOLS):
        return None

    last_ai = _last_ai_message(state)
    if last_ai is None:
        return None

    content = last_ai.content
    if isinstance(content, list):
        text = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    elif isinstance(content, str):
        text = content
    else:
        return None

    if _KG_SEARCH_QUESTION_RE.search(text):
        return None
    if _CREATE_FAILURE_DISCLOSURE_RE.search(text):
        return None
    if _KG_SEARCH_FAILURE_DISCLOSURE_RE.search(text):
        return None

    if not _KG_SEARCH_FABRICATION_CLAIM_RE.search(text):
        return None

    return (
        "Assistant claims it searched the knowledge graph or presents graph "
        "entities/relations, but no search_knowledge_graph tool executed this "
        "turn — the graph was not queried (any entity ids are fabricated). "
        "Re-answer: either call search_knowledge_graph or tell the user the "
        "search did not run."
    )


def _ingest_zero_count(te: Any) -> bool:
    """Detect an ``ingest_arxiv_papers`` execution that ingested nothing.

    The tool node marks the execution ``completed`` whenever the Python
    call returned without raising — even when the tool's own ``status``
    field is ``ingestion_failed``. We have to look one level deeper at
    ``result.status`` / ``result.ingested_count`` to spot the lie.
    """
    if not isinstance(te, dict):
        return False
    if te.get("tool_name") != "ingest_arxiv_papers":
        return False
    result = te.get("result")
    if not isinstance(result, dict):
        return False
    if result.get("status") in _INGEST_FAILURE_STATUSES:
        return True
    # Defensive: legacy result shape with no ``status`` but a count.
    try:
        return int(result.get("ingested_count", 0)) == 0
    except (TypeError, ValueError):
        return False


def _detect_ingest_success_lie(state: dict) -> Optional[str]:
    """Return an issue string when the AI claims ingest success but the
    tool reported failure/partial, else None.

    Deterministic guard for the failure mode in trace 019e1a1d, where
    ``ingest_arxiv_papers`` returned ``ingested_count: 0`` and the agent
    still told the user "Done — I added one paper". The LLM reflector
    missed it (passed=true) because the AI text looks plausible.
    """
    tool_executions = state.get("tool_executions", []) or []
    failing = [te for te in tool_executions if _ingest_zero_count(te)]
    if not failing:
        return None

    last_ai = _last_ai_message(state)
    if last_ai is None:
        return None
    content = last_ai.content
    if isinstance(content, list):
        text = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    elif isinstance(content, str):
        text = content
    else:
        return None

    if not _INGEST_SUCCESS_CLAIM_RE.search(text):
        return None
    if _INGEST_FAILURE_DISCLOSURE_RE.search(text):
        return None

    paper_ids: list[str] = []
    for te in failing:
        result = te.get("result") or {}
        for pid in result.get("paper_ids", []) or []:
            if pid not in paper_ids:
                paper_ids.append(str(pid))
    return (
        "Assistant claims ingest succeeded but "
        f"ingest_arxiv_papers returned 0 documents (paper_ids="
        f"{paper_ids or 'unknown'}). Tell the user the ingest failed "
        "and propose a concrete next step (retry, different IDs, or "
        "wait for very recent papers to be indexed)."
    )


# ---------------------------------------------------------------------------
# Fabricated tool success guard
# ---------------------------------------------------------------------------

# Creation tools whose *actual* execution is required before the AI may
# claim a project / note / draft / document was created or added.
# Names verified from ``backend/src/services/agent/_nodes_tools.py``
# (DESTRUCTIVE_TOOLS) and ``backend/src/services/agent/tools.py``.
CREATION_TOOLS: frozenset[str] = frozenset(
    {
        "create_project",
        "create_project_note",
        "create_draft",
        "revise_draft",
        "add_document_to_project",
    }
)

# Patterns that indicate the AI is asserting a creation already happened.
#
# Design intent: require an EXPLICIT success assertion in the model's own
# action voice, OR a fabricated tool-call/result JSON blob.  A bare
# ``project_id`` substring must NOT trigger on its own — it appears in
# legitimate quoted tool results (e.g. list_projects output) and forward-
# looking suggestions ("you could add this paper to the project later").
#
# A hit requires at least one of:
#   A) An explicit success-assertion verb near "project"/"note"/"draft"
#      ("created the project", "note saved", "I've added … to the project",
#      "Project created", "creating the project", "I've created", …).
#   B) An inline fabricated tool-call/result JSON blob that contains
#      ``"name":`` / ``"project_id":`` / ``"status":"active"`` — the model
#      dumping tool args or result JSON directly into prose.
#   C) A ``project_id`` token that co-occurs with a nearby ``active``
#      keyword (e.g. "project_id: … is active") — catches the common shape
#      of a fabricated status summary without a JSON wrapper.
#
# NOT triggered by:
#   - "Here are your projects: NLP (project_id: abc-123)" (list output)
#   - "You could add this paper to the project later"      (suggestion)
_CREATE_SUCCESS_CLAIM_RE = re.compile(
    # A) explicit success-assertion verbs in model's own voice ---------------
    r'"status"\s*:\s*"active"'  # inline JSON status field
    r"|\bproject\s+created\b"  # "project created"
    r"|\bcreating\s+the\s+project\b"  # "Creating the project …"
    r"|\bcreated\s+(the\s+)?project\b"  # "created the project"
    r"|\b(I'?ve|I\s+have)\s+(created|added|saved)\b"  # "I've created / I have added"
    r"|\bnote\b.{0,25}(created|saved|added)\b"  # "note created / note has been saved"
    r"|\badded\s+.{0,40}\bto\s+(the\s+)?project\b"  # "added X to project"
    r"|\bi(?:'ve|\s+have)?\s+saved\s+(?:the\s+)?revision(?:\s+v?\d+)?\b"
    r"|\brevision(?:\s+v?\d+)?\s+(?:was\s+|has\s+been\s+)?saved\b"
    r"|\bupdated\s+the\s+draft\b"
    # B) inline fabricated JSON blob -----------------------------------------
    r'|\{[^}]*"name"\s*:'  # {"name": …}
    r'|\{[^}]*"project_id"\s*:'  # {"project_id": …}
    # C) project_id co-occurring with active (fabricated status summary) ------
    r"|project_id\b.{0,80}\bactive\b",  # "project_id: … active"
    re.IGNORECASE | re.DOTALL,
)

# Honest-disclosure phrases: the AI admits the action wasn't performed (or is
# uncertain whether the target exists). If any match, we do NOT flag — the model
# is being truthful, not asserting a fabricated success.
_CREATE_FAILURE_DISCLOSURE_RE = re.compile(
    r"couldn'?t\s+save"
    r"|no\s+project_id\s+was\s+provided"
    r"|need\s+the\s+destination"
    r"|would\s+you\s+like\s+me\s+to"
    r"|I\s+was\s+unable"
    r"|could\s+not\s+create"
    r"|was\s+not\s+(?:able|performed|completed)"
    # Honest uncertainty about the target's state — incompatible with claiming a
    # creation succeeded. Observed false positive (ingest scenario): "I'm ready
    # to ingest, but I don't yet know if the project X exists in your workspace."
    r"|do(?:n'?t|\s+not)\s+(?:yet\s+)?know\s+(?:if|whether)"
    r"|not\s+sure\s+(?:yet\s+)?(?:if|whether)",
    re.IGNORECASE,
)


def _detect_fabricated_tool_success(state: dict) -> Optional[str]:
    """Return an issue string when the AI claims a creation succeeded but no
    creation tool actually executed this turn, else None.

    Guards against the hallucination pattern where the LLM emits a response
    like "Project created (project_id: …, status: active)" or
    "I have created the project for NLP" without ever calling
    ``create_project``, ``create_project_note``, ``create_draft``, or
    ``add_document_to_project``.

    Returns:
        A non-None string when fabrication is detected; None when the turn
        looks honest (real tool ran, or AI disclosed it didn't act).
    """
    tool_executions: list[Any] = state.get("tool_executions", []) or []

    # Check whether at least one creation tool completed this turn.
    executed = [
        te
        for te in tool_executions
        if isinstance(te, dict)
        and te.get("tool_name") in CREATION_TOOLS
        and te.get("status") == "completed"
    ]

    last_ai = _last_ai_message(state)
    if last_ai is None:
        return None

    content = last_ai.content
    if isinstance(content, list):
        text = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    elif isinstance(content, str):
        text = content
    else:
        return None

    # If the AI honestly discloses it didn't act, no fabrication.
    if _CREATE_FAILURE_DISCLOSURE_RE.search(text):
        return None

    # Fabrication: AI claims creation but no creation tool ran.
    if _CREATE_SUCCESS_CLAIM_RE.search(text) and not executed:
        return (
            "Assistant claims a project/note/draft was created or added, but no "
            "creation tool actually executed this turn — the result (including any "
            "id) is fabricated. Re-answer: either call the tool or tell the user "
            "the action was not performed."
        )

    return None


def _is_completed_or_transient(te: Any) -> bool:
    """Status counts toward the transient-acknowledged skip: completed
    successes, transient failures, or deduped (already-counted) entries.
    A non-transient failure (e.g. validation error, auth denied) should
    NOT skip — those are agent-fixable and worth critiquing.
    """
    if not isinstance(te, dict):
        return False
    status = te.get("status")
    if status in ("completed", "deduped"):
        return True
    return _is_transient_failure(te)


def _last_ai_message(state: dict) -> AIMessage | None:
    """Return the most recent AIMessage in state, or None."""
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, AIMessage):
            return msg
    return None


def _should_skip_reflection(state: dict) -> tuple[bool, str]:
    """Decide whether to skip the reflection LLM call.

    Skip conditions (cheap, deterministic checks that avoid a ~944 token
    critique LLM call when the response is too trivial to benefit from
    one):

    1. Latest AIMessage content is shorter than
       ``_REFLECTION_MIN_CONTENT_CHARS`` AND has no tool_calls.
    2. ``state["tool_executions"]`` is empty AND the latest AIMessage has
       no tool_calls (no tools ran -> nothing tool-grounded to critique).

    Args:
        state: The current agent state dict.

    Returns:
        ``(skip, reason)`` — ``skip`` is True when the reflection LLM
        call should be bypassed; ``reason`` is a short human-readable
        string suitable for logging and test assertions.
    """
    last_ai = _last_ai_message(state)
    if last_ai is None:
        # Defer to existing missing-message handling in the node.
        return (False, "no-ai-message")

    # Fabrication guards run first — before any skip conditions — so that
    # a hallucinated "project created" or "paper imported" response is never
    # silently passed through, regardless of content length or tool_executions.
    if _detect_fabricated_tool_success(state) is not None:
        return (False, "potential fabricated tool success")
    if _detect_fabricated_ingest(state) is not None:
        return (False, "potential fabricated ingest")
    if _detect_fabricated_kg_search(state) is not None:
        return (False, "potential fabricated kg search")
    if _detect_fabricated_doc_search(state) is not None:
        return (False, "potential fabricated doc search")

    tool_calls = getattr(last_ai, "tool_calls", None) or []
    has_tool_calls = bool(tool_calls)

    content = last_ai.content or ""
    content_len = len(content) if isinstance(content, str) else 0

    if not has_tool_calls and content_len < _REFLECTION_MIN_CONTENT_CHARS:
        return (
            True,
            f"short-output ({content_len} < {_REFLECTION_MIN_CONTENT_CHARS} chars, no tool_calls)",
        )

    tool_executions = state.get("tool_executions", []) or []
    if not has_tool_calls and not tool_executions:
        return (True, "no-tools (tool_executions empty, no tool_calls)")

    # Fast-path: substantive final response with all tools succeeded —
    # skip the critique LLM (saves ~5-20s/turn). The cheap deterministic
    # checks above already gate the truly-trivial cases.
    #
    # Exclude ingest tools that returned status=ingestion_failed/partial:
    # the outer ToolNode marks them ``completed`` (no exception raised),
    # so happy-path would skip and the LLM reflector never sees the
    # mismatch between "Done — I added one paper" and ``ingested_count=0``.
    if (
        not has_tool_calls
        and content_len >= _REFLECTION_MIN_CONTENT_CHARS
        and tool_executions
        and all(
            (te.get("status") if isinstance(te, dict) else getattr(te, "status", None))
            == "completed"
            for te in tool_executions
        )
        and not any(_ingest_zero_count(te) for te in tool_executions)
    ):
        return (
            True,
            f"happy-path ({content_len} chars, {len(tool_executions)} tools all completed)",
        )

    # Fast-path: tools ran but the only failures were transient/external
    # (rate limits, timeouts, network errors). The agent cannot recover by
    # regenerating its response — the failure is upstream. Critiquing as
    # "did not execute tools" wastes ~3k tokens per turn and can trigger a
    # useless revise loop.
    #
    # Trace (revision e8d8b1ad, "grab me more paper about ML in Health Care"):
    # arXiv 429 → tool_executions=[{status:"failed", result:{error_type:
    # "transient"}}] → reflection ran 2× and flagged "did not execute any
    # tools" both times. Wrong: tool ran, external API failed.
    if (
        not has_tool_calls
        and content_len >= _REFLECTION_MIN_CONTENT_CHARS
        and tool_executions
    ):
        # Single pass: classify every entry once. Avoids the 3× iteration
        # the predicate-pair version did (all + any + sum on the same list).
        transient_count = 0
        ok_to_skip = True
        for te in tool_executions:
            if _is_transient_failure(te):
                transient_count += 1
            elif not _is_completed_or_transient(te):
                ok_to_skip = False
                break
        if ok_to_skip and transient_count:
            return (
                True,
                f"transient-failure-acknowledged ({content_len} chars, "
                f"{transient_count} transient failures)",
            )

    return (False, "")


# R7-L3: the assistant response is embedded verbatim in the reflection user
# prompt while the user request is capped at 400 chars, so a response echoing
# injected document text could both drown the critique prompt and open its own
# "## " section. Cap it and defang leading markdown headings — but keep the
# newlines, because response formatting is part of what reflection critiques.
_MAX_REFLECTION_RESPONSE_CHARS = 6000
_LEADING_HEADING_RE = re.compile(r"(?m)^[ \t]*#+")


def _render_response_for_reflection(raw_content: Any) -> str:
    """Render an AIMessage content payload for the reflection prompt."""
    if isinstance(raw_content, list):
        # Multimodal content (list of content blocks). Render only the text
        # parts so the reflection prompt stays human-readable.
        text_parts: list[str] = []
        for block in raw_content:
            if isinstance(block, str):
                text_parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
        rendered = "\n".join(text_parts) or "(no text content)"
    elif isinstance(raw_content, str) and raw_content:
        rendered = raw_content
    else:
        return "(no content)"

    if len(rendered) > _MAX_REFLECTION_RESPONSE_CHARS:
        rendered = rendered[:_MAX_REFLECTION_RESPONSE_CHARS] + "..."
    return _LEADING_HEADING_RE.sub(lambda m: m.group(0).replace("#", r"\#"), rendered)


async def reflect_on_response(
    last_ai_message: AIMessage,
    original_user_message: str,
    plan: Optional[list] = None,
    intent: str = "research",
) -> ReflectionResult:
    """Evaluate the quality of the agent's last response.

    Args:
        last_ai_message: The AI message to evaluate.
        original_user_message: The user's original request.
        plan: Optional advisory plan steps (for plan-aware evaluation).
        intent: The classified intent (research, writing, etc.).

    Returns:
        A ``ReflectionResult`` with pass/fail, issues, and severity.
    """
    llm = _build_reflection_llm()
    structured_llm = llm.with_structured_output(ReflectionResult)

    criteria = _criteria_for_intent(intent)
    system_text = _REFLECTION_SYSTEM_PROMPT.format(criteria=criteria)

    plan_text = ""
    if plan:
        plan_lines: list[str] = []
        for i, step in enumerate(plan):
            if isinstance(step, dict):
                summary = step.get("description") or step.get("step") or str(step)
            else:
                # Plan items can occasionally be raw strings (e.g. when an
                # external producer skips the dict envelope). Fall back to
                # ``str(step)`` instead of crashing with ``AttributeError``.
                summary = str(step)
            plan_lines.append(f"  {i + 1}. {summary}")
        plan_text = "\n\nAdvisory plan the agent was following:\n" + "\n".join(
            plan_lines
        )

    rendered_content = _render_response_for_reflection(last_ai_message.content)

    user_prompt = (
        f"## User's original request\n{_sanitize_prompt_field(original_user_message)}\n\n"
        f"## Assistant's response\n{rendered_content}"
        f"{plan_text}"
    )

    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_prompt},
    ]

    result = await structured_llm.ainvoke(messages, config=internal_llm_config())
    return result


# ---------------------------------------------------------------------------
# Reflection gate factory
# ---------------------------------------------------------------------------


def make_reflection_gate(
    intent_filter: set[str] | None = None,
) -> tuple[Callable, Callable]:
    """Create the reflection gate node function and routing function.

    Args:
        intent_filter: Set of intents that should be reflected on.
            Defaults to ``{"research", "writing"}``.

    Returns:
        A tuple of ``(node_function, routing_function)`` suitable for
        use in a LangGraph StateGraph.
    """
    if intent_filter is None:
        intent_filter = {"research", "writing"}

    async def _reflection_node_impl(state: dict, config: RunnableConfig) -> dict:
        """Evaluate the last AI response and decide whether to revise.

        Skips reflection when:
        - The intent is not in the filter set.
        - The reflection count has reached the maximum (2).
        - The latest AIMessage is too trivial to critique (see
          ``_should_skip_reflection``): short content with no tool_calls,
          or no tools were executed and no pending tool_calls.
        """
        intent = state.get("intent", "general")
        current_count = state.get("reflection_count", 0)

        # Skip if max rounds reached. Checked before the deterministic guards so
        # a fabrication can't force an unbounded revise loop.
        if current_count >= 2:
            return {"reflection_count": current_count}

        # The deterministic fabrication guards below run for EVERY intent, BEFORE
        # the intent_filter gate. A fabricated ingest/create/search is a hard
        # error regardless of routing, and these are cheap (regex + a
        # tool_executions scan, no LLM call). Running them unconditionally also
        # protects knowledge_graph turns, which deliberately skip the expensive
        # LLM critique (deterministic lookups) and would otherwise let a
        # narrated-but-never-executed KG search reach the user.

        # Deterministic guard: AI claims ingest worked but the tool
        # actually returned 0 documents (status=ingestion_failed/partial).
        # Hard-fail without an LLM call — the issue is unambiguous from
        # tool result + AI text, and the LLM reflector has been observed
        # to miss it (trace 019e1a1d, passed=true after the lie).
        ingest_issue = _detect_ingest_success_lie(state)
        if ingest_issue is not None:
            logger.warning(
                "Reflection deterministic fail: ingest success-claim mismatch"
            )
            return {
                "reflection_count": current_count + 1,
                "_reflection_result": ReflectionResult(
                    passed=False, issues=[ingest_issue], severity="major"
                ),
            }

        # Deterministic guard: AI claims a project/note/draft was created or
        # a document was added, but no creation tool actually ran. Hard-fail
        # without an LLM call — the fabricated ID / status field is a clear
        # signal that does not need probabilistic critique.
        fabrication_issue = _detect_fabricated_tool_success(state)
        if fabrication_issue is not None:
            last_ai_for_log = _last_ai_message(state)
            _snippet = ""
            _matched = ""
            if last_ai_for_log is not None:
                _raw = last_ai_for_log.content
                _text = (
                    " ".join(
                        b.get("text", "") if isinstance(b, dict) else str(b)
                        for b in _raw
                    )
                    if isinstance(_raw, list)
                    else (str(_raw) if _raw else "")
                )
                _snippet = _text[:120].replace("\n", " ")
                # Log the exact span that tripped the claim regex — the 120-char
                # head often truncates before the match, leaving false positives
                # undiagnosable. The matched phrase is what to tune against.
                _m = _CREATE_SUCCESS_CLAIM_RE.search(_text)
                _matched = _m.group(0).replace("\n", " ") if _m else ""
            logger.warning(
                "Reflection guard: forcing major-revise — fabricated tool success "
                "detected (no creation tool ran). matched=%r snippet=%r intent=%s",
                _matched,
                _snippet,
                intent,
            )
            return {
                "reflection_count": current_count + 1,
                "_reflection_result": ReflectionResult(
                    passed=False, issues=[fabrication_issue], severity="major"
                ),
            }

        # Deterministic guard: AI claims an arXiv paper was imported/ingested
        # but ingest_arxiv_papers never executed this turn. The model may dump
        # the tool-arg JSON {"paper_ids":[...]} + "Tool call made." + "I ran
        # the import" inline as prose — a fully-fabricated ingest that slips
        # past both _detect_ingest_success_lie (no tool ran) and the LLM
        # reflector (text looks plausible). Hard-fail without an LLM call.
        fabricated_ingest_issue = _detect_fabricated_ingest(state)
        if fabricated_ingest_issue is not None:
            logger.warning(
                "Reflection guard: forcing major-revise — fabricated ingest detected "
                "(ingest_arxiv_papers never executed). intent=%s",
                intent,
            )
            return {
                "reflection_count": current_count + 1,
                "_reflection_result": ReflectionResult(
                    passed=False,
                    issues=[fabricated_ingest_issue],
                    severity="major",
                ),
            }

        # Deterministic guard: AI claims it searched the knowledge graph (or
        # presents graph entities/relations with ids) but search_knowledge_graph
        # never executed this turn — the read tool was narrated, not called, so
        # the results are fabricated. The ingest/create guards above don't cover
        # read tools, so this would otherwise reach the user unflagged.
        fabricated_kg_search_issue = _detect_fabricated_kg_search(state)
        if fabricated_kg_search_issue is not None:
            logger.warning(
                "Reflection guard: forcing major-revise — fabricated KG search "
                "detected (search_knowledge_graph never executed). intent=%s",
                intent,
            )
            return {
                "reflection_count": current_count + 1,
                "_reflection_result": ReflectionResult(
                    passed=False,
                    issues=[fabricated_kg_search_issue],
                    severity="major",
                ),
            }

        # Same read-side fabrication guard for the DOCUMENT retrieval tools
        # (search_documents / search_arxiv / do_kb_retrieve): the model claims
        # it searched the library/arXiv and cites sources, but no read tool ran.
        fabricated_doc_search_issue = _detect_fabricated_doc_search(state)
        if fabricated_doc_search_issue is not None:
            logger.warning(
                "Reflection guard: forcing major-revise — fabricated document "
                "search detected (no search_documents/search_arxiv/do_kb_retrieve "
                "executed). intent=%s",
                intent,
            )
            return {
                "reflection_count": current_count + 1,
                "_reflection_result": ReflectionResult(
                    passed=False,
                    issues=[fabricated_doc_search_issue],
                    severity="major",
                ),
            }

        # Gate the EXPENSIVE LLM critique by intent. The deterministic guards
        # above already ran for every intent; intents outside the filter (e.g.
        # knowledge_graph deterministic lookups) skip only the probabilistic
        # reflection, not the fabrication guards.
        if intent not in intent_filter:
            # No verdict is written here on purpose: no critique ran, so
            # claiming one passed would surface a phantom "Reflection passed"
            # SSE frame. The wrapper below clears a *stale* verdict instead.
            return {"reflection_count": current_count}

        # Cheap pre-LLM gate: skip critique for trivial / tool-less turns.
        skip, reason = _should_skip_reflection(state)
        if skip:
            logger.debug("Reflection skipped: %s", reason)
            return {
                "reflection_count": current_count,
                "_reflection_result": ReflectionResult(
                    passed=True, issues=[], severity="none"
                ),
            }

        # Find last AI message and most recent user message (the request
        # being addressed in this turn). The reverse scan intentionally
        # picks the latest HumanMessage so multi-turn conversations
        # critique the response against the current turn's question, not
        # the very first one.
        last_ai_message: AIMessage | None = None
        original_user_message: str = ""

        for msg in reversed(state.get("messages", [])):
            if isinstance(msg, AIMessage) and last_ai_message is None:
                last_ai_message = msg
            if isinstance(msg, HumanMessage) and not original_user_message:
                original_user_message = msg.content
            if last_ai_message is not None and original_user_message:
                break

        if last_ai_message is None or not original_user_message:
            logger.debug("Reflection skipped: missing AI or user message")
            return {"reflection_count": current_count}

        plan = state.get("plan")

        try:
            result = await asyncio.wait_for(
                reflect_on_response(
                    last_ai_message=last_ai_message,
                    original_user_message=original_user_message,
                    plan=plan,
                    intent=intent,
                ),
                timeout=_REFLECTION_LLM_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Reflection LLM timed out after %.1fs; proceeding without revision",
                _REFLECTION_LLM_TIMEOUT_SECONDS,
            )
            # Don't burn a retry budget slot for an infrastructure failure.
            return {
                "reflection_count": current_count,
                "_reflection_result": ReflectionResult(
                    passed=True, issues=[], severity="none"
                ),
            }
        except asyncio.CancelledError:
            # User abort / shutdown — propagate, never swallow into a
            # silent "passed" result (house pattern, see classifier).
            raise
        except Exception as e:
            logger.warning("Reflection failed, proceeding anyway: %s", e)
            # Don't burn a retry budget slot — let the agent recover on the
            # next turn with full budget, and clear any stale result so the
            # router defaults to ``proceed``.
            return {
                "reflection_count": current_count,
                "_reflection_result": ReflectionResult(
                    passed=True, issues=[], severity="none"
                ),
            }

        return {
            "reflection_count": current_count + 1,
            "_reflection_result": result,
        }

    def reflection_route(state: dict) -> str:
        """Route based on reflection result.

        Returns:
            ``"proceed"`` — continue to memory_save_node / END.
            ``"revise"`` — loop back to llm_node for another attempt.
        """
        result: ReflectionResult | None = state.get("_reflection_result")

        # Major severity routes to revise if we still have budget; everything
        # else proceeds.
        # NOTE: the reflection_node increments reflection_count BEFORE the
        # router runs, so the value here is already post-increment. The node's
        # own >= 2 guard caps revisions at 2 (skipping the increment and
        # returning early), so by the time the router runs current_count is
        # at most 1 (first revise) or 2 (second revise). Using < 2 here is
        # consistent with the node's cap: once current_count reaches 2 the
        # router proceeds instead of revising again.
        current_count = state.get("reflection_count", 0)
        if (
            result is not None
            and not result.passed
            and result.severity == "major"
            and current_count < 2
        ):
            decision = "revise"
        else:
            decision = "proceed"

        try:
            from src.services.agent.observability import record_reflection_decision

            record_reflection_decision(decision, state.get("intent", "unknown"))
        except Exception:
            pass

        return decision

    async def reflection_node(state: dict, config: RunnableConfig) -> dict:
        """Enforce the router contract: never leave a stale verdict readable.

        ``reflection_route`` is a pure conditional-edge function — it can read
        ``_reflection_result`` but cannot clear it. So any return path here that
        omits the key leaves the PREVIOUS superstep's verdict live, and a stale
        ``major`` verdict routes ``revise`` forever (those paths also skip the
        increment, so the ``>= 2`` cap never fires). That is exactly how trace
        01a00ce9 hit GraphRecursionError at step 145.

        Clearing with ``None`` rather than a synthetic passing verdict keeps the
        distinction between "evaluated and passed" and "not evaluated": both
        ``reflection_route`` and the two SSE handlers in ``api/agent/streaming``
        branch on ``is not None``, so a manufactured pass would emit a phantom
        "Reflection passed" frame for every turn that skipped the critique.

        The write only happens when there is something stale to clear, so a
        first-pass skip still returns the counter alone.
        """
        updates = await _reflection_node_impl(state, config)
        if (
            "_reflection_result" not in updates
            and state.get("_reflection_result") is not None
        ):
            updates["_reflection_result"] = None
        return updates

    return reflection_node, reflection_route
