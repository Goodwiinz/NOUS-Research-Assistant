"""Audit S2-M2 regression: rag_context / PLAN / REFLECTION frames bypassed
every byte bound on the live+buffered SSE path — live caps counted items,
the Redis buffer ltrim'd frame counts, and MAX_PAYLOAD_BYTES was enforced
only on the durable ledger. A single oversized retrieval context therefore
amplified into both the live socket and up to 5000 replayed copies.

The emitter must clamp any oversized payload to the shared MAX_PAYLOAD_BYTES
budget *before* the frame reaches the wire or the resumable buffer, so live
consumers and replays see byte-identical bounded frames.
"""

import json

import pytest

from src.api.agent import streaming as streaming_mod
from src.api.agent.streaming import _SeqEmitter
from src.services.agent.run_event_types import MAX_PAYLOAD_BYTES
from src.shared.enums import AgentStreamEvent

# The envelope adds fixed per-frame metadata (schema_version, sequence,
# event_id, occurred_at, trace_id, thread_id, route) around the clamped
# payload — allow slack for it on top of the payload budget.
_ENVELOPE_SLACK_BYTES = 2048


def _frame_data(frame: str) -> dict:
    for line in frame.splitlines():
        if line.startswith("data: "):
            data: dict = json.loads(line[len("data: ") :])
            return data
    raise AssertionError("frame carries no data line")


async def _emitter_capturing_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_SeqEmitter, list[str]]:
    captured: list[str] = []

    async def fake_append(sid: str, seq: int, frame: str) -> None:
        captured.append(frame)

    monkeypatch.setattr(streaming_mod._stream_buffer, "append", fake_append)
    emitter = _SeqEmitter()
    emitter.set_context(thread_id="thread-1")
    emitter.sid = "sid-bounds"
    return emitter, captured


async def test_oversized_rag_context_bounded_on_wire_and_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitter, buffered = await _emitter_capturing_buffer(monkeypatch)
    giant_contexts = [{"text": "x" * 200_000} for _ in range(3)]

    frame = await emitter.emit(
        AgentStreamEvent.RAG_CONTEXT, {"contexts": giant_contexts}
    )

    assert len(frame.encode("utf-8")) <= MAX_PAYLOAD_BYTES + _ENVELOPE_SLACK_BYTES
    # Replay must see exactly what live saw — one clamp point feeds both.
    assert buffered == [frame]
    data = _frame_data(frame)
    assert data["payload_truncated"] is True
    # Shape survives clipping: still a list of context dicts.
    assert isinstance(data["contexts"], list) and len(data["contexts"]) == 3
    assert all(
        isinstance(ctx, dict) and isinstance(ctx["text"], str)
        for ctx in data["contexts"]
    )


async def test_rag_context_keeps_all_twenty_sources_under_utf8_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitter, buffered = await _emitter_capturing_buffer(monkeypatch)
    contexts = [
        {
            "document_id": f"00000000-0000-0000-0000-{index:012d}",
            "chunk_id": f"chunk-{index}-" + "a" * 80,
            "title": "論文" * 500,
            "content": (f"來源 {index} " + "🧪" * 3000),
            "score": 0.8,
            "page_number": index,
            "tool_call_id": "must-not-leak",
        }
        for index in range(20)
    ]

    frame = await emitter.emit(AgentStreamEvent.RAG_CONTEXT, {"contexts": contexts})

    assert len(frame.encode("utf-8")) <= MAX_PAYLOAD_BYTES + _ENVELOPE_SLACK_BYTES
    assert buffered == [frame]
    public_contexts = _frame_data(frame)["contexts"]
    assert len(public_contexts) == 20
    assert [context["source_position"] for context in public_contexts] == list(
        range(1, 21)
    )
    assert [context["document_id"] for context in public_contexts] == [
        context["document_id"] for context in contexts
    ]
    assert [context["chunk_id"] for context in public_contexts] == [
        context["chunk_id"] for context in contexts
    ]
    assert [context["page_number"] for context in public_contexts] == list(range(20))
    assert all("tool_call_id" not in context for context in public_contexts)


async def test_oversized_plan_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    emitter, _ = await _emitter_capturing_buffer(monkeypatch)

    frame = await emitter.emit(
        AgentStreamEvent.PLAN,
        {
            "steps": [
                {"title": f"step-{i}", "detail": "y" * 50_000} for i in range(200)
            ],
            "reasoning": "z" * 300_000,
        },
    )

    assert len(frame.encode("utf-8")) <= MAX_PAYLOAD_BYTES + _ENVELOPE_SLACK_BYTES
    assert _frame_data(frame)["payload_truncated"] is True


