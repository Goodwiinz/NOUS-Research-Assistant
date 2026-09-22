"""Contract test for the SSE ``error`` frame's server-authored ``category`` (W2).

An ``error`` frame is flat: ``{"error": "<safe string>", "category": "<label>"}``
with ``schema_version`` unchanged at ``"1.0"``. ``error`` stays a STRING — the
category is a *sibling* key, because turning ``error`` into an object would break
every deployed consumer.

Two guarantees are pinned here:

1. The frozen category wire values (``AgentErrorCategory``), in order. Renaming
   one is a breaking change for the frontend mirror in
   ``frontend/src/services/agentStreamEvents.ts``.
2. Every ``AgentStreamEvent.ERROR`` emit in ``api/agent/streaming.py`` builds its
   payload through ``error_frame_payload(...)`` (or passes an explicit
   ``AgentErrorCategory`` member). A future bare ``{"error": ...}`` emit fails
   here — that is the whole point, and ``test_the_ast_guard_actually_catches_a_
   violation`` proves the check has teeth against a synthetic offender.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import AbstractSet, List, Optional, Set, Tuple
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.shared.enums import AgentErrorCategory
from tests.utils.agent_stream import frames_of_type, sse_data
from tests.utils.agent_thread_access import editable_thread

# backend/tests/contract/<this file> -> parents[2] == backend/
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_STREAMING_PY = _BACKEND_ROOT / "src" / "api" / "agent" / "streaming.py"

_PAYLOAD_BUILDER = "error_frame_payload"
_ENUM_NAME = "AgentErrorCategory"
_ENUM_MEMBER_NAMES = frozenset(AgentErrorCategory.__members__)

# The frozen wire contract. Changing any of these strings breaks every client
# that branches on the category; this ordered list is the deliberate tripwire.
_EXPECTED_CATEGORY_VALUES = [
    "upstream_timeout",
    "model_error",
    "tool_error",
    "checkpoint_unavailable",
    "rate_limited",
    "cancelled",
    "invalid_request",
    "conflict",
    "internal",
]


def _error_event_aliases(tree: ast.AST) -> Set[str]:
    """Names bound to the ``error`` event anywhere in *tree*.

    Without this, ``event = AgentStreamEvent.ERROR`` followed by
    ``emitter.emit(event, {...})`` would slip past the guard entirely — the
    emit would not even be *counted* as an error emit, so a category-less,
    message-leaking frame could ship with the contract suite still green.
    """
    aliases: Set[str] = set()
    for node in ast.walk(tree):
        targets: List[ast.expr] = []
        value: Optional[ast.expr] = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        if value is None or not _is_error_event_arg(value):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                aliases.add(target.id)
    return aliases


def _is_error_event_arg(arg: ast.expr, aliases: AbstractSet[str] = frozenset()) -> bool:
    """True when *arg* names the ``error`` SSE event (enum member, literal or alias)."""
    if isinstance(arg, ast.Constant) and arg.value == "error":
        return True
    if isinstance(arg, ast.Name) and arg.id in aliases:
        return True
    return (
        isinstance(arg, ast.Attribute)
        and isinstance(arg.value, ast.Name)
        and arg.value.id == "AgentStreamEvent"
        and arg.attr == "ERROR"
    )


def _mentions_category_enum(node: ast.expr) -> bool:
    """True when *node* contains an ``AgentErrorCategory.<MEMBER>`` reference."""
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and isinstance(sub.value, ast.Name)
            and sub.value.id == _ENUM_NAME
            and sub.attr in _ENUM_MEMBER_NAMES
        ):
            return True
    return False


def _collect_error_emit_offenders(source: str) -> Tuple[int, List[str]]:
    """Return ``(error-emit count, offender descriptions)`` for *source*.

    An ``AgentStreamEvent.ERROR`` emit is compliant when its payload argument is
    a call to ``error_frame_payload(...)``. Anything else — a dict display, a
    name, a helper that forgot the category — is an offender, because only the
    single builder can guarantee the key names and the presence of ``category``.
    A payload that is not an ``error_frame_payload`` call is tolerated ONLY if it
    is a dict literal that both carries an ``"error"`` key and references an
    ``AgentErrorCategory`` member (the explicit-category escape hatch).
    """
    tree = ast.parse(source)
    aliases = _error_event_aliases(tree)
    offenders: List[str] = []
    count = 0

    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "emit"
        ):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        event_arg = node.args[0] if node.args else kwargs.get("event_type")
        if event_arg is None or not _is_error_event_arg(event_arg, aliases):
            continue
        count += 1
        payload = (
            node.args[1]
            if len(node.args) > 1
            else kwargs.get("data") or kwargs.get("payload")
        )
        if payload is None:
            offenders.append(f"L{node.lineno}: error emit with no payload argument")
            continue
        if (
            isinstance(payload, ast.Call)
            and isinstance(payload.func, ast.Name)
            and payload.func.id == _PAYLOAD_BUILDER
        ):
            continue
        if isinstance(payload, ast.Dict) and _mentions_category_enum(payload):
            keys = {
                k.value
                for k in payload.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            if "error" in keys and "category" in keys:
                continue
            offenders.append(
                f"L{node.lineno}: error-frame dict literal is missing "
                f"'error'/'category' (has {sorted(keys)})"
            )
            continue
        offenders.append(
            f"L{node.lineno}: error frame payload does not come from "
            f"{_PAYLOAD_BUILDER}() and carries no explicit {_ENUM_NAME} member "
            f"({type(payload).__name__})"
        )
    return count, offenders


@pytest.fixture(scope="module")
def _streaming_error_emits() -> Tuple[int, List[str]]:
    assert _STREAMING_PY.is_file(), f"cannot locate streaming.py at {_STREAMING_PY}"
    return _collect_error_emit_offenders(_STREAMING_PY.read_text())


@pytest.mark.unit
def test_enum_pins_the_frozen_category_wire_values() -> None:
    """The category strings are the wire contract — a rename must trip CI."""
    assert [c.value for c in AgentErrorCategory] == _EXPECTED_CATEGORY_VALUES


@pytest.mark.unit
def test_every_error_emit_builds_its_payload_through_the_single_builder(
    _streaming_error_emits: Tuple[int, List[str]],
) -> None:
    count, offenders = _streaming_error_emits
    assert count > 0, "no AgentStreamEvent.ERROR emit sites found — test is blind"
    assert not offenders, (
        "streaming.py emits error frames without a server-authored category:\n"
        + "\n".join(offenders)
    )


@pytest.mark.unit
def test_the_ast_guard_actually_catches_a_violation() -> None:
    """Self-test: a bare ``{"error": ...}`` emit MUST be reported. Without this
    the guard above could pass by never matching anything."""
    violating = (
        "async def f():\n"
        "    await emitter.emit(AgentStreamEvent.ERROR, {'error': 'boom'})\n"
    )
    count, offenders = _collect_error_emit_offenders(violating)
    assert count == 1
    assert len(offenders) == 1 and "L2" in offenders[0]

    # ...and the two compliant shapes must NOT be reported.
    compliant_builder = (
        "async def f():\n"
        "    await emitter.emit(AgentStreamEvent.ERROR, error_frame_payload(e))\n"
    )
    assert _collect_error_emit_offenders(compliant_builder) == (1, [])

    compliant_explicit = (
        "async def f():\n"
        "    await emitter.emit(\n"
        "        AgentStreamEvent.ERROR,\n"
        "        {'error': 'x', 'category': AgentErrorCategory.CONFLICT.value},\n"
        "    )\n"
    )
    assert _collect_error_emit_offenders(compliant_explicit) == (1, [])

    # A dict that name-drops the enum but omits the key is still a violation.
    sneaky = (
        "async def f():\n"
        "    await emitter.emit(\n"
        "        AgentStreamEvent.ERROR,\n"
        "        {'error': AgentErrorCategory.CONFLICT.value},\n"
        "    )\n"
    )
    sneaky_count, sneaky_offenders = _collect_error_emit_offenders(sneaky)
    assert sneaky_count == 1 and len(sneaky_offenders) == 1


class _ConfirmDenialGraph:
    def __init__(self, snapshot: object) -> None:
        self.snapshot = snapshot

    async def aget_state(self, _config: object) -> object:
        return self.snapshot


async def _confirm_denial_payload(
    snapshot: object,
    *,
    accessible: bool,
) -> tuple[dict[str, object], AsyncMock]:
    from src.api.agent.streaming import stream_confirm_event_generator

    thread_id = "11111111-1111-4111-8111-111111111612"
    db = AsyncMock()
    graph = _ConfirmDenialGraph(snapshot)
    access_result = editable_thread(thread_id) if accessible else None
    get_thread = AsyncMock(return_value=access_result)

    with (
        patch("src.api.agent.streaming.AsyncSessionLocal", return_value=db),
        patch(
            "src.services.threads.workspace_access.get_thread",
            new=get_thread,
        ),
        patch(
            "src.services.agent.observability.configure_langsmith",
            return_value=None,
        ),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.checkpointer.reset_checkpointer",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
    ):
        frames = [
            frame
            async for frame in stream_confirm_event_generator(
                SimpleNamespace(thread_id=thread_id, confirmed=True, model=""),
                SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
                Mock(id="user-1", organization_id="org-1"),
            )
        ]

    errors = frames_of_type(frames, "error")
    assert len(errors) == 1
    payload: dict[str, object] = sse_data(errors[0])
    return payload, get_thread


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_denials_cannot_oracle_thread_existence() -> None:
    """Access denial, missing checkpoint, and foreign checkpoint are identical."""
    owned_durable = SimpleNamespace(
        values={"user_id": "user-1", "thread_persistence": "durable"},
        tasks=(),
        config={"configurable": {"checkpoint_id": "ckpt-owned"}},
    )
    missing = SimpleNamespace(values={}, tasks=(), config={})
    foreign = SimpleNamespace(
        values={"user_id": "someone-else", "thread_persistence": "durable"},
        tasks=(),
        config={"configurable": {"checkpoint_id": "ckpt-foreign"}},
    )

    denied_payload, denied_access = await _confirm_denial_payload(
        owned_durable,
        accessible=False,
    )
    missing_payload, missing_access = await _confirm_denial_payload(
        missing,
        accessible=True,
    )
    foreign_payload, foreign_access = await _confirm_denial_payload(
        foreign,
        accessible=True,
    )

    denied_access.assert_awaited_once()
    missing_access.assert_not_awaited()
    foreign_access.assert_not_awaited()

    envelope_keys = {"event_id", "occurred_at", "trace_id"}

    def observable(payload: dict[str, object]) -> dict[str, object]:
        return {
            key: value for key, value in payload.items() if key not in envelope_keys
        }

    assert observable(denied_payload) == observable(missing_payload)
    assert observable(missing_payload) == observable(foreign_payload)
    assert missing_payload["error"] == "Thread not found"
    assert missing_payload["category"] == AgentErrorCategory.INVALID_REQUEST.value


def test_guard_catches_an_aliased_event_variable() -> None:
    """An alias must not let a bare payload through.

    ``event = AgentStreamEvent.ERROR`` then ``emit(event, {...})`` was
    previously invisible to the walker: the emit was never counted, so a
    category-less frame shipped with the suite green.
    """
    source = (
        "event = AgentStreamEvent.ERROR\n"
        'await emitter.emit(event, {"error": str(exc)})\n'
    )
    count, offenders = _collect_error_emit_offenders(source)
    assert count == 1, "aliased error emit must still be counted"
    assert offenders, "aliased emit with a bare dict payload must be an offender"


def test_guard_catches_keyword_arguments() -> None:
    """Keyword-form emits are held to the same contract as positional ones."""
    source = (
        "await emitter.emit(\n"
        "    event_type=AgentStreamEvent.ERROR,\n"
        '    data={"error": str(exc)},\n'
        ")\n"
    )
    count, offenders = _collect_error_emit_offenders(source)
    assert count == 1, "keyword error emit must still be counted"
    assert offenders, "keyword emit with a bare dict payload must be an offender"
