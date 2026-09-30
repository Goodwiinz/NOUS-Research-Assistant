"""Contract test for the agent SSE event vocabulary (audit finding C1).

The agent stream used to spell its 12 wire event names as inline string
literals scattered across ~40 ``emitter.emit(...)`` sites, with the endpoint
docstring, the resume-path terminal set, and the frontend consumer switch each
re-listing a drifting subset. They are now anchored to a single source of truth:
``AgentStreamEvent`` (+ ``TERMINAL_STREAM_EVENTS``) in ``src/shared/enums.py``.

This test AST-walks ``api/agent/streaming.py`` and asserts every event name it
emits resolves to an ``AgentStreamEvent`` member — so a newly introduced raw
literal (or a typo'd enum member) fails CI instead of silently diverging from
the frontend. It also pins the frozen wire values, checks the terminal subset,
and guards that the resume path no longer hand-lists literal event lines.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.shared.enums import TERMINAL_STREAM_EVENTS, AgentStreamEvent

# backend/tests/contract/<this file> -> parents[2] == backend/
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_STREAMING_PY = _BACKEND_ROOT / "src" / "api" / "agent" / "streaming.py"
_HARNESS_STREAMING_PY = _BACKEND_ROOT / "src" / "api" / "agent" / "harness_streaming.py"

_ENUM_VALUES = frozenset(e.value for e in AgentStreamEvent)
_ENUM_MEMBER_NAMES = frozenset(AgentStreamEvent.__members__)

# The frozen wire contract. Changing any of these strings is a breaking change
# for every SSE client; this list is the deliberate tripwire.
_EXPECTED_WIRE_VALUES = [
    "token",
    "reasoning_delta",
    "tool_start",
    "tool_end",
    "rag_context",
    "plan",
    "reflection",
    "trace",
    "usage",
    "heartbeat",
    "status",
    "confirmation",
    "approval_required",
    "done",
    "error",
]


def _collect_emit_events(source: str) -> tuple[set[str], list[str]]:
    """Return (resolved wire values, offender descriptions) for every
    ``<x>.emit(<event>, ...)`` call site in ``source``.

    A call is an offender when its first positional argument is neither a
    known ``AgentStreamEvent`` member nor a string literal already in the
    enum — i.e. a drifted raw literal, a bad ``AgentStreamEvent.FOO`` name, or
    a dynamic expression. That is exactly the drift this contract forbids.
    """
    tree = ast.parse(source)
    values: set[str] = set()
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "emit"
        ):
            continue
        if not node.args:
            offenders.append(f"L{node.lineno}: emit() with no event-name argument")
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            # Raw literal at an emit site — only tolerated if it's a known enum
            # value; the whole point is to route these through AgentStreamEvent.
            if arg.value in _ENUM_VALUES:
                values.add(arg.value)
            else:
                offenders.append(
                    f"L{node.lineno}: raw literal {arg.value!r} is not an "
                    f"AgentStreamEvent value (use the enum, don't add a literal)"
                )
        elif (
            isinstance(arg, ast.Attribute)
            and isinstance(arg.value, ast.Name)
            and arg.value.id == "AgentStreamEvent"
        ):
            if arg.attr in _ENUM_MEMBER_NAMES:
                values.add(AgentStreamEvent[arg.attr].value)
            else:
                offenders.append(
                    f"L{node.lineno}: AgentStreamEvent.{arg.attr} is not a member"
                )
        else:
            offenders.append(
                f"L{node.lineno}: emit event name is neither an AgentStreamEvent "
                f"member nor a literal ({ast.dump(arg)})"
            )
    return values, offenders


def _collect_referenced_events(source: str) -> set[str]:
    """Collect enum-backed frame types used by non-emitter bridge adapters."""
    tree = ast.parse(source)
    return {
        AgentStreamEvent[node.attr].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "AgentStreamEvent"
        and node.attr in _ENUM_MEMBER_NAMES
    }


@pytest.fixture(scope="module")
def _streaming_emit_events() -> tuple[set[str], list[str]]:
    assert _STREAMING_PY.is_file(), f"cannot locate streaming.py at {_STREAMING_PY}"
    values, offenders = _collect_emit_events(_STREAMING_PY.read_text())
    values.update(_collect_referenced_events(_HARNESS_STREAMING_PY.read_text()))
    return values, offenders


@pytest.mark.unit
def test_enum_pins_the_frozen_wire_values() -> None:
    """The 12 wire strings are the SSE contract — pinned so a rename trips CI."""
    assert [e.value for e in AgentStreamEvent] == _EXPECTED_WIRE_VALUES


@pytest.mark.unit
def test_every_emit_site_uses_the_enum_vocabulary(_streaming_emit_events) -> None:
    """emit ⊆ AgentStreamEvent: no emit site may introduce a name outside the
    enum. A new raw literal or a typo'd ``AgentStreamEvent.FOO`` fails here."""
    _values, offenders = _streaming_emit_events
    assert (
        not offenders
    ), "streaming.py emits event names outside AgentStreamEvent:\n" + "\n".join(
        offenders
    )


@pytest.mark.unit
def test_every_enum_member_is_actually_emitted(_streaming_emit_events) -> None:
    """AgentStreamEvent ⊆ emit: no dead enum members. Combined with the subset
    check above this pins emitted-set == enum-value-set exactly."""
    values, _offenders = _streaming_emit_events
    assert values == _ENUM_VALUES, (
        "AgentStreamEvent members not emitted by streaming.py: "
        f"{sorted(_ENUM_VALUES - values)}; "
        f"emitted names not in enum: {sorted(values - _ENUM_VALUES)}"
    )


@pytest.mark.unit
def test_terminal_events_are_a_subset_of_the_enum() -> None:
    assert TERMINAL_STREAM_EVENTS <= set(AgentStreamEvent)
    assert {e.value for e in TERMINAL_STREAM_EVENTS} == {
        "done",
        "error",
        "confirmation",
    }


@pytest.mark.unit
def test_resume_path_uses_terminal_stream_events_not_a_literal_set() -> None:
    """Guards item C1(3): the resume replay loop must classify
    terminal frames via TERMINAL_STREAM_EVENTS, never a re-hand-listed set of
    ``"event: done"`` string literals."""
    execute_src = _STREAMING_PY.read_text()
    assert (
        "TERMINAL_STREAM_EVENTS" in execute_src
    ), "resume path should reference TERMINAL_STREAM_EVENTS"
    # The deleted hand-listed set uniquely contained these two literals; the
    # surviving ``"event: done"`` occurrence is an explanatory comment, so we
    # key the guard off the members that only ever lived inside the set.
    for literal in ('"event: error"', '"event: confirmation"'):
        assert literal not in execute_src, (
            f"resume path re-introduced a hand-listed terminal event literal "
            f"({literal}); classify via TERMINAL_STREAM_EVENTS instead"
        )