async def test_oversized_reflection_issues_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitter, _ = await _emitter_capturing_buffer(monkeypatch)

    frame = await emitter.emit(
        AgentStreamEvent.REFLECTION,
        {
            "passed": False,
            "issues": ["w" * 120_000 for _ in range(10)],
            "round": 1,
            "revising": True,
        },
    )

    assert len(frame.encode("utf-8")) <= MAX_PAYLOAD_BYTES + _ENVELOPE_SLACK_BYTES
    data = _frame_data(frame)
    assert data["payload_truncated"] is True
    assert data["passed"] is False and data["revising"] is True


async def test_normal_sized_payload_passes_through_untouched() -> None:
    """Under-budget frames keep their exact pre-clamp shape — no marker, no
    mutation — so this is a no-op for every well-behaved producer."""
    emitter = _SeqEmitter()
    emitter.set_context(thread_id="thread-1")

    payload = {
        "contexts": [{"title": "Paper", "score": 0.87, "snippet": "short"}],
    }
    frame = await emitter.emit(AgentStreamEvent.RAG_CONTEXT, payload, buffer=False)

    data = _frame_data(frame)
    assert data["contexts"] == [{**payload["contexts"][0], "source_position": 1}]
    assert "payload_truncated" not in data


async def test_clamped_frames_stay_individually_valid_sse() -> None:
    """A pathological multi-megabyte payload must not wedge the stream: the
    emitter degrades to a bounded frame instead of raising."""
    emitter = _SeqEmitter()
    emitter.set_context(thread_id="thread-1")
    pathological = {"contexts": [{"text": "\uffff" * 5_000_000}]}

    frame = await emitter.emit(AgentStreamEvent.RAG_CONTEXT, pathological, buffer=False)

    assert frame.startswith("id: 1\nevent: rag_context\ndata: ")
    assert len(frame.encode("utf-8")) <= MAX_PAYLOAD_BYTES + _ENVELOPE_SLACK_BYTES


async def test_many_keyed_payload_degrades_to_identity_stub() -> None:
    """The clip ladder bottoms out at (64, 1) per *value* — a payload with
    enough top-level keys is still over budget after every rung. Such a frame
    must degrade to an identity stub rather than ship over the budget."""
    emitter = _SeqEmitter()
    emitter.set_context(thread_id="thread-1")

    payload: dict = {
        "call_id": "call-abc123",
        "note_id": "note-42",
        "round": 3,
        "passed": False,
        "revising": True,
        "score": 0.87,
        "cursor": None,
    }
    # 4000 bulk keys: even clipped to 64 chars each this far exceeds the budget.
    for i in range(4000):
        payload[f"chunk_{i}"] = "q" * 4000

    frame = await emitter.emit(AgentStreamEvent.RAG_CONTEXT, payload, buffer=False)

    data = _frame_data(frame)
    assert len(frame.encode("utf-8")) <= MAX_PAYLOAD_BYTES + _ENVELOPE_SLACK_BYTES
    assert data["payload_truncated"] is True
    assert data["payload_stub"] is True
    # Identity survives, with types intact — this is what the stub exists for.
    assert data["call_id"] == "call-abc123"
    assert data["note_id"] == "note-42"
    assert data["round"] == 3
    assert data["passed"] is False
    assert data["revising"] is True
    assert data["score"] == 0.87
    assert data["cursor"] is None
    # Bulk is gone entirely, not merely shortened.
    assert not any(key.startswith("chunk_") for key in data)


async def test_stub_floor_fits_budget_with_unbounded_identity_keys() -> None:
    """The stub is built under a running budget, so it fits even when the
    identity fields themselves are too numerous to all survive."""
    payload = {f"id_{i}": f"value-{i}" for i in range(20_000)}

    stub = streaming_mod._stub_identity_payload(payload)

    assert len(json.dumps(stub)) <= MAX_PAYLOAD_BYTES
    assert stub["payload_truncated"] is True and stub["payload_stub"] is True
    # Kept in producer order, and every survivor is verbatim (never clipped).
    kept = [key for key in stub if key.startswith("id_")]
    assert kept == sorted(kept, key=lambda k: int(k.removeprefix("id_")))
    assert all(stub[key] == payload[key] for key in kept)
