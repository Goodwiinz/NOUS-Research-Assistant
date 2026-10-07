"""Deterministic scripted chat model for offline golden replay (B2, Phase 1).

``GoldenReplayLLM`` stands in for the real Azure chat model at the LLM
construction seams during a golden eval run, so cases execute with **no
credentials** and **no model nondeterminism**. It is seeded per
``GoldenCase`` from a committed cassette (see
``docs/testing/b2-llm-replay-design.md`` for the full design).

Phase 1 lands the machinery only — it is inert unless ``AGENT_GOLDEN_REPLAY``
is set, and cassette files arrive in Phase 2. The model is intentionally a
plain duck-typed object (not a ``BaseChatModel`` subclass), mirroring the
house ``MockChatModel`` in ``tests/integration/agent/conftest.py``: it only
needs ``with_structured_output``, ``bind_tools``, and (a)``invoke``. Stock
LangChain fakes are unusable here because their ``bind_tools`` /
``with_structured_output`` raise ``NotImplementedError``.

Replay is keyed by call KIND (and schema name for structured calls), not by
prompt hash — so prompt-whitespace edits don't invalidate a cassette, but a
topology change that asks for an un-recorded structured output fails LOUDLY
(``CassetteExhausted``), never silently falling back to a live model.
"""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

CASSETTE_DIR = Path(__file__).with_name("cassettes")


class CassetteExhausted(RuntimeError):
    """A structured output was requested that the cassette does not contain.

    Signals an agent-topology change (new/extra LLM call) — the cassette must
    be re-recorded. Raised rather than silently going to a live model.
    """


def replay_enabled() -> bool:
    """True when golden replay is active (gated to keep Phase 1 inert)."""
    return os.environ.get("AGENT_GOLDEN_REPLAY") == "1"


def _to_ai_message(msg: dict[str, Any] | None) -> AIMessage:
    """Build an AIMessage from a recorded ``{content, tool_calls}`` dict."""
    if msg is None:
        return AIMessage(content="")
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        tool_calls.append({**tc, "type": tc.get("type", "tool_call")})
    return AIMessage(content=msg.get("content", ""), tool_calls=tool_calls)


class GoldenCassette:
    """Replay queues parsed from a cassette dict, grouped by call kind.

    Structured calls are bucketed by schema name (FIFO per schema) so the
    classifier keyword fast-path — which may skip the ``IntentClassification``
    LLM call entirely — simply leaves that payload unconsumed instead of
    desynchronising an ordinal cursor.
    """

    def __init__(self, data: dict[str, Any]):
        self.case: str = data.get("case", "?")
        self._structured: dict[str, deque] = {}
        self._tool: deque = deque()
        self._text: deque = deque()
        for call in data.get("calls", []):
            kind = call.get("kind")
            if kind == "structured":
                self._structured.setdefault(call["schema"], deque()).append(
                    call["payload"]
                )
            elif kind == "tool":
                self._tool.append(call["message"])
            elif kind == "text":
                self._text.append(call["message"])

    def next_structured(self, schema_name: str) -> dict[str, Any]:
        queue = self._structured.get(schema_name)
        if not queue:
            raise CassetteExhausted(
                f"cassette {self.case!r}: no recorded {schema_name} output left "
                "(agent topology changed — re-record the cassette)"
            )
        return queue.popleft()

    def next_tool(self) -> dict[str, Any] | None:
        return self._tool.popleft() if self._tool else None

    def next_text(self) -> dict[str, Any] | None:
        return self._text.popleft() if self._text else None


class _StructuredReplay:
    """Runnable returned by ``with_structured_output`` — yields the recorded
    pydantic instance for its schema at the current slot."""

    def __init__(self, cassette: GoldenCassette, schema: Any):
        self._cassette = cassette
        self._schema = schema

    def _build(self):
        payload = self._cassette.next_structured(self._schema.__name__)
        return self._schema(**payload)

    async def ainvoke(self, _messages: Any, *_a: Any, **_kw: Any):
        return self._build()

    def invoke(self, _messages: Any, *_a: Any, **_kw: Any):
        return self._build()


class _ToolReplay:
    """Runnable returned by ``bind_tools`` — yields the scripted tool-call
    AIMessage, then a terminal tool-less AIMessage when the script is spent
    (so a tool loop ends gracefully rather than erroring)."""

    def __init__(self, cassette: GoldenCassette):
        self._cassette = cassette

    async def ainvoke(self, _messages: Any, *_a: Any, **_kw: Any) -> AIMessage:
        return _to_ai_message(self._cassette.next_tool())

    def invoke(self, _messages: Any, *_a: Any, **_kw: Any) -> AIMessage:
        return _to_ai_message(self._cassette.next_tool())


class GoldenReplayLLM:
    """Duck-typed scripted chat model seeded from a cassette."""

    def __init__(self, cassette: GoldenCassette):
        self.cassette = cassette

    def with_structured_output(self, schema: Any, **_kw: Any) -> _StructuredReplay:
        return _StructuredReplay(self.cassette, schema)

    def bind_tools(self, tools: Any = None, **_kw: Any) -> _ToolReplay:
        return _ToolReplay(self.cassette)

    async def ainvoke(self, _messages: Any, *_a: Any, **_kw: Any) -> AIMessage:
        return _to_ai_message(self.cassette.next_text())

    def invoke(self, _messages: Any, *_a: Any, **_kw: Any) -> AIMessage:
        return _to_ai_message(self.cassette.next_text())


async def stub_tool_executor(*, tool_name: str, args: Any = None, **_kw: Any) -> dict:
    """No-op tool executor for replay — returns a benign result without touching
    any infra (DB / Neo4j / Qdrant / arXiv / network).

    Golden cases assert tool-call NAMES (captured from the AIMessage at emission
    time, before execution) and the final intent — never tool *results* — so a
    stub is sufficient and removes the last live dependency, making the replay
    gate fully infra-free (B2 Phase 5).
    """
    return {"status": "stubbed-in-replay", "tool": tool_name}


def load_cassette(case_name: str) -> GoldenCassette:
    path = CASSETTE_DIR / f"{case_name}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"no replay cassette for golden case {case_name!r}: {path} "
            "(record it with record_golden_cassette.py — see B2 design doc)"
        )
    return GoldenCassette(json.loads(path.read_text()))
