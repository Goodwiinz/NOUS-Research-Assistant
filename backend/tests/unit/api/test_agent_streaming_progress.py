from src.api.agent.streaming import (
    _MAX_REASONING_SUMMARY_CHARS,
    _append_reasoning_summary,
    _chunk_reasoning_summary,
    _SeqEmitter,
)
from src.shared.enums import AgentStreamEvent


async def test_status_frames_accumulate_bounded_display_safe_progress() -> None:
    emitter = _SeqEmitter()

    for index in range(18):
        await emitter.emit(
            AgentStreamEvent.STATUS,
            {"phase": "planning", "detail": f"Step {index}"},
            buffer=False,
        )
    await emitter.emit(
        AgentStreamEvent.STATUS,
        {"phase": "planning", "detail": "Step 17"},
        buffer=False,
    )
    await emitter.emit(
        AgentStreamEvent.STATUS,
        {"phase": "private", "detail": "hidden"},
        buffer=False,
    )

    assert len(emitter.progress_steps) == 16
    assert emitter.progress_steps[0]["detail"] == "Step 2"
    assert emitter.progress_steps[-1] == {
        "phase": "planning",
        "detail": "Step 17",
    }


async def test_buffer_append_failure_is_logged_not_silent(monkeypatch, caplog):
    """Audit S2-M1 regression: a swallowed buffer-append gap must be logged
    with enough context to detect resumable-ledger divergence."""
    import logging

    from src.api.agent import streaming as streaming_mod

    emitter = _SeqEmitter()
    emitter.set_context(thread_id="thread-1")
    emitter.sid = "sid-x"

    async def boom(sid, seq, frame):
        raise RuntimeError("redis down")

    monkeypatch.setattr(streaming_mod._stream_buffer, "append", boom)

    with caplog.at_level(logging.WARNING):
        frame = await emitter.emit(AgentStreamEvent.STATUS, {"phase": "p"})

    assert frame  # stream continues despite the buffer failure
    warnings = [r for r in caplog.records if r.message == "stream_buffer.append failed"]
    assert len(warnings) == 1
    assert getattr(warnings[0], "stream_id", None) == "sid-x"
    assert getattr(warnings[0], "seq", None) == 1


async def test_reasoning_summary_extracts_public_typed_text_and_bounds_accumulation():
    chunk = type(
        "Chunk",
        (),
        {
            "content": [
                {
                    "type": "reasoning",
                    "summary": [
                        {"type": "summary_text", "text": "Public summary"},
                        {"type": "encrypted", "text": "private payload"},
                    ],
                },
                {"type": "text", "text": "answer"},
            ]
        },
    )()

    delta = _chunk_reasoning_summary(chunk)

    assert delta == "Public summary"
    assert "private payload" not in delta
    assert (
        len(_append_reasoning_summary("x" * _MAX_REASONING_SUMMARY_CHARS, delta))
        == _MAX_REASONING_SUMMARY_CHARS
    )
